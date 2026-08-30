"""Smart Valve Controller: per-zone proportional-offset TRV control via external sensor; writes via HomeKit (preferred) or cloud."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import (
    ABSOLUTE_MAX_VALVE_TARGET,
    HOMEKIT_WRITE_GRACE_SECONDS,
    SMART_VALVE_CLOUD_RATE_LIMIT,
    SMART_VALVE_DEBOUNCE_WINDOW,
    SMART_VALVE_HYSTERESIS,
    SMART_VALVE_MIN_CHANGE,
)

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

from .climate_helpers import SensorProxy, subscribe_external_sensors
from .error_dispatch import handle_background_write_error
from .exceptions import TadoAuthError, TadoRateLimitError
from .helpers import get_zone_overlay_termination, mask_serial

_LOGGER = logging.getLogger(__name__)


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
    pending_cloud_target: float | None = None
    overlay_set_by_controller: bool = False
    backed_off_overlay_target: float | None = None
    desired_target: float | None = None
    unsub_external_sensors: list[CALLBACK_TYPE] = field(default_factory=list)
    # Set in async_deactivate before unsub/cancel so in-flight sensor
    # callbacks and scheduled debounce timers short-circuit instead of
    # writing to a deactivated controller.
    deactivated: bool = False



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

    def detect_manual_override(self, zone_data: dict[str, Any]) -> bool:
        """Return True when the overlay was changed by something other than us.

        Suppressed within HOMEKIT_WRITE_GRACE_SECONDS of our own write
        to avoid false positives during the HomeKit → cloud sync delay.
        """
        if not self._runtime.overlay_set_by_controller:
            return False

        last_ts = self._runtime.last_evaluation_ts
        if last_ts is not None:
            if time.monotonic() - last_ts < HOMEKIT_WRITE_GRACE_SECONDS:
                return False

        overlay = zone_data.get("overlay")
        if overlay is None:
            return True

        overlay_temp = (
            overlay.get("setting", {})
            .get("temperature") or {}
        ).get("celsius")
        if overlay_temp is None:
            return True

        last = self._runtime.last_valve_target
        if last is None:
            return False

        return abs(float(overlay_temp) - last) >= 0.1

    def detect_schedule_block_change(
        self, current_schedule_target: float | None,
    ) -> bool:
        """Return True when the schedule block changed (ON↔OFF or target shift)."""
        last = self._runtime.last_schedule_target
        if (current_schedule_target is None) != (last is None):
            return True
        if current_schedule_target is None:
            return False
        return abs(current_schedule_target - last) >= 0.1  # type: ignore[operator]

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

        overlay_temp = (
            overlay.get("setting", {})
            .get("temperature") or {}
        ).get("celsius")
        if overlay_temp is None:
            return True

        saved = self._runtime.backed_off_overlay_target
        if saved is None:
            return True

        return abs(float(overlay_temp) - saved) >= 0.1

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

        if state == ControllerState.BACKED_OFF:  # force_override is True here
            # Re-arming IDLE entry: enter IDLE but KEEP the desired_target just
            # set above, so deliberately NOT _enter_idle (which would clear it).
            self._runtime.state = ControllerState.IDLE
            self._runtime.backed_off_overlay_target = None

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

    async def _async_write_valve_target(self, valve_target: float) -> bool:
        """Write the valve target via HomeKit, falling back to cloud on failure."""
        if self._homekit_provider is not None:
            try:
                success = await self._homekit_provider.set_temperature(
                    self._zone_id, valve_target,
                )
                if success:
                    _LOGGER.debug(
                        "Smart Valve: zone %s set TRV target to %.1f°C via HomeKit",
                        self._zone_id, valve_target,
                    )
                    self._stamp_local_write()
                    self._runtime.last_valve_target = valve_target
                    self._runtime.overlay_set_by_controller = True
                    self._runtime.last_evaluation_ts = time.monotonic()
                    return True
            except (TimeoutError, aiohttp.ClientError, OSError):
                _LOGGER.warning(
                    "Smart Valve: zone %s HomeKit write failed, falling "
                    "back to cloud",
                    self._zone_id, exc_info=True,
                )

        return await self._async_write_cloud(valve_target)

    async def _async_write_cloud(self, valve_target: float) -> bool:
        """Write the valve target via cloud API, rate-limited per zone."""
        now = time.monotonic()
        last_cloud = self._runtime.last_cloud_write_ts

        if last_cloud is not None and (now - last_cloud) < self._cloud_rate_limit_seconds:
            self._runtime.pending_cloud_target = valve_target
            _LOGGER.debug(
                "Smart Valve: zone %s cloud write rate-limited, queued "
                "target %.1f°C for next window",
                self._zone_id, valve_target,
            )
            return False

        if self._coordinator.is_cloud_backoff_active():
            self._runtime.pending_cloud_target = valve_target
            _LOGGER.debug(
                "Smart Valve: zone %s cloud write held. Tado quota "
                "backoff active, queued target %.1f°C",
                self._zone_id, valve_target,
            )
            return False

        try:
            setting = {"type": "HEATING", "power": "ON", "temperature": {"celsius": valve_target}}
            entry_id = self._coordinator.config_entry.entry_id
            termination = get_zone_overlay_termination(
                self._hass, self._zone_id, entry_id=entry_id,
            )
            success = await self._api_client.set_zone_overlay(
                self._zone_id, setting, termination,
            )
            if success:
                _LOGGER.debug(
                    "Smart Valve: zone %s set TRV target to %.1f°C via cloud",
                    self._zone_id, valve_target,
                )
                self._stamp_local_write()
                self._runtime.last_valve_target = valve_target
                self._runtime.last_cloud_write_ts = now
                self._runtime.overlay_set_by_controller = True
                self._runtime.pending_cloud_target = None
                self._runtime.last_evaluation_ts = time.monotonic()
                return True
            _LOGGER.warning(
                "Smart Valve: zone %s cloud write rejected by Tado, "
                "will retry on next sensor change",
                self._zone_id,
            )
            return False
        except (TadoAuthError, TadoRateLimitError) as e:
            self._runtime.pending_cloud_target = valve_target
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
            return False

    async def _async_resume_schedule(self) -> bool:
        """Delete the zone overlay so Tado's schedule resumes (cloud only)."""
        try:
            success = await self._api_client.delete_zone_overlay(self._zone_id)
            if success:
                _LOGGER.debug(
                    "Smart Valve: zone %s overlay cleared. Tado schedule resumed",
                    self._zone_id,
                )
                self._runtime.overlay_set_by_controller = False
                return True
            _LOGGER.warning(
                "Smart Valve: zone %s could not clear overlay. Tado "
                "schedule did not resume, will retry next cycle",
                self._zone_id,
            )
            return False
        except (TadoAuthError, TadoRateLimitError) as e:
            handle_background_write_error(
                e, self._coordinator.config_entry, self._hass, self._coordinator,
                f"Smart Valve: zone {self._zone_id} clearing overlay failed, "
                "starting recovery; will retry next cycle",
            )
            return False
        except (TimeoutError, aiohttp.ClientError):
            _LOGGER.warning(
                "Smart Valve: zone %s clearing overlay raised an exception, "
                "will retry next cycle",
                self._zone_id, exc_info=True,
            )
            return False

    # ------------------------------------------------------------------
    # Main evaluation cycle
    # ------------------------------------------------------------------

    async def async_evaluate(self) -> None:
        """Run one evaluation: read inputs, decide state, write if needed."""
        if self._runtime.deactivated:
            return

        from .climate_helpers import read_external_sensor
        from .schedule_helpers import get_current_schedule_target

        zone_data = self._get_zone_data()
        if zone_data is None:
            _LOGGER.warning(
                "Smart Valve: zone %s has no zone data, skipping evaluation, "
                "will retry on next poll",
                self._zone_id,
            )
            return

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
                overlay = zone_data.get("overlay")
                if overlay is not None:
                    self._runtime.backed_off_overlay_target = (
                        overlay.get("setting", {})
                        .get("temperature") or {}
                    ).get("celsius")
                else:
                    self._runtime.backed_off_overlay_target = None
                _LOGGER.info(
                    "Smart Valve: zone %s manual change detected from the "
                    "Tado app or device, pausing Smart Valve Control until "
                    "the schedule changes or the override is cleared",
                    self._zone_id,
                )
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

        desired_target: float | None
        if self._runtime.desired_target is not None:
            desired_target = self._runtime.desired_target
        else:
            desired_target = self._read_desired_target(zone_data)

        if desired_target is None:
            if not self._no_target_warned:
                _LOGGER.warning(
                    "Smart Valve: zone %s has no target to work from, so it "
                    "stays idle: no manual override is active and no schedule "
                    "has been fetched for this zone. Turn on Schedule Calendar "
                    "(Settings → Tado CE → Configure → Schedule Calendar) and "
                    "restart HA to give it the zone's schedule.",
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

        await self._async_flush_pending_cloud()

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

    async def _async_flush_pending_cloud(self) -> None:
        """Write the queued cloud target once the rate-limit window has expired."""
        pending = self._runtime.pending_cloud_target
        if pending is None:
            return

        now = time.monotonic()
        last_cloud = self._runtime.last_cloud_write_ts
        if last_cloud is not None and (now - last_cloud) < self._cloud_rate_limit_seconds:
            return

        self._runtime.pending_cloud_target = None
        await self._async_write_cloud(pending)

    # ------------------------------------------------------------------
    # Input readers
    # ------------------------------------------------------------------

    def _get_zone_data(self) -> dict[str, Any] | None:
        """Read zone data with the freshest HomeKit reading merged in.

        Merging HomeKit temperature into sensorDataPoints lets
        _read_trv_temperature() see real-time pushes instead of the
        last cloud poll.
        """
        from .helpers import get_zone_state, merge_homekit_into_zone_data

        zone_data = get_zone_state(self._coordinator.data, self._zone_id)
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

    def _read_desired_target(self, zone_data: dict[str, Any]) -> float | None:
        """Return the desired target: active overlay first, then schedule."""
        from .schedule_helpers import get_current_schedule_target

        overlay = zone_data.get("overlay")
        if overlay is not None:
            overlay_temp = (
                overlay.get("setting", {})
                .get("temperature") or {}
            ).get("celsius")
            if overlay_temp is not None:
                try:
                    return float(overlay_temp)
                except (ValueError, TypeError):
                    pass

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
            await self._async_resume_schedule()
            self._runtime.overlay_set_by_controller = False

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

    async def async_deactivate(self) -> None:
        """Resume schedule if active, then unsubscribe and persist state."""
        if self._runtime.state == ControllerState.ACTIVE and self._runtime.overlay_set_by_controller:
            await self._async_resume_schedule()

        # Order matters: flag first so any sensor event that slips
        # through the subscription path short-circuits; cancel before
        # unsub so a pending debounce firing between the two also sees
        # the flag.
        self._runtime.deactivated = True
        self._action_debouncer.cancel(f"svc_{self._zone_id}")

        for unsub in self._runtime.unsub_external_sensors:
            unsub()
        self._runtime.unsub_external_sensors.clear()

        await self.async_persist_state()

        _LOGGER.info(
            "Smart Valve: zone %s controller deactivated", self._zone_id,
        )

    async def _async_check_device_offset(self) -> None:
        """Warn the user when a non-zero device offset would double-compensate.

        Tado returns offset-adjusted temperatures in
        sensorDataPoints.insideTemperature. Smart Valve Control reads
        that adjusted value and applies its own compensation on top, so
        any non-zero device offset produces overshoot.
        """
        try:
            zone_data = self._get_zone_data()
            if zone_data is None:
                return

            devices = zone_data.get("devices", [])
            if not devices:
                return

            for device in devices:
                serial = device.get("serialNo", "")
                offset = device.get("temperatureOffset", {}).get("celsius")
                if offset is not None and abs(float(offset)) >= 0.1:
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
        attrs: dict[str, Any] = {
            "valve_control_enabled": True,
            "valve_control_active": state == ControllerState.ACTIVE,
        }
        if state == ControllerState.BACKED_OFF:
            attrs["valve_control_backed_off"] = True
        if state == ControllerState.ACTIVE and self._runtime.last_valve_target is not None:
            attrs["valve_target"] = self._runtime.last_valve_target
        if state == ControllerState.ACTIVE and self._runtime.desired_target is not None:
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
        state_dict = {
            "state": self._runtime.state.value,
            "last_valve_target": self._runtime.last_valve_target,
            "last_schedule_target": self._runtime.last_schedule_target,
            "overlay_set_by_controller": self._runtime.overlay_set_by_controller,
            "backed_off_overlay_target": self._runtime.backed_off_overlay_target,
            "desired_target": self._runtime.desired_target,
        }
        await self._zcm.async_set_zone_value(
            self._zone_id, "svc_state", state_dict,
        )

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
            self._runtime.backed_off_overlay_target = _optional_float("backed_off_overlay_target")
            self._runtime.desired_target = _optional_float("desired_target")
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
