"""Offset Sync Controller: per-zone device offset synchronisation so Tado sees the external sensor's reading."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import (
    DEVICE_OFFSET_MAX,
    DEVICE_OFFSET_MIN,
    SMART_VALVE_CLOUD_RATE_LIMIT,
    SMART_VALVE_DEBOUNCE_WINDOW,
    SVC_OFFSET_MAX_STEP,
    SVC_OFFSET_MIN_CHANGE,
)

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

from .climate_helpers import SensorProxy, subscribe_external_sensors
from .exceptions import TadoAuthError, TadoRateLimitError

_LOGGER = logging.getLogger(__name__)

# Pause duration after external offset write (seconds)
_EXTERNAL_WRITE_PAUSE: float = SMART_VALVE_CLOUD_RATE_LIMIT


@dataclass
class OffsetSyncRuntime:
    """In-memory runtime state for one zone's offset sync controller."""

    last_written_offset: float | None = None
    last_offset_write_ts: float | None = None
    paused_until_ts: float | None = None
    # Zone-fetch epoch (coordinator._last_cloud_zone_fetch) in effect at the
    # last confirmed write. Settling-gate compares it against the current
    # epoch: a write is only allowed once a newer zone-state poll has
    # refreshed inside_temperature to reflect that write.
    zone_fetch_at_last_write: datetime | None = None
    unsub_external_sensors: list[CALLBACK_TYPE] = field(default_factory=list)
    # Set in async_deactivate before unsub/cancel so in-flight sensor
    # callbacks and scheduled debounce timers short-circuit instead of
    # writing to a deactivated controller.
    deactivated: bool = False


@dataclass(frozen=True)
class OffsetCalc:
    """Result of calculate_desired_offset: clamped value plus clamp tag.

    clamp_direction is "none" (in range), "hit_max" (raw > +10), or
    "hit_min" (raw < -10). A raw value exactly on the limit is "none";
    only true overshoots flag.
    """

    value: float
    clamp_direction: str


