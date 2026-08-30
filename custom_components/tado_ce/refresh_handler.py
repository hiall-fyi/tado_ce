"""Tado CE immediate-refresh handler: debounces rapid state changes into one poll.

Calls the coordinator's `async_request_refresh()` after a brief
debounce so several user actions in quick succession (mode change +
target temperature + presence) collapse into a single API call
instead of N. Rate-limit gates and an exponential backoff sit in
front of the refresh so a degrading API can't be hammered further.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Entity types that should trigger immediate refresh
REFRESH_ENTITY_TYPES = {
    "climate",  # Temperature and HVAC mode changes
    "switch",  # Switch toggles
    "water_heater",  # Hot water state changes
    "select",  # Presence mode changes
}

# Rate limiting thresholds
QUOTA_WARNING_THRESHOLD = 0.8  # 80% quota used
QUOTA_CRITICAL_THRESHOLD = 0.9  # 90% quota used
MIN_QUOTA_PERCENTAGE_FOR_REFRESH = 0.10  # Minimum 10% remaining to allow refresh


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
        self._pending_zone_only: bool = False
        self._pending_force_zone_fetch: bool = False
        self._pending_forced_fetch: set[str] = set()
        self._debounce_task: asyncio.Task[None] | None = None
        self._debounce_delay = 15.0

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
        """Cancel any pending debounced refresh."""
        if self._debounce_task is not None:
            self._debounce_task.cancel()
            self._debounce_task = None

    def request_forced_fetch(self, fetch_type: str) -> None:
        """Queue a floored data type to bypass its floor on the next poll."""
        self._pending_forced_fetch.add(fetch_type)

    def request_forced_zone_fetch(self) -> None:
        """Force the next poll to fetch zone states even inside the HomeKit skip window."""
        self._pending_force_zone_fetch = True

    def consume_pending_flags(self) -> tuple[bool, bool, set[str]]:
        """Atomically read and reset the pending zone-only / force-fetch / forced-type flags.

        Called once per poll so the coordinator knows whether the upcoming
        fetch was triggered by a write (zone data only) or a general refresh,
        and which slow-data types to force past their floor; resets so the
        next poll starts clean.
        """
        zone_only = self._pending_zone_only
        force_zone_fetch = self._pending_force_zone_fetch
        forced_fetch = self._pending_forced_fetch
        self._pending_zone_only = False
        self._pending_force_zone_fetch = False
        self._pending_forced_fetch = set()
        return zone_only, force_zone_fetch, forced_fetch

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
        """Return (can_refresh, reason); fails open when quota info is missing."""
        rl_info = await self._get_rate_limit_info()

        if not rl_info:
            return True, "no_rate_limit_data"

        remaining = rl_info.get("remaining")
        limit = rl_info.get("limit")
        status = rl_info.get("status")

        if status == "rate_limited" or remaining == 0:
            return False, "rate_limited"

        if limit and remaining is not None:
            percentage_remaining = remaining / limit
            percentage_used = 1 - percentage_remaining

            if percentage_remaining < MIN_QUOTA_PERCENTAGE_FOR_REFRESH:
                return False, f"quota_too_low ({int(percentage_remaining * 100)}% remaining)"

            if percentage_used >= QUOTA_CRITICAL_THRESHOLD:
                return False, f"quota_critical ({int(percentage_used * 100)}% used)"

            if percentage_used >= QUOTA_WARNING_THRESHOLD:
                _LOGGER.warning(
                    "Refresh: Tado API quota at %d%% used (%s of %s calls "
                    "remaining), immediate refreshes may start being skipped",
                    int(percentage_used * 100), remaining, limit,
                )

        return True, "ok"

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
        """Wait out the debounce, then run the refresh through the coordinator."""
        try:
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
                    return

            _LOGGER.debug(
                "Refresh: running debounced refresh (triggered by %s)", reason,
            )

            try:
                await self._coordinator.async_request_refresh()
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
        finally:
            self._debounce_task = None

    async def trigger_refresh(
        self,
        entity_id: str,
        reason: str = "state_change",
        force: bool = False,
        skip_debounce: bool = False,
        zone_only: bool = False,
    ) -> None:
        """Schedule a debounced coordinator refresh after a state-changing action.

        `force=True` bypasses the entity-domain filter (used by buttons
        like "Resume All Schedules"). `skip_debounce=True` runs
        immediately, useful when the user expects an instant response.
        `zone_only=True` narrows the next fetch to zone data,
        skipping weather / mobile / insights work.
        """
        if not force and not self.should_refresh(entity_id):
            _LOGGER.debug(
                "Refresh: %s is not in a domain that triggers immediate refresh",
                entity_id,
            )
            return

        can_refresh, quota_reason = await self._check_quota_available()
        if not can_refresh:
            _LOGGER.debug(
                "Refresh: skipping immediate refresh for %s (%s), "
                "next normal poll will catch the change",
                entity_id, quota_reason,
            )
            return

        _LOGGER.debug(
            "Refresh: queued debounced refresh for %s (reason=%s)",
            entity_id, reason,
        )

        if self._debounce_task is not None:
            self._debounce_task.cancel()
            self._debounce_task = None

        self._pending_refresh = True
        self._pending_zone_only = zone_only
        self._pending_force_zone_fetch = True

        self._debounce_task = asyncio.create_task(
            self._execute_debounced_refresh(reason, skip_debounce),
        )
