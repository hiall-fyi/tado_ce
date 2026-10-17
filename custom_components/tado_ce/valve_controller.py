"""Smart Valve Controller: per-zone proportional-offset TRV control via external sensor; writes via HomeKit (preferred) or cloud."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Final

import aiohttp

from .const import (
    ABSOLUTE_MAX_VALVE_TARGET,
    HOMEKIT_WRITE_GRACE_SECONDS,
    SMART_VALVE_CLOUD_RATE_LIMIT,
    SMART_VALVE_DEBOUNCE_WINDOW,
    SMART_VALVE_HYSTERESIS,
    SMART_VALVE_MIN_CHANGE,
    SVC_RESUME_RETRY_MAX_SECONDS,
    SVC_RESUME_RETRY_SECONDS,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

from .climate_helpers import SensorProxy, subscribe_external_sensors
from .error_dispatch import handle_background_write_error
from .exceptions import TadoAuthError, TadoRateLimitError
from .helpers import get_zone_overlay_termination, is_reading_withheld, mask_serial
from .homekit_provider import HOMEKIT_UNKNOWN_OUTCOME_ERRORS

_LOGGER = logging.getLogger(__name__)

# Doublings after which the cap always wins, so the exponent never overflows a float.
_RESUME_DOUBLINGS_TO_CAP: Final = math.ceil(
    math.log2(SVC_RESUME_RETRY_MAX_SECONDS / SVC_RESUME_RETRY_SECONDS),
)


def _same_temperature(a: float, b: float) -> bool:
    """Whether two temperatures differ by less than 0.1 °C, rounded to 0.01 °C so float noise cannot tip a 0.1 step."""
    return round(abs(a - b), 2) < 0.1


class ControllerState(StrEnum):
    """Smart valve controller states."""

    IDLE = "idle"
    ACTIVE = "active"
    BACKED_OFF = "backed_off"


@dataclass
class ValveControllerRuntime:
    """In-memory runtime state for one zone's smart-valve controller."""

    state: ControllerState = ControllerState.IDLE
    last_valve_target: float | None = None
    last_evaluation_ts: float | None = None
    last_schedule_target: float | None = None
    last_cloud_write_ts: float | None = None
    overlay_set_by_controller: bool = False
    # Ownership flag value last handed to storage; a change is saved at once. Not persisted.
    persisted_ownership: bool = False
    last_resume_attempt_ts: float | None = None
    resume_failures: int = 0
    backed_off_overlay_target: float | None = None
    desired_target: float | None = None
    # Zone-fetch epoch in effect at SVC's last overlay write, delete or adopted service write; older zone data cannot show the zone since. Not persisted.
    zone_fetch_at_own_change: datetime | None = None
    # Target of a write that may or may not have landed; counted as ours while the flag is set, until an accepted or adopted write replaces it.
    unconfirmed_write_target: float | None = None
    unsub_external_sensors: list[CALLBACK_TYPE] = field(default_factory=list)
    # Switched off but a hand-back is still owed; only the retry runs. Not persisted.
    retiring: bool = False
    # Set by _stop() (deactivate or discard) so late sensor callbacks and queued debounces do nothing.
    deactivated: bool = False
    # Set by discard(): another controller or room now owns the zone id, so nothing may be saved for it.
    discarded: bool = False



