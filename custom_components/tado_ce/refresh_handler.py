"""Tado CE immediate-refresh handler: debounces rapid state changes into one poll.

Calls the coordinator's `async_request_refresh()` after a brief
debounce so several user actions in quick succession (mode change +
target temperature + presence) collapse into a single API call
instead of N. Rate-limit gates and an exponential backoff sit in
front of the refresh so a degrading API can't be hammered further.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from . import const
from .cloud_health import BACKOFF_FLOOR_SECONDS, CloudHealthState
from .const import MECH_REFRESH_PROBE
from .fetch_plan import FetchPlan, PollOutcome, requeue_flags
from .quota_levels import QuotaLevels

if TYPE_CHECKING:
    from .cloud_health import CloudHealth
    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Entity types that should trigger immediate refresh
REFRESH_ENTITY_TYPES = {
    "climate",  # Temperature and HVAC mode changes
    "switch",  # Switch toggles
    "water_heater",  # Hot water state changes
    "select",  # Presence mode changes
}


@dataclass
class _VerifyGroup:
    """Puts sharing one verification fetch: each zone's latest put time, the fetch not-before and its timer."""

    puts: dict[str, datetime]
    not_before: datetime
    timer: asyncio.TimerHandle | None = None


class RefreshHandler:
    """Coalesce post-action refreshes into one debounced coordinator call."""

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        """Initialise the handler bound to one coordinator (one config entry)."""
        self._coordinator = coordinator
        self.hass = coordinator.hass
        self._global_last_refresh: datetime | None = None
        self._min_global_interval = 2
        self._consecutive_failures = 0
        self._max_backoff_interval = 300

        self._pending_refresh: bool = False
        self._pending_force_zone_fetch: bool = False
        self._pending_forced_fetch: set[str] = set()
        self._debounce_task: asyncio.Task[None] | None = None
        self._debounce_delay = 15.0
        self._refresh_in_flight = False
        self._followup_reason = "state_change"
        self._followup_skip_debounce = False
        self._verify_groups: list[_VerifyGroup] = []
        self._last_probe_at: datetime | None = None

    def _get_debounce_delay(self) -> float:
        """Return the configured debounce delay in seconds, defaulting on error."""
        try:
            return float(self._coordinator.config_manager.get_refresh_debounce_seconds())
        except Exception as e:
            _LOGGER.debug(
                "Refresh: could not read debounce delay from config (%s), "
                "using default %.0fs",
                e, self._debounce_delay,
            )
        return self._debounce_delay

    def cancel(self) -> None:
        """Cancel any pending debounced refresh and verification request."""
        if self._debounce_task is not None:
            self._debounce_task.cancel()
            self._debounce_task = None
        for group in self._verify_groups:
            if group.timer is not None:
                group.timer.cancel()
        self._verify_groups = []

    def schedule_homekit_verification(self, put_at: datetime, lag_seconds: float, entity_id: str) -> None:
        """Request a forced zone fetch at put_at + lag_seconds; puts within the chain bound share one fetch."""
        group = self._verify_groups[-1] if self._verify_groups else None
        # Read at call time so a test can patch it; the bound caps how far chained puts push the shared fetch.
        max_chain = timedelta(seconds=const.HOMEKIT_CHAINED_FETCH_MAX_SECONDS)
        # A zone keeps only its latest put, so its own earlier put no longer counts towards the bound.
        puts = {**group.puts, entity_id: max(put_at, group.puts.get(entity_id, put_at))} if group is not None else {}
        if group is not None and max(puts.values()) - min(puts.values()) <= max_chain:
            if group.timer is not None:
                group.timer.cancel()
            group.puts = puts
        else:
            group = _VerifyGroup(puts={entity_id: put_at}, not_before=put_at)
            self._verify_groups.append(group)
        group.not_before = max(group.not_before, put_at + timedelta(seconds=lag_seconds))
        self._arm_verification(group, entity_id)

    def _arm_verification(self, group: _VerifyGroup, entity_id: str) -> None:
        """Arm the group timer for the time left until its wall-clock not-before."""
        remaining = max(0.0, (group.not_before - dt_util.utcnow()).total_seconds())
        group.timer = asyncio.get_running_loop().call_later(remaining, self._fire_verification, group, entity_id)

    def _fire_verification(self, group: _VerifyGroup, entity_id: str) -> None:
        """Fetch when the wall clock has reached not-before, else wait the remainder."""
        group.timer = None
        if dt_util.utcnow() < group.not_before:
            # The loop clock fired ahead of the wall clock; never fetch early.
            self._arm_verification(group, entity_id)
            return
        self._verify_groups.remove(group)
        self.hass.async_create_task(
            self.trigger_refresh(entity_id, "homekit_verification", force=True, skip_debounce=True),
        )

    def request_forced_fetch(self, fetch_type: str) -> None:
        """Queue a floored data type to bypass its floor on the next poll."""
        self._pending_forced_fetch.add(fetch_type)

    def request_forced_zone_fetch(self) -> None:
        """Force the next poll to fetch zone states even inside the HomeKit skip window."""
        self._pending_force_zone_fetch = True

    def requeue(
        self,
        plan: FetchPlan,
        outcome: PollOutcome,
        satisfied: frozenset[str] = frozenset(),
        enabled_types: frozenset[str] = frozenset(),
    ) -> None:
        """Put back the flags a poll consumed and did not use; it only adds and never raises."""
        try:
            back = requeue_flags(plan, outcome, satisfied=satisfied, enabled_types=enabled_types)
            if back.force_zone:
                self._pending_force_zone_fetch = True
            self._pending_forced_fetch.update(back.forced_types)
        except Exception:  # noqa: BLE001 - runs on a failing exit; it must never replace the original error
            _LOGGER.warning("RefreshHandler: could not requeue the pending fetch flags", exc_info=True)

    def consume_pending_flags(self) -> tuple[bool, set[str]]:
        """Atomically read and reset the force-zone flag and the forced slow-data types, once per poll."""
        force_zone_fetch = self._pending_force_zone_fetch
        forced_fetch = self._pending_forced_fetch
        self._pending_force_zone_fetch = False
        self._pending_forced_fetch = set()
        return force_zone_fetch, forced_fetch

    async def _get_rate_limit_info(self) -> dict[str, Any]:
        """Read the latest rate-limit snapshot from the data loader cache."""
        try:
            return self._coordinator.data_loader.load_ratelimit_file() or {}
        except Exception as e:
            _LOGGER.debug(
                "Refresh: could not read rate-limit snapshot (%s), "
                "treating quota as unknown",
                e,
            )
        return {}

    async def _check_quota_available(self) -> tuple[bool, str]:
        """Return (can_refresh, reason); refuses while the cloud holds except one probe a minute while UNREACHABLE."""
        coordinator = self._coordinator
        if coordinator.cloud_health.holds():
            if not self._probe_due(coordinator.cloud_health):
                coordinator.mechanism_counters.count(MECH_REFRESH_PROBE, "refused")
                return False, "cloud_held"
            # A probe still waits for the quota like any other refresh.
            can_probe, quota_reason = await self._quota_verdict()
            if not can_probe:
                return False, quota_reason
            self._last_probe_at = dt_util.utcnow()
            coordinator.mechanism_counters.count(MECH_REFRESH_PROBE, "allowed", calls=1)
            _LOGGER.debug("Refresh: cloud unreachable, a user refresh is allowed as the once-a-minute probe")
            return True, "probe"
        return await self._quota_verdict()

    async def _quota_verdict(self) -> tuple[bool, str]:
        """Return (can_refresh, reason) from the quota alone; fails open when quota info is missing."""
        coordinator = self._coordinator
        rl_info = await self._get_rate_limit_info()

        if not rl_info:
            return True, "no_rate_limit_data"

        levels = QuotaLevels.build(rl_info, coordinator.config_manager)
        if levels.poll_holds(rl_info, coordinator.refresh_plan_cost(), dt_util.utcnow()):
            return False, f"quota_held (remaining={rl_info.get('remaining')}, reserve={levels.hard_reserve})"
        return True, "ok"

    def _probe_due(self, health: CloudHealth) -> bool:
        """Return True when the cloud is UNREACHABLE and the last probe is a minute old."""
        if health.state is not CloudHealthState.UNREACHABLE:
            return False
        last = self._last_probe_at
        return last is None or (dt_util.utcnow() - last).total_seconds() >= BACKOFF_FLOOR_SECONDS

    def _get_backoff_interval(self) -> int:
        """Return the next backoff window: exponential, capped at 5 minutes."""
        if self._consecutive_failures == 0:
            return self._min_global_interval

        backoff = self._min_global_interval * (2**self._consecutive_failures)
        return min(backoff, self._max_backoff_interval)  # type: ignore[no-any-return]

    def should_refresh(self, entity_id: str) -> bool:
        """Return True when the entity's domain warrants an immediate refresh."""
        domain = entity_id.split(".", maxsplit=1)[0]
        return domain in REFRESH_ENTITY_TYPES


    async def _execute_debounced_refresh(self, reason: str, skip_debounce: bool) -> None:
        """Run debounced refreshes until no trigger that arrived during a poll is left pending."""
        try:
            while True:
                await self._debounced_pass(reason, skip_debounce)
                if not self._pending_refresh:
                    return
                reason, skip_debounce = self._followup_reason, self._followup_skip_debounce
                self._followup_skip_debounce = False
        finally:
            # A cancelled task must not clear the handle of the task that replaced it.
            if self._debounce_task is asyncio.current_task():
                self._debounce_task = None

    async def _debounced_pass(self, reason: str, skip_debounce: bool) -> None:
        """Wait out the debounce, then run one refresh through the coordinator."""
        if not skip_debounce:
            delay = self._get_debounce_delay()
            await asyncio.sleep(delay)

        if not self._pending_refresh:
            return

        self._pending_refresh = False

        now = dt_util.utcnow()
        if self._global_last_refresh:
            global_elapsed = (now - self._global_last_refresh).total_seconds()
            required_global = self._get_backoff_interval()
            if global_elapsed < required_global:
                _LOGGER.debug(
                    "Refresh: global backoff active (%ds remaining)",
                    int(required_global - global_elapsed),
                )
                # A newer trigger re-sets the pending flag and cancels this wait.
                await asyncio.sleep(required_global - global_elapsed)

        _LOGGER.debug(
            "Refresh: running debounced refresh (triggered by %s)", reason,
        )

        try:
            # A poll still running would make HA's Debouncer drop this request after its cooldown.
            await self._coordinator.async_wait_for_poll_end()
            self._refresh_in_flight = True
            try:
                await self._coordinator.async_request_refresh()
            finally:
                self._refresh_in_flight = False
            self._global_last_refresh = dt_util.utcnow()

            if self._consecutive_failures > 0:
                _LOGGER.info(
                    "Refresh: recovered after %d consecutive failure(s)",
                    self._consecutive_failures,
                )
                self._consecutive_failures = 0

            _LOGGER.debug("Refresh: coordinator refresh completed")

        except Exception:
            self._consecutive_failures += 1
            _LOGGER.warning(
                "Refresh: coordinator refresh failed (attempt %d), "
                "next try in %ds",
                self._consecutive_failures, self._get_backoff_interval(),
                exc_info=True,
            )

    async def trigger_refresh(
        self,
        entity_id: str,
        reason: str = "state_change",
        force: bool = False,
        skip_debounce: bool = False,
    ) -> bool:
        """Schedule a debounced coordinator refresh and return False when it was refused.

        `force=True` bypasses the entity-domain filter (used by buttons
        like "Resume All Schedules"). `skip_debounce=True` runs
        immediately, useful when the user expects an instant response.
        """
        if not force and not self.should_refresh(entity_id):
            _LOGGER.debug(
                "Refresh: %s is not in a domain that triggers immediate refresh",
                entity_id,
            )
            return False

        can_refresh, quota_reason = await self._check_quota_available()
        if not can_refresh:
            _LOGGER.debug(
                "Refresh: skipping immediate refresh for %s (%s), "
                "next normal poll will catch the change, zone fetch kept for the next poll",
                entity_id, quota_reason,
            )
            # The needed zone fetch must not be lost with the refused refresh.
            self._pending_force_zone_fetch = True
            return False

        _LOGGER.debug(
            "Refresh: queued debounced refresh for %s (reason=%s)",
            entity_id, reason,
        )

        self._pending_refresh = True
        self._pending_force_zone_fetch = True

        if self._refresh_in_flight:
            # Cancelling would kill the poll itself; the running task makes one more pass for these flags.
            self._followup_reason = reason
            self._followup_skip_debounce = self._followup_skip_debounce or skip_debounce
            return True

        if self._debounce_task is not None:
            self._debounce_task.cancel()
            self._debounce_task = None

        self._followup_skip_debounce = False
        self._debounce_task = asyncio.create_task(
            self._execute_debounced_refresh(reason, skip_debounce),
        )
        return True