class OffsetSyncController:
    """Per-zone offset sync controller that writes device offsets to match the external sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
    ) -> None:
        """Initialise the controller for one zone."""
        self._hass = hass
        self._coordinator = coordinator
        self._zone_id = zone_id

        self._zcm = coordinator.zone_config_manager
        self._api_client = coordinator.api_client
        self._action_debouncer = coordinator.action_debouncer

        self._runtime = OffsetSyncRuntime()

        # Source of the next evaluation. The sensor-change handler sets
        # this to "sensor"; everything else (post-poll loop, first-tick
        # activation) leaves the default. Surfaced in the structured
        # debug log so users debugging oscillation can tell whether a
        # given line came from a poll tick or a real-time sensor update.
        self._last_trigger: str = "poll"

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def zone_id(self) -> str:
        """Return the zone ID this controller manages."""
        return self._zone_id

    # ------------------------------------------------------------------
    # Core pure functions (static, testable)
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_desired_offset(
        inside_temperature: float,
        current_device_offset: float,
        external_temp: float,
        max_step: float,
    ) -> OffsetCalc:
        """Calculate the offset that would make Tado display external_temp.

        TRV raw reading = inside_temperature - current_device_offset.
        desired_offset = external_temp - TRV raw, stepped to at most
        max_step away from current_device_offset (inside_temperature can be
        momentarily unreliable), then clamped to Tado's ±10°C device limit.

        Returns OffsetCalc with the stepped-and-clamped value and a clamp
        tag reflecting only the device-limit clamp, so the caller can
        surface to the user when the physical gap exceeds Tado's storage
        limit.
        """
        trv_raw = inside_temperature - current_device_offset
        raw_desired = external_temp - trv_raw
        stepped = max(
            current_device_offset - max_step,
            min(raw_desired, current_device_offset + max_step),
        )
        clamped = round(max(DEVICE_OFFSET_MIN, min(stepped, DEVICE_OFFSET_MAX)), 1)

        if raw_desired > DEVICE_OFFSET_MAX:
            direction = "hit_max"
        elif raw_desired < DEVICE_OFFSET_MIN:
            direction = "hit_min"
        else:
            direction = "none"

        return OffsetCalc(value=clamped, clamp_direction=direction)

    @staticmethod
    def should_write(
        desired_offset: float,
        current_offset: float,
        min_change: float = SVC_OFFSET_MIN_CHANGE,
    ) -> bool:
        """Return True when the desired offset differs enough to warrant a write."""
        return abs(desired_offset - current_offset) >= min_change

    # ------------------------------------------------------------------
    # Diagnostic logging
    # ------------------------------------------------------------------

    def _log_decision(self, trigger: str, outcome: str, **fields: Any) -> None:
        """Emit one structured debug line per decision in the cycle.

        Pairs `trigger` ("poll" / "sensor") with `outcome` so a user
        enabling debug logging can replay the controller's decisions
        across an oscillation window: what it saw, why it skipped,
        when it wrote. Lazy: skipped entirely when DEBUG is off.
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        parts = [f"zone={self._zone_id}", f"trigger={trigger}", f"outcome={outcome}"]
        for key, value in fields.items():
            if value is None:
                parts.append(f"{key}=None")
            elif isinstance(value, float):
                parts.append(f"{key}={value:.2f}")
            else:
                parts.append(f"{key}={value}")
        _LOGGER.debug("Offset Sync decision: %s", " ".join(parts))

    # ------------------------------------------------------------------
    # Rate limiting and pause
    # ------------------------------------------------------------------

    def is_rate_limited(self) -> bool:
        """Return True when the last write was less than 300 seconds ago."""
        last_ts = self._runtime.last_offset_write_ts
        if last_ts is None:
            return False
        return (time.monotonic() - last_ts) < SMART_VALVE_CLOUD_RATE_LIMIT

    def is_paused(self) -> bool:
        """Return True when a manual-override pause window is in effect."""
        paused_until = self._runtime.paused_until_ts
        if paused_until is None:
            return False
        return time.monotonic() < paused_until

    def is_settling(self) -> bool:
        """Return True while inside_temperature has not refreshed since the last write.

        Suppresses writes until the cloud zone-state poll reflects the last
        offset write, so the controller can't march on stale feedback (the
        dead-time oscillation).
        """
        at_write = self._runtime.zone_fetch_at_last_write
        if at_write is None:
            return False  # never written, nothing to settle against
        current = self._coordinator.last_zone_fetch_ts()
        if current is None:
            return False  # no fetch recorded yet, don't block
        return current <= at_write  # not advanced → still settling

    def on_external_offset_write(self) -> None:
        """Pause sync for one rate-limit window after an external offset write."""
        self._runtime.paused_until_ts = time.monotonic() + _EXTERNAL_WRITE_PAUSE
        _LOGGER.info(
            "Offset Sync: zone %s paused for %ss after manual offset write, "
            "will resume sync once the pause window expires",
            self._zone_id, int(_EXTERNAL_WRITE_PAUSE),
        )

    # ------------------------------------------------------------------
    # Evaluation cycle
    # ------------------------------------------------------------------

    def _read_cached_offset(self) -> float:
        """Read this zone's offset from the drift cache (coordinator.data).

        Used only as the pre-first-write fallback for the physics calc; the
        steady-state physics source is last_written_offset.
        Returns 0.0 when the cache is absent or unparseable.
        """
        offsets_data = self._coordinator.data.get("offsets", {}) if self._coordinator.data else {}
        if isinstance(offsets_data, dict):
            cached_offset = offsets_data.get(self._zone_id)
            if cached_offset is not None:
                try:
                    return float(cached_offset)
                except (ValueError, TypeError):
                    pass
        return 0.0

    async def async_evaluate(self) -> None:
        """Run one evaluation: read inputs, calculate, threshold, rate-limit, write."""
        if self._runtime.deactivated:
            return

        # Capture and reset the trigger for this evaluation. The sensor-
        # change handler sets _last_trigger to "sensor"; everything else
        # leaves the default. Surfaced in the structured debug log.
        trigger = self._last_trigger
        self._last_trigger = "poll"

        from .climate_helpers import read_external_sensor
        from .helpers import get_zone_state

        zone_data = get_zone_state(self._coordinator.data, self._zone_id)
        if zone_data is None:
            self._log_decision(trigger, outcome="skip_no_zone_data")
            return

        try:
            inside_temperature = float(
                zone_data["sensorDataPoints"]["insideTemperature"]["celsius"],
            )
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning(
                "Offset Sync: zone %s could not read TRV temperature from Tado, "
                "skipping this cycle, will retry on next poll",
                self._zone_id,
            )
            self._log_decision(trigger, outcome="skip_no_inside_temp")
            return

        zone_setting = zone_data.get("setting") or {}
        power = zone_setting.get("power", "OFF")
        target = (zone_setting.get("temperature") or {}).get("celsius")

        external_temp = read_external_sensor(
            self._hass, self._zcm, self._zone_id, "external_temp_sensor",
        )
        if external_temp is None:
            self._log_decision(
                trigger, outcome="skip_no_external",
                power=power, target=target, trv=inside_temperature,
            )
            return

        # Single-offset consistency: physics and the write decision must
        # use the SAME offset value. Prefer the runtime
        # last_written_offset; fall back to the drift cache only before the
        # first write (None), which also matches settling Scenario C.
        cached_offset = self._read_cached_offset()
        physics_offset = (
            self._runtime.last_written_offset
            if self._runtime.last_written_offset is not None
            else cached_offset
        )

        if self.is_paused():
            self._log_decision(
                trigger, outcome="skip_paused",
                power=power, target=target, ext=external_temp,
                trv=inside_temperature, current_offset=physics_offset,
            )
            return

        zone_config = self._zcm.get_zone_config(self._zone_id)
        min_change = float(zone_config.get("svc_offset_min_change", SVC_OFFSET_MIN_CHANGE))
        # Never smaller than min_change, or should_write's threshold
        # becomes permanently unreachable.
        max_step = max(SVC_OFFSET_MAX_STEP, min_change)

        calc = self.calculate_desired_offset(
            inside_temperature, physics_offset, external_temp, max_step,
        )
        desired_offset = calc.value
        effective_current = physics_offset

        if not self.should_write(desired_offset, effective_current, min_change):
            self._log_decision(
                trigger, outcome="eval_no_write",
                power=power, target=target, ext=external_temp,
                trv=inside_temperature, current_offset=physics_offset,
                last_written=self._runtime.last_written_offset,
                desired=desired_offset, min_change=min_change,
                clamp=calc.clamp_direction,
            )
            return

        if self.is_settling():
            self._log_decision(
                trigger, outcome="skip_settling",
                power=power, target=target, ext=external_temp,
                trv=inside_temperature, current_offset=physics_offset,
                last_written=self._runtime.last_written_offset,
                desired=desired_offset, min_change=min_change,
                clamp=calc.clamp_direction,
            )
            return

        if self.is_rate_limited():
            self._log_decision(
                trigger, outcome="eval_rate_limited",
                power=power, target=target, ext=external_temp,
                trv=inside_temperature, current_offset=physics_offset,
                last_written=self._runtime.last_written_offset,
                desired=desired_offset, min_change=min_change,
                clamp=calc.clamp_direction,
            )
            return

        if self._coordinator.is_cloud_backoff_active():
            self._log_decision(
                trigger, outcome="eval_backoff_held",
                power=power, target=target, ext=external_temp,
                trv=inside_temperature, current_offset=physics_offset,
                last_written=self._runtime.last_written_offset,
                desired=desired_offset, min_change=min_change,
                clamp=calc.clamp_direction,
            )
            return

        self._log_decision(
            trigger, outcome="eval_write_attempt",
            power=power, target=target, ext=external_temp,
            trv=inside_temperature, current_offset=physics_offset,
            last_written=self._runtime.last_written_offset,
            desired=desired_offset, min_change=min_change,
            clamp=calc.clamp_direction,
        )

        try:
            await self._async_write_offset(desired_offset, calc.clamp_direction)
        except (TadoAuthError, TadoRateLimitError) as e:
            from .error_dispatch import handle_background_write_error

            handle_background_write_error(
                e, self._coordinator.config_entry, self._hass, self._coordinator,
                f"Offset Sync: zone {self._zone_id} write failed, "
                "starting recovery; will retry on next sensor change",
            )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def _async_write_offset(
        self, offset: float, clamp_direction: str = "none",
    ) -> None:
        """Write offset to every device in the zone, gated by readback.

        State and cache update only after Tado confirms the written
        value via readback. Prevents the optimistic-update feedback
        loop where a failed write (rate limit, server error, queue
        overflow) would poison the cache with a value the TRV never
        stored.

        `clamp_direction` is recorded in
        `coordinator.data["offset_clamps"]` so the climate entity can
        surface the clamp state to the user.
        """
        from .write_optimizer import DeviceOperation

        serials = self._get_zone_device_serials()
        if not serials:
            _LOGGER.warning(
                "Offset Sync: zone %s has no devices to write to, "
                "skipping offset update",
                self._zone_id,
            )
            return

        device_sync_queue = self._coordinator.device_sync_queue
        dones: list[asyncio.Future[bool]] = []
        any_accepted = False

        for serial in serials:
            async def _do_write(s: str = serial, o: float = offset) -> bool:
                return await self._api_client.set_device_offset(s, o)

            operation = DeviceOperation(
                device_serial=serial,
                operation_name=f"offset_sync_{self._zone_id}",
                callback=_do_write,
                entity_id=f"offset_sync.zone_{self._zone_id}",
            )
            accepted, done = await device_sync_queue.enqueue(operation)
            if accepted:
                any_accepted = True
                dones.append(done)
            else:
                from .helpers import mask_serial

                _LOGGER.warning(
                    "Offset Sync: zone %s device %s write queue full, "
                    "dropping this update, will retry on next sensor change",
                    self._zone_id, mask_serial(serial),
                )

        if not any_accepted:
            _LOGGER.warning(
                "Offset Sync: zone %s could not queue offset write for any "
                "device, leaving offset unchanged, will retry on next "
                "sensor change",
                self._zone_id,
            )
            return

        # Failure on any device means we cannot claim the offset landed
        # on every TRV in the zone, safer to retry than to poison the
        # cache with a value only some devices accepted.
        results = await asyncio.gather(*dones, return_exceptions=False)
        if not all(results):
            _LOGGER.warning(
                "Offset Sync: zone %s one or more device writes failed, "
                "leaving offset unchanged, will retry on next sensor change",
                self._zone_id,
            )
            return

        readback = await self._api_client.get_device_offset(serials[0])
        if readback is None:
            _LOGGER.warning(
                "Offset Sync: zone %s could not read offset back from Tado "
                "after writing %.1f°C, leaving cache unchanged",
                self._zone_id, offset,
            )
            return

        if abs(readback - offset) > 0.05:
            _LOGGER.warning(
                "Offset Sync: zone %s wrote %.1f°C but Tado reports %.1f°C. "
                "Tado may have clamped or rejected the value. Leaving "
                "cache unchanged.",
                self._zone_id, offset, readback,
            )
            return

        self._runtime.last_written_offset = readback
        self._runtime.last_offset_write_ts = time.monotonic()
        self._runtime.zone_fetch_at_last_write = self._coordinator.last_zone_fetch_ts()

        raw_offsets = self._coordinator.data_loader.get_cached("offsets")
        cached_offsets: dict[str, float] = (
            raw_offsets if isinstance(raw_offsets, dict) else {}
        )
        cached_offsets[self._zone_id] = readback
        self._coordinator.data_loader.update_cache("offsets", cached_offsets)
        if self._coordinator.data and isinstance(self._coordinator.data, dict):
            self._coordinator.data["offsets"] = cached_offsets

        raw_clamps = self._coordinator.data_loader.get_cached("offset_clamps")
        cached_clamps: dict[str, str] = (
            raw_clamps if isinstance(raw_clamps, dict) else {}
        )
        cached_clamps[self._zone_id] = clamp_direction
        self._coordinator.data_loader.update_cache("offset_clamps", cached_clamps)
        if self._coordinator.data and isinstance(self._coordinator.data, dict):
            self._coordinator.data["offset_clamps"] = cached_clamps

        await self.async_persist_state()

        if clamp_direction != "none":
            direction_desc = (
                "above the +10°C maximum" if clamp_direction == "hit_max"
                else "below the -10°C minimum"
            )
            _LOGGER.warning(
                "Offset Sync: zone %s wrote offset %.1f°C, but the required "
                "correction was %s, the TRV's displayed temperature will "
                "still differ from the external sensor. Check for draughts, "
                "a cold external wall, or sensor placement.",
                self._zone_id, readback, direction_desc,
            )

        _LOGGER.debug(
            "Offset Sync: zone %s wrote offset %.1f°C to %d device(s) "
            "(Tado confirmed)",
            self._zone_id, readback, len(serials),
        )

    def _get_zone_device_serials(self) -> list[str]:
        """Get device serials for this zone from zones_info cache."""
        zones_info = self._coordinator.data_loader.get_cached("zones_info")
        if not zones_info or not isinstance(zones_info, list):
            return []

        for zone in zones_info:
            if str(zone.get("id")) == self._zone_id:
                serials: list[str] = []
                for device in zone.get("devices") or []:
                    serial = device.get("shortSerialNo")
                    if serial:
                        serials.append(serial)
                return serials
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_activate(self) -> None:
        """Load persisted state and subscribe to external sensor changes."""
        await self.async_load_state()

        proxy = SensorProxy(self._hass, self._coordinator)
        unsubs = subscribe_external_sensors(
            proxy,
            self._zone_id,
            self._on_external_sensor_change,
            include_humidity=False,
        )
        self._runtime.unsub_external_sensors = unsubs

        last = self._runtime.last_written_offset
        _LOGGER.info(
            "Offset Sync: zone %s controller activated (last offset=%s°C)",
            self._zone_id,
            f"{last:.1f}" if last is not None else "unknown",
        )

    async def async_deactivate(self) -> None:
        """Unsubscribe sensors and persist state without changing the device offset."""
        # Order matters: flag first so any sensor event that slips
        # through short-circuits; cancel before unsub so a pending
        # debounce firing between the two also sees the flag.
        self._runtime.deactivated = True
        self._action_debouncer.cancel(f"offset_sync_{self._zone_id}")

        for unsub in self._runtime.unsub_external_sensors:
            unsub()
        self._runtime.unsub_external_sensors.clear()

        await self.async_persist_state()

        last = self._runtime.last_written_offset
        _LOGGER.info(
            "Offset Sync: zone %s controller deactivated, offset preserved at %s°C",
            self._zone_id,
            f"{last:.1f}" if last is not None else "unknown",
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def async_persist_state(self) -> None:
        """Persist last_written_offset to zone config.

        last_offset_write_ts is deliberately not persisted: it's a
        monotonic-clock value, meaningless across process restarts.
        async_set_zone_value replaces the whole offset_sync_state dict,
        so stale fields left over from older versions are cleared on
        the first persist after upgrade.
        """
        state_dict: dict[str, float | None] = {
            "last_written_offset": self._runtime.last_written_offset,
        }
        await self._zcm.async_set_zone_value(
            self._zone_id, "offset_sync_state", state_dict,
        )

    async def async_load_state(self) -> None:
        """Restore persisted state on startup, falling back to a fresh API read."""
        config = self._zcm.get_zone_config(self._zone_id)
        raw = config.get("offset_sync_state")
        if not isinstance(raw, dict):
            _LOGGER.debug(
                "Offset Sync: zone %s has no persisted state, reading "
                "current offset from Tado",
                self._zone_id,
            )
            await self._async_load_offset_from_api()
            return

        try:
            offset_raw = raw.get("last_written_offset")
            # Cast inside try so a non-numeric value (e.g. from a manual
            # edit or schema change) triggers the except fallback
            # instead of leaking a string into runtime.
            self._runtime.last_written_offset = (
                float(offset_raw) if offset_raw is not None else None
            )
            # Don't restore last_offset_write_ts: monotonic resets on
            # boot, so a persisted value would block writes for as long
            # as the previous HA uptime.
            self._runtime.last_offset_write_ts = None
            _LOGGER.debug(
                "Offset Sync: zone %s restored last offset %s°C from storage",
                self._zone_id, self._runtime.last_written_offset,
            )
        except (ValueError, TypeError, KeyError):
            _LOGGER.warning(
                "Offset Sync: zone %s persisted state was unreadable, "
                "starting fresh",
                self._zone_id,
            )
            await self._async_load_offset_from_api()

    async def _async_load_offset_from_api(self) -> None:
        """Read the device offset from Tado for a fresh install or corrupt state."""
        serials = self._get_zone_device_serials()
        if not serials:
            return

        try:
            offset = await self._api_client.get_device_offset(serials[0])
            if offset is not None:
                self._runtime.last_written_offset = offset
                _LOGGER.debug(
                    "Offset Sync: zone %s loaded current offset %.1f°C from Tado",
                    self._zone_id, offset,
                )
        except (TadoAuthError, TadoRateLimitError) as e:
            from .error_dispatch import handle_background_write_error

            handle_background_write_error(
                e, self._coordinator.config_entry, self._hass, self._coordinator,
                f"Offset Sync: zone {self._zone_id} could not read offset, "
                "starting recovery; starting with offset 0.0°C",
            )
        except (TimeoutError, aiohttp.ClientError):
            _LOGGER.warning(
                "Offset Sync: zone %s could not read offset from Tado, "
                "starting with offset 0.0°C",
                self._zone_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Sensor change handler
    # ------------------------------------------------------------------

    def _on_external_sensor_change(self, _event: Any) -> None:
        """Tag the next evaluation as sensor-driven and debounce it."""
        if self._runtime.deactivated:
            return
        self._last_trigger = "sensor"
        self._hass.async_create_task(
            self._action_debouncer.debounce(
                f"offset_sync_{self._zone_id}",
                self.async_evaluate,
                window=SMART_VALVE_DEBOUNCE_WINDOW,
            ),
        )

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    def get_attributes(self) -> dict[str, Any]:
        """Return attributes for the climate entity's extra_state_attributes."""
        active = not self._runtime.deactivated and not self.is_paused()
        return {
            "valve_control_active": active,
            "valve_control_mode": "offset_sync",
        }