class SmartValveController:
    """Per-zone proportional-offset controller for TRV valve management.

    Calculates a proportional offset from the gap between the user's
    desired room temperature and the external sensor reading, then
    writes the adjusted target to the TRV via HomeKit or cloud API.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        *,
        hysteresis: float = SMART_VALVE_HYSTERESIS,
        min_change: float = SMART_VALVE_MIN_CHANGE,
        cloud_rate_limit_seconds: float = SMART_VALVE_CLOUD_RATE_LIMIT,
    ) -> None:
        """Initialise the controller for one zone."""
        self._hass = hass
        self._coordinator = coordinator
        self._zone_id = zone_id
        self._hysteresis = hysteresis
        self._min_change = min_change
        self._cloud_rate_limit_seconds = cloud_rate_limit_seconds

        self._zcm = coordinator.zone_config_manager
        self._homekit_provider = coordinator.homekit_provider
        self._api_client = coordinator.api_client
        self._action_debouncer = coordinator.action_debouncer

        self._runtime = ValveControllerRuntime()
        # The no-target bail runs on every evaluation, so its warning is
        # latched and cleared once a target is readable again.
        self._no_target_warned = False
        # Warned once while the zone has no data; cleared when data returns.
        self._no_zone_data_warned = False
        # Logged once while the zone waits for the first real zone update after a restart.
        self._awaiting_fetch_logged = False
        self._unreadable_overlay_logged = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def zone_id(self) -> str:
        """Return the zone ID this controller manages."""
        return self._zone_id

    @property
    def state(self) -> ControllerState:
        """Return the current controller state."""
        return self._runtime.state

    @property
    def last_valve_target(self) -> float | None:
        """Return the last valve target written to the TRV."""
        return self._runtime.last_valve_target

    @property
    def retiring(self) -> bool:
        """Return True while this switched-off controller still owes a hand-back."""
        return self._runtime.retiring

    # ------------------------------------------------------------------
    # Core pure functions
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_valve_target(
        trv_reading: float,
        desired_target: float,
        external_temp: float,
        min_temp: float,
        max_temp: float,
    ) -> float:
        """Calculate the clamped valve target from a proportional offset.

        Pure function. Applies the absolute safety cap after min/max
        clamping so user-configured maxima can't push past
        ABSOLUTE_MAX_VALVE_TARGET.
        """
        offset = desired_target - external_temp
        raw = trv_reading + offset
        clamped = max(min_temp, min(raw, max_temp))
        return round(min(clamped, ABSOLUTE_MAX_VALVE_TARGET), 1)

    def should_write(self, new_valve_target: float) -> bool:
        """Return True when the new target differs enough to warrant a write."""
        last = self._runtime.last_valve_target
        if last is None:
            return True
        return abs(new_valve_target - last) >= self._min_change

    def should_transition(
        self,
        external_temp: float,
        desired_target: float,
    ) -> ControllerState | None:
        """Return the next state if hysteresis crossed, else None."""
        current = self._runtime.state

        if current == ControllerState.IDLE:
            if external_temp < desired_target - self._hysteresis:
                return ControllerState.ACTIVE
        elif current == ControllerState.ACTIVE:
            if external_temp >= desired_target + self._hysteresis:
                return ControllerState.IDLE

        return None

    # ------------------------------------------------------------------
    # Manual override / schedule block detection
    # ------------------------------------------------------------------

    def _note_own_change(self) -> None:
        """Record the zone-fetch epoch in effect now, after SVC's own overlay call or adopted service write returned."""
        self._runtime.zone_fetch_at_own_change = self._coordinator.zone_fetch_epoch_for_write()

    def _zone_data_newer_than_own_change(self) -> bool:
        """Whether the zone data comes from a fetch that started after SVC's own last change."""
        recorded = self._runtime.zone_fetch_at_own_change
        current = self._coordinator.last_zone_fetch_ts()
        # No own change recorded: any real fetch in this process counts, a saved snapshot does not.
        if recorded is None:
            return current is not None
        return current is not None and current > recorded

    def detect_manual_override(self, zone_data: dict[str, Any]) -> bool:
        """Return True when zone data newer than our last change shows our overlay gone, unreadable or replaced."""
        if not self._runtime.overlay_set_by_controller:
            return False

        # Overlay data older than our own last change cannot show anything we did not set.
        if not self._zone_data_newer_than_own_change():
            return False

        # Right after our own write the zone data may still lag behind it.
        last_ts = self._runtime.last_evaluation_ts
        if last_ts is not None:
            if time.monotonic() - last_ts < HOMEKIT_WRITE_GRACE_SECONDS:
                return False

        overlay = zone_data.get("overlay")
        if overlay is None:
            return self._runtime.unconfirmed_write_target is None

        overlay_temp = self._overlay_temp(overlay)
        if overlay_temp is None:
            return True

        if not self._our_targets():
            return False

        return not self._is_own_leftover_overlay(overlay_temp)

    def detect_schedule_block_change(
        self, current_schedule_target: float | None,
    ) -> bool:
        """Return True when the schedule block changed (ON↔OFF or target shift)."""
        last = self._runtime.last_schedule_target
        if (current_schedule_target is None) != (last is None):
            return True
        if current_schedule_target is None:
            return False
        return not _same_temperature(current_schedule_target, last)  # type: ignore[arg-type]

    def detect_overlay_change_while_backed_off(
        self, zone_data: dict[str, Any],
    ) -> bool:
        """Return True when the overlay was edited while backed off.

        Catches the case where every schedule block is OFF (schedule
        target stays None) and the user or an automation sets a new
        overlay. Without this, backed-off state would be permanent.
        """
        overlay = zone_data.get("overlay")

        if overlay is None:
            return self._runtime.backed_off_overlay_target is not None

        overlay_temp = self._overlay_temp(overlay)
        if overlay_temp is None:
            return True

        saved = self._runtime.backed_off_overlay_target
        if saved is None:
            return True

        return not _same_temperature(overlay_temp, saved)

    def _enter_idle(self) -> None:
        """Enter IDLE and clear the desired target: the universal "stop managing" invariant.

        IDLE means SVC is not actively driving the valve, so it holds no desired
        target. These two always move together: every terminating transition into
        IDLE clears desired_target so a later evaluation re-captures a fresh one
        instead of compensating toward a value the controller no longer owns.
        The only IDLE entry that does NOT use this helper is the trusted-write
        re-arm (on_trusted_target_write), which enters IDLE *with* a freshly-set
        desired_target it must keep. That distinction is deliberate, not an
        oversight.
        """
        self._runtime.state = ControllerState.IDLE
        self._runtime.desired_target = None

    def on_trusted_target_write(self, target: float, force_override: bool) -> None:
        """Adopt a service-driven target as SVC's own so it is not treated as a manual override."""
        state = self._runtime.state
        if state == ControllerState.BACKED_OFF and not force_override:
            return

        self._runtime.desired_target = target
        self._runtime.last_valve_target = target
        self._runtime.last_schedule_target = target  # anchor schedule detector so
        # the ACTIVE-path detect_schedule_block_change doesn't confuse the trusted
        # target with a schedule change: only a genuine schedule advance past this
        # value will re-arm the detector.
        self._runtime.last_evaluation_ts = time.monotonic()  # arm grace window
        self._runtime.unconfirmed_write_target = None
        self._note_own_change()

        if state == ControllerState.BACKED_OFF:  # force_override is True here
            # Re-arming IDLE entry: enter IDLE but KEEP the desired_target just
            # set above, so deliberately NOT _enter_idle (which would clear it).
            self._runtime.state = ControllerState.IDLE
            self._runtime.backed_off_overlay_target = None

        # A trusted write replaces whatever SVC owned and IDLE does not adopt it.
        if self._runtime.state == ControllerState.IDLE:
            self._runtime.overlay_set_by_controller = False

        # Re-enter evaluation through the shared debounce key so a poll landing
        # at the same moment coalesces instead of double-evaluating.
        self._hass.async_create_task(
            self._action_debouncer.debounce(
                f"svc_{self._zone_id}",
                self.async_evaluate,
                window=SMART_VALVE_DEBOUNCE_WINDOW,
            ),
        )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def _stamp_local_write(self) -> None:
        """Open the write-protection window so the bridge cannot echo the valve target back.

        Only the display merge reads the window; this controller reads the
        cloud overlay, so stamping cannot starve its own next evaluation.
        """
        reconciler = self._coordinator.state_reconciler
        if reconciler is not None:
            reconciler.record_local_write(self._zone_id)

    def _record_own_write(self, valve_target: float) -> None:
        """Record an accepted write: ownership, target, failure count, grace window, write-protection stamp and fetch epoch."""
        self._stamp_local_write()
        self._runtime.last_valve_target = valve_target
        self._runtime.overlay_set_by_controller = True
        self._runtime.resume_failures = 0
        self._runtime.last_evaluation_ts = time.monotonic()
        self._runtime.unconfirmed_write_target = None
        self._note_own_change()

    def _mark_possible_ownership(self, valve_target: float) -> None:
        """Treat a sent write whose outcome is unknown as ours until a later accepted or adopted write replaces it."""
        self._runtime.overlay_set_by_controller = True
        self._runtime.unconfirmed_write_target = valve_target
        self._note_own_change()

    async def _async_write_valve_target(self, valve_target: float) -> bool:
        """Write the valve target via HomeKit, falling back to cloud on failure."""
        if self._homekit_provider is not None:
            try:
                success = await self._homekit_provider.set_temperature(
                    self._zone_id, valve_target, raise_ambiguous=True,
                )
                if success:
                    _LOGGER.debug(
                        "Smart Valve: zone %s set TRV target to %.1f°C via HomeKit",
                        self._zone_id, valve_target,
                    )
                    self._record_own_write(valve_target)
                    return True
            except HOMEKIT_UNKNOWN_OUTCOME_ERRORS:
                _LOGGER.warning(
                    "Smart Valve: zone %s HomeKit write failed, falling "
                    "back to cloud",
                    self._zone_id, exc_info=True,
                )
                # Sent but unanswered: the bridge may have applied it.
                self._mark_possible_ownership(valve_target)

        return await self._async_write_cloud(valve_target)

    async def _async_write_cloud(self, valve_target: float) -> bool:
        """Write the valve target via cloud API, rate-limited per zone."""
        now = time.monotonic()
        last_cloud = self._runtime.last_cloud_write_ts

        if last_cloud is not None and (now - last_cloud) < self._cloud_rate_limit_seconds:
            _LOGGER.debug(
                "Smart Valve: zone %s cloud write rate-limited, will "
                "retry on next evaluation",
                self._zone_id,
            )
            return False

        if self._coordinator.is_cloud_backoff_active():
            _LOGGER.debug(
                "Smart Valve: zone %s cloud write held. Tado quota "
                "backoff active, will retry on next evaluation",
                self._zone_id,
            )
            return False

        if self._coordinator.is_quota_spend_held():
            _LOGGER.debug(
                "Smart Valve: zone %s cloud write held. Tado quota is at "
                "the reserve, will retry on next evaluation",
                self._zone_id,
            )
            return False

        try:
            setting = {"type": "HEATING", "power": "ON", "temperature": {"celsius": valve_target}}
            entry_id = self._coordinator.config_entry.entry_id
            termination = get_zone_overlay_termination(
                self._hass, self._zone_id, entry_id=entry_id,
            )
            success = await self._api_client.set_zone_overlay(
                self._zone_id, setting, termination, raise_network_errors=True,
            )
            if success:
                _LOGGER.debug(
                    "Smart Valve: zone %s set TRV target to %.1f°C via cloud",
                    self._zone_id, valve_target,
                )
                self._record_own_write(valve_target)
                self._runtime.last_cloud_write_ts = now
                return True
            _LOGGER.warning(
                "Smart Valve: zone %s cloud write rejected by Tado, "
                "will retry on next sensor change",
                self._zone_id,
            )
            return False
        except (TadoAuthError, TadoRateLimitError) as e:
            handle_background_write_error(
                e, self._coordinator.config_entry, self._hass, self._coordinator,
                f"Smart Valve: zone {self._zone_id} cloud write failed, "
                "starting recovery; will retry on next sensor change",
            )
            return False
        except (TimeoutError, aiohttp.ClientError):
            _LOGGER.warning(
                "Smart Valve: zone %s cloud write raised an exception, "
                "will retry on next sensor change",
                self._zone_id, exc_info=True,
            )
            # Sent but unanswered: Tado may have applied it.
            self._mark_possible_ownership(valve_target)
            return False

    def _is_hand_back_held(self) -> bool:
        """Whether the quota is at the last few calls, where a hand-back waits for the reset."""
        held: bool = self._coordinator.is_quota_spend_held(essential=True)
        if held:
            _LOGGER.debug(
                "Smart Valve: zone %s hand-back held. Tado quota is at the "
                "last few calls, will retry after the quota resets",
                self._zone_id,
            )
        return held

    async def _async_resume_schedule(self) -> bool:
        """Delete the zone overlay so Tado's schedule resumes (cloud only)."""
        self._action_debouncer.cancel(f"svc_{self._zone_id}")
        # No write of ours is known or suspected to be on the zone, so a delete could only remove someone else's overlay.
        if not self._runtime.overlay_set_by_controller:
            return True
        # A hold is not a failure: nothing is stamped or counted, so the retry fires on the first fetch after the reset.
        if self._is_hand_back_held():
            return False
        self._runtime.last_resume_attempt_ts = time.monotonic()
        try:
            try:
                success = await self._api_client.delete_zone_overlay(self._zone_id)
            finally:
                # Recorded after the call returns, whatever its outcome, so a sync that started mid-call is not read as newer.
                self._note_own_change()
            if success:
                _LOGGER.debug(
                    "Smart Valve: zone %s overlay cleared. Tado schedule resumed",
                    self._zone_id,
                )
                self._runtime.overlay_set_by_controller = False
                # The cleared flag is saved at once, so a crash cannot restore "flag set, no overlay" as a manual change.
                await self.async_persist_state()
                return True
            self._runtime.resume_failures += 1
            _LOGGER.warning(
                "Smart Valve: zone %s could not clear overlay. Tado "
                "schedule did not resume, will retry on a later cycle",
                self._zone_id,
            )
            return False
        except (TadoAuthError, TadoRateLimitError) as e:
            self._runtime.resume_failures += 1
            handle_background_write_error(
                e, self._coordinator.config_entry, self._hass, self._coordinator,
                f"Smart Valve: zone {self._zone_id} clearing overlay failed, "
                "starting recovery; will retry on a later cycle",
            )
            return False
        except (TimeoutError, aiohttp.ClientError):
            self._runtime.resume_failures += 1
            _LOGGER.warning(
                "Smart Valve: zone %s clearing overlay raised an exception, "
                "will retry on a later cycle",
                self._zone_id, exc_info=True,
            )
            return False

    async def _async_hand_back_pending_overlay(self) -> None:
        """Hand back a leftover SVC overlay once, unless newer zone data shows it gone or taken over."""
        zone_data = self._get_zone_data()
        if zone_data is None:
            return
        if zone_data.get("overlay") is None and self._zone_data_newer_than_own_change():
            self._runtime.overlay_set_by_controller = False
        elif (
            not self.detect_manual_override(zone_data)
            and not self._coordinator.is_cloud_backoff_active()
        ):
            await self._async_resume_schedule()

    async def _async_retry_pending_resume(
        self, zone_data: dict[str, Any], current_state: ControllerState,
    ) -> bool:
        """Retry an earlier failed hand-back while IDLE; True when a delete was sent."""
        if current_state != ControllerState.IDLE or not self._runtime.overlay_set_by_controller:
            return False
        if not self._zone_data_newer_than_own_change():
            return False
        if zone_data.get("overlay") is None:
            self._runtime.overlay_set_by_controller = False
            return False
        if self.detect_manual_override(zone_data) or self._coordinator.is_cloud_backoff_active():
            return False
        last_attempt = self._runtime.last_resume_attempt_ts
        doublings = min(max(self._runtime.resume_failures - 1, 0), _RESUME_DOUBLINGS_TO_CAP)
        gap = min(SVC_RESUME_RETRY_SECONDS * 2 ** doublings, SVC_RESUME_RETRY_MAX_SECONDS)
        if last_attempt is not None and time.monotonic() - last_attempt < gap:
            return False
        # A held hand-back sends nothing, so the pass carries on with the data it has.
        if self._is_hand_back_held():
            return False
        await self._async_resume_schedule()
        return True

    # ------------------------------------------------------------------
    # Main evaluation cycle
    # ------------------------------------------------------------------

    async def async_evaluate(self) -> None:
        """Run one evaluation: read inputs, decide state, write if needed."""
        if self._runtime.deactivated:
            if self._runtime.retiring:
                await self._async_retry_retiring()
            return

        from .climate_helpers import read_external_sensor
        from .schedule_helpers import get_current_schedule_target

        zone_data = self._get_zone_data()
        if zone_data is None:
            if self._coordinator.last_zone_fetch_ts() is None:
                if not self._awaiting_fetch_logged:
                    self._awaiting_fetch_logged = True
                    _LOGGER.info(
                        "Smart Valve: zone %s is waiting for the first zone update from Tado "
                        "after the restart before it acts",
                        self._zone_id,
                    )
                return
            if not self._no_zone_data_warned:
                _LOGGER.warning(
                    "Smart Valve: zone %s has no zone data, skipping evaluation "
                    "until it has some",
                    self._zone_id,
                )
                self._no_zone_data_warned = True
            return
        self._no_zone_data_warned = False
        self._awaiting_fetch_logged = False

        zone_config = self._zcm.get_zone_config(self._zone_id)
        min_temp: float = zone_config.get("min_temp", 5.0)
        max_temp: float = zone_config.get("max_temp", 25.0)

        if min_temp > max_temp:
            _LOGGER.warning(
                "Smart Valve: zone %s has invalid temperature limits "
                "(min %.1f°C > max %.1f°C), falling back to defaults "
                "(5.0–25.0°C). Fix this in Zone Configuration → "
                "Temperature Limits.",
                self._zone_id, min_temp, max_temp,
            )
            min_temp, max_temp = 5.0, 25.0

        external_temp = read_external_sensor(
            self._hass, self._zcm, self._zone_id, "external_temp_sensor",
        )

        trv_reading = self._read_trv_temperature(zone_data)

        schedule_target = get_current_schedule_target(
            self._zone_id, data_loader=self._coordinator.data_loader,
        )

        current_state = self._runtime.state

        # Return after an attempt: the rest of this pass would act on zone_data read before the DELETE.
        if await self._async_retry_pending_resume(zone_data, current_state):
            return

        _LOGGER.debug(
            "Smart Valve: zone %s eval, state=%s, ext=%s, trv=%s, "
            "schedule=%s, desired=%s",
            self._zone_id,
            self._runtime.state.value,
            f"{external_temp:.1f}" if external_temp is not None else "None",
            f"{trv_reading:.1f}" if trv_reading is not None else "None",
            f"{schedule_target:.1f}" if schedule_target is not None else "None",
            f"{self._runtime.desired_target:.1f}" if self._runtime.desired_target is not None else "None",
        )

        if current_state == ControllerState.BACKED_OFF:
            if self.detect_schedule_block_change(schedule_target):
                _LOGGER.info(
                    "Smart Valve: zone %s schedule block changed, "
                    "re-enabling Smart Valve Control",
                    self._zone_id,
                )
                self._enter_idle()
                self._runtime.last_schedule_target = schedule_target
                self._runtime.backed_off_overlay_target = None
            elif self.detect_overlay_change_while_backed_off(zone_data):
                _LOGGER.info(
                    "Smart Valve: zone %s manual override cleared, "
                    "re-enabling Smart Valve Control",
                    self._zone_id,
                )
                self._enter_idle()
                self._runtime.backed_off_overlay_target = None
            return

        if current_state == ControllerState.ACTIVE:
            if self.detect_manual_override(zone_data):
                self._runtime.backed_off_overlay_target = self._overlay_temp(
                    zone_data.get("overlay"),
                )
                _LOGGER.info(
                    "Smart Valve: zone %s manual change detected from the "
                    "Tado app or device, pausing Smart Valve Control until "
                    "the schedule changes or the override is cleared",
                    self._zone_id,
                )
                # Drops a write queued by an earlier evaluation so it cannot overwrite the user's change; one already running is not stopped.
                self._action_debouncer.cancel(f"svc_{self._zone_id}")
                self._runtime.state = ControllerState.BACKED_OFF
                self._runtime.last_schedule_target = schedule_target
                return

        if current_state == ControllerState.ACTIVE:
            if schedule_target is None:
                _LOGGER.info(
                    "Smart Valve: zone %s schedule has no heating target, "
                    "handing control back to Tado",
                    self._zone_id,
                )
                await self._async_resume_schedule()
                self._enter_idle()
                self._runtime.last_schedule_target = schedule_target
                return
            if self.detect_schedule_block_change(schedule_target):
                _LOGGER.info(
                    "Smart Valve: zone %s schedule block changed "
                    "(%.1f°C → %.1f°C), adjusting valve target to match",
                    self._zone_id,
                    self._runtime.last_schedule_target
                    if self._runtime.last_schedule_target is not None
                    else 0.0,
                    schedule_target,
                )
                self._runtime.desired_target = schedule_target
                self._runtime.last_schedule_target = schedule_target

        if external_temp is None:
            if current_state == ControllerState.ACTIVE:
                _LOGGER.warning(
                    "Smart Valve: zone %s external sensor unavailable, "
                    "handing control back to Tado",
                    self._zone_id,
                )
                await self._async_resume_schedule()
                self._enter_idle()
            return

        # Hidden room reading and no fresh TRV reading: hand back once and stay idle; returning before should_transition stops it re-activating and handing back every cycle.
        if trv_reading is None and is_reading_withheld(
            self._coordinator.data, self._zone_id, "insideTemperature",
        ):
            if current_state == ControllerState.ACTIVE:
                _LOGGER.info(
                    "Smart Valve: zone %s room temperature has stopped updating because its main device has been offline for four hours or more, handing control back to Tado",
                    self._zone_id,
                )
                await self._async_resume_schedule()
                self._enter_idle()
            return

        desired_target: float | None
        if self._runtime.desired_target is not None:
            desired_target = self._runtime.desired_target
        else:
            desired_target = self._read_desired_target(zone_data)

        if desired_target is None:
            if not self._no_target_warned:
                _LOGGER.warning(
                    "Smart Valve: zone %s has no target to work from yet, so "
                    "it stays idle: no manual override is active and this "
                    "zone's schedule hasn't been fetched. It fetches "
                    "automatically on the next full sync once the quota "
                    "allows.",
                    self._zone_id,
                )
                self._no_target_warned = True
            return
        self._no_target_warned = False

        new_state = self.should_transition(external_temp, desired_target)
        # should_transition only ever returns IDLE→ACTIVE or ACTIVE→IDLE; both
        # are handled explicitly below, so there is no other transition to cover.
        if new_state == ControllerState.ACTIVE and current_state == ControllerState.IDLE:
            self._runtime.desired_target = desired_target
            self._runtime.state = new_state
        elif new_state == ControllerState.IDLE and current_state == ControllerState.ACTIVE:
            _LOGGER.info(
                "Smart Valve: zone %s reached target (external %.1f°C "
                "≥ desired %.1f°C + %.1f°C hysteresis), handing control "
                "back to Tado",
                self._zone_id, external_temp, desired_target, self._hysteresis,
            )
            await self._async_resume_schedule()
            self._enter_idle()

        if self._runtime.state == ControllerState.ACTIVE:
            if trv_reading is None:
                # Without a TRV reading we cannot calculate a precise
                # offset, so we open the valve fully as a safe fallback
                # until the next poll brings a reading back.
                _LOGGER.warning(
                    "Smart Valve: zone %s TRV temperature unreadable, "
                    "opening valve fully (target %.1f°C) until the sensor returns",
                    self._zone_id, max_temp,
                )
                if self.should_write(max_temp):
                    await self._async_debounced_write(max_temp)
                return

            active_desired = self._runtime.desired_target
            if active_desired is None:
                return

            valve_target = self.calculate_valve_target(
                trv_reading, active_desired, external_temp, min_temp, max_temp,
            )

            if self.should_write(valve_target):
                await self._async_debounced_write(valve_target)

    async def _async_debounced_write(self, valve_target: float) -> None:
        """Write the valve target through the action debouncer."""
        async def _do_write() -> None:
            await self._async_write_valve_target(valve_target)
            await self.async_persist_state()

        await self._action_debouncer.debounce(
            f"svc_{self._zone_id}",
            _do_write,
            window=SMART_VALVE_DEBOUNCE_WINDOW,
        )

    # ------------------------------------------------------------------
    # Input readers
    # ------------------------------------------------------------------

    def _get_zone_data(self) -> dict[str, Any] | None:
        """Zone data a decision may use: None until a real zone fetch, then merged with the freshest HomeKit reading."""
        from .helpers import get_zone_state_for_control, merge_homekit_into_zone_data

        # The HomeKit merge lets _read_trv_temperature() see real-time pushes instead of the last cloud poll.
        zone_data = get_zone_state_for_control(self._coordinator, self._zone_id)
        if zone_data is None:
            return None
        return merge_homekit_into_zone_data(
            zone_data, self._zone_id, self._coordinator,
        )

    @staticmethod
    def _read_trv_temperature(zone_data: dict[str, Any]) -> float | None:
        """Return the TRV's built-in sensor reading, or None if unreadable."""
        try:
            return float(
                zone_data["sensorDataPoints"]["insideTemperature"]["celsius"],
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _overlay_temp(self, overlay: Any) -> float | None:
        """Return the overlay's celsius target, or None if absent, malformed or not a finite number."""
        if overlay is None:
            return None
        try:
            value = float(overlay["setting"]["temperature"]["celsius"])
        except (KeyError, TypeError, ValueError, OverflowError):
            value = math.nan
        if math.isfinite(value):
            self._unreadable_overlay_logged = False
            return value
        if not self._unreadable_overlay_logged:
            _LOGGER.debug(
                "Smart Valve: zone %s overlay temperature is missing or malformed, ignoring it",
                self._zone_id,
            )
            self._unreadable_overlay_logged = True
        return None

    def _our_targets(self) -> list[float]:
        """Every target SVC sent or tried to send that may still be on the zone."""
        known = (self._runtime.last_valve_target, self._runtime.unconfirmed_write_target)
        return [t for t in known if t is not None]

    def _is_own_leftover_overlay(self, overlay_temp: float) -> bool:
        """Whether this overlay is at a temperature SVC sent or tried to send and has not yet handed back."""
        if not self._runtime.overlay_set_by_controller:
            return False
        return any(_same_temperature(overlay_temp, t) for t in self._our_targets())

    def _read_desired_target(self, zone_data: dict[str, Any]) -> float | None:
        """Return the desired target: active overlay first, then schedule."""
        from .schedule_helpers import get_current_schedule_target

        overlay_temp = self._overlay_temp(zone_data.get("overlay"))
        if (
            overlay_temp is not None
            and self._zone_data_newer_than_own_change()
            and not self._is_own_leftover_overlay(overlay_temp)
        ):
            return overlay_temp

        return get_current_schedule_target(
            self._zone_id, data_loader=self._coordinator.data_loader,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_activate(self) -> None:
        """Load persisted state, subscribe to sensors, and evaluate if active."""
        await self.async_load_state()

        # Restored as ACTIVE without a desired_target means the persisted
        # state predates the desired_target field. Reset to IDLE so the
        # next evaluation captures a fresh desired_target instead of
        # reading our own inflated overlay back as the user's intent.
        if self._runtime.state == ControllerState.ACTIVE and self._runtime.desired_target is None:
            _LOGGER.debug(
                "Smart Valve: zone %s restored ACTIVE without desired_target, "
                "resetting to IDLE",
                self._zone_id,
            )
            self._enter_idle()

        # Clean up an overlay left behind by a previous crash or HA restart.
        if self._runtime.overlay_set_by_controller and self._runtime.state == ControllerState.IDLE:
            _LOGGER.debug(
                "Smart Valve: zone %s clearing stale overlay left over "
                "from a previous session",
                self._zone_id,
            )
            await self._async_hand_back_pending_overlay()

        await self._async_check_device_offset()

        proxy = SensorProxy(self._hass, self._coordinator)
        unsubs = subscribe_external_sensors(
            proxy,
            self._zone_id,
            self._on_external_sensor_change,
            include_humidity=False,
        )
        self._runtime.unsub_external_sensors = unsubs

        if self._runtime.state == ControllerState.ACTIVE:
            await self.async_evaluate()

        _LOGGER.info(
            "Smart Valve: zone %s controller activated (state=%s)",
            self._zone_id, self._runtime.state,
        )

    def _stop(self) -> None:
        """Stop reacting: flag first so a late sensor event short-circuits, cancel before unsub so a pending debounce also sees it."""
        self._runtime.deactivated = True
        self._action_debouncer.cancel(f"svc_{self._zone_id}")

        for unsub in self._runtime.unsub_external_sensors:
            unsub()
        self._runtime.unsub_external_sensors.clear()

    def _is_hand_back_owed(self) -> bool:
        """Whether SVC's own override may still be on the zone and the user has not taken the zone over."""
        return self._runtime.overlay_set_by_controller and self._runtime.state != ControllerState.BACKED_OFF

    async def async_deactivate(self) -> bool:
        """Hand back a pending override, stop and persist; True when the hand-back is still owed."""
        # A user's overlay on a backed-off zone must never be deleted, even on data older than our write.
        if self._runtime.state != ControllerState.BACKED_OFF:
            await self._async_hand_back_pending_overlay()

        owed = self._is_hand_back_owed()
        if owed:
            # IDLE with the flag set is the state the startup and retry hand-backs act on.
            self._enter_idle()
        self._stop()
        self._runtime.retiring = owed
        await self.async_persist_state()

        _LOGGER.info(
            "Smart Valve: zone %s controller deactivated", self._zone_id,
        )
        if owed:
            _LOGGER.warning(
                "Smart Valve: zone %s is switched off, but Tado has not taken back the last "
                "valve setting yet. Tado CE will try again after later zone updates, until "
                "Tado accepts it or something else changes the zone",
                self._zone_id,
            )
        return owed

    async def _async_retry_retiring(self) -> None:
        """Retry the owed hand-back on a switched-off zone and finish once it is settled."""
        zone_data = self._get_zone_data()
        if zone_data is None:
            return
        # Someone else's setting, or none, on newer data: nothing of ours is left to hand back.
        someone_else = self.detect_manual_override(zone_data)
        if someone_else:
            self._runtime.overlay_set_by_controller = False
        else:
            await self._async_retry_pending_resume(zone_data, self._runtime.state)
        if self._runtime.overlay_set_by_controller:
            return
        self._runtime.retiring = False
        await self.async_persist_state()
        if someone_else and zone_data.get("overlay") is not None:
            _LOGGER.info(
                "Smart Valve: zone %s has someone else's temperature setting on it now, "
                "leaving it and no longer retrying",
                self._zone_id,
            )
        else:
            _LOGGER.info(
                "Smart Valve: zone %s handed back to Tado, no longer retrying",
                self._zone_id,
            )

    async def async_restore_retiring(self) -> bool:
        """Rebuild a switched-off controller from saved state and make one attempt; True when it is still owed."""
        await self.async_load_state()
        if not self._is_hand_back_owed():
            return False
        self._enter_idle()
        self._stop()
        self._runtime.retiring = True
        await self._async_retry_retiring()
        return self._runtime.retiring

    def adopt_predecessor(self, previous: SmartValveController) -> None:
        """Take over the zone-fetch epoch of the controller this one replaces, so older zone data stays untrusted."""
        self._runtime.zone_fetch_at_own_change = previous._runtime.zone_fetch_at_own_change
        _LOGGER.debug(
            "Smart Valve: zone %s controller took over the previous controller's last change time",
            self._zone_id,
        )

    def discard(self) -> None:
        """Stop without handing back or saving, for a controller that failed to start, lost its zone id, or was replaced by a new one."""
        self._stop()
        self._runtime.discarded = True
        self._runtime.retiring = False
        _LOGGER.info(
            "Smart Valve: zone %s controller discarded without a hand-back",
            self._zone_id,
        )

    async def _async_check_device_offset(self) -> None:
        """Warn the user when a non-zero device offset would double-compensate.

        Tado returns offset-adjusted temperatures in
        sensorDataPoints.insideTemperature. Smart Valve Control reads
        that adjusted value and applies its own compensation on top, so
        any non-zero device offset produces overshoot.
        """
        from .helpers import get_zone_state

        try:
            # Advisory only, so it may read the saved snapshot a control decision may not.
            zone_data = get_zone_state(self._coordinator.data, self._zone_id)
            if zone_data is None:
                return

            devices = zone_data.get("devices", [])
            if not devices:
                return

            for device in devices:
                serial = device.get("serialNo", "")
                offset = device.get("temperatureOffset", {}).get("celsius")
                if offset is not None and not _same_temperature(float(offset), 0.0):
                    _LOGGER.warning(
                        "Smart Valve: zone %s device %s has a temperature "
                        "offset of %.1f°C set in Tado, this will cause "
                        "double compensation with Smart Valve Control. "
                        "Reset the device offset to 0 in the Tado app for "
                        "accurate results.",
                        self._zone_id, mask_serial(serial), float(offset),
                    )
        except (KeyError, TypeError, AttributeError):
            _LOGGER.debug(
                "Smart Valve: zone %s could not check device offset, "
                "continuing without the warning",
                self._zone_id, exc_info=True,
            )

    def _on_external_sensor_change(self, _event: Any) -> None:
        """Debounce an external sensor change into a fresh evaluation."""
        if self._runtime.deactivated:
            return
        self._hass.async_create_task(
            self._action_debouncer.debounce(
                f"svc_{self._zone_id}",
                self.async_evaluate,
                window=SMART_VALVE_DEBOUNCE_WINDOW,
            ),
        )

    def get_attributes(self) -> dict[str, Any]:
        """Return attributes for the climate entity's extra_state_attributes."""
        state = self._runtime.state
        # A restored ACTIVE state does nothing until a real zone fetch, so it is not reported as active.
        acting = state == ControllerState.ACTIVE and self._coordinator.last_zone_fetch_ts() is not None
        attrs: dict[str, Any] = {
            "valve_control_enabled": True,
            "valve_control_active": acting,
        }
        if state == ControllerState.BACKED_OFF:
            attrs["valve_control_backed_off"] = True
        if acting and self._runtime.last_valve_target is not None:
            attrs["valve_target"] = self._runtime.last_valve_target
        if acting and self._runtime.desired_target is not None:
            attrs["desired_target"] = self._runtime.desired_target
        return attrs

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def async_persist_state(self) -> None:
        """Persist controller state via ZoneConfigManager.

        last_evaluation_ts is deliberately omitted: it's a monotonic
        clock value, meaningless across process restarts.
        """
        if self._runtime.discarded:
            return
        state_dict = {
            "state": self._runtime.state.value,
            "last_valve_target": self._runtime.last_valve_target,
            "last_schedule_target": self._runtime.last_schedule_target,
            "overlay_set_by_controller": self._runtime.overlay_set_by_controller,
            "backed_off_overlay_target": self._runtime.backed_off_overlay_target,
            "desired_target": self._runtime.desired_target,
            # Saved only while the flag is set, so a settled record never carries a stale target.
            "unconfirmed_write_target": (
                self._runtime.unconfirmed_write_target if self._runtime.overlay_set_by_controller else None
            ),
        }
        # An ownership change is saved at once; other fields use the delayed save.
        flag = self._runtime.overlay_set_by_controller
        await self._zcm.async_set_zone_value(
            self._zone_id, "svc_state", state_dict,
            immediate=flag != self._runtime.persisted_ownership,
        )
        self._runtime.persisted_ownership = flag

    async def async_load_state(self) -> None:
        """Restore persisted controller state on startup."""
        config = self._zcm.get_zone_config(self._zone_id)
        raw = config.get("svc_state")
        if not isinstance(raw, dict):
            _LOGGER.debug(
                "Smart Valve: zone %s has no persisted state, starting fresh",
                self._zone_id,
            )
            return

        def _optional_float(key: str) -> float | None:
            # Raises on malformed scalars (e.g. strings) so the
            # except-fallback below fires instead of leaking a bad value.
            value = raw.get(key)
            return float(value) if value is not None else None

        try:
            state_str = raw.get("state", "idle")
            self._runtime.state = ControllerState(state_str)
            self._runtime.last_valve_target = _optional_float("last_valve_target")
            # Don't restore last_evaluation_ts: monotonic resets on
            # boot, so a persisted value would suppress a legitimate
            # post-restart manual-override detection.
            self._runtime.last_evaluation_ts = None
            self._runtime.last_schedule_target = _optional_float("last_schedule_target")
            self._runtime.overlay_set_by_controller = bool(
                raw.get("overlay_set_by_controller", False),
            )
            self._runtime.persisted_ownership = self._runtime.overlay_set_by_controller
            self._runtime.backed_off_overlay_target = _optional_float("backed_off_overlay_target")
            self._runtime.desired_target = _optional_float("desired_target")
            unconfirmed = _optional_float("unconfirmed_write_target")
            self._runtime.unconfirmed_write_target = (
                unconfirmed if self._runtime.overlay_set_by_controller else None
            )
            _LOGGER.debug(
                "Smart Valve: zone %s restored state=%s, last target=%s",
                self._zone_id, self._runtime.state, self._runtime.last_valve_target,
            )
        except (ValueError, TypeError, KeyError):
            _LOGGER.warning(
                "Smart Valve: zone %s persisted state was unreadable, "
                "starting fresh",
                self._zone_id,
            )
            self._runtime = ValveControllerRuntime()
