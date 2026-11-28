"""Tado CE heating climate entity: TRV / thermostat control with overlay management.

Carries the optimistic-update + rollback pattern from
`climate_helpers.api_call_with_rollback`, and consults the state
reconciler to merge cloud + HomeKit targets so the bridge can't
push stale values over fresh user actions during the
write-protection window.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.components.climate import ATTR_HVAC_MODE
from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_HOME,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .climate_base import TadoClimateBase
from .climate_helpers import (
    CLIMATE_STATE_NAMES,
    api_call_with_rollback,
    apply_homekit_target,
    cloud_write_failed,
    homekit_target_attribute,
    inject_presence_state,
    source_update_attrs,
    update_offset,
    update_offset_clamp,
    update_preset_mode,
)
from .const import FORCEABLE_FETCH_HOME_STATE, OPEN_WINDOW_DEFAULT_TEMP
from .error_dispatch import dispatch_to_service_call
from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError
from .format_helpers import (
    format_overlay_type as _format_overlay_type,
)
from .helpers import (
    Route,
    RouteDecision,
    async_trigger_immediate_refresh,
    build_timer_termination,
    decide_route,
    get_zone_overlay_termination,
    get_zone_state,
    payload_to_time,
)
from .homekit_provider import PutOutcome
from .optimistic_helpers import (
    OptimisticUpdateResult,
    clear_homekit_write,
    hold_locally,
    is_held_locally,
    mark_homekit_put,
    put_time,
    resend_entry,
    resolve_optimistic_update,
    set_optimistic_fields,
    stash_resend,
    supersede_owed,
    take_resend_payload,
    zone_owes_change,
)
from .ratelimit import async_check_bootstrap_reserve_or_raise as _check_bootstrap_reserve_or_raise
from .schedule_helpers import get_active_timetable, get_current_schedule_target
from .write_optimizer import ActionGuard, held_data_unconfirmed
from .zone_signals import ZoneView, owed_held_target, zone_power_is_on, zone_view, zone_view_target
from .zone_write_ledger import IntentKind, IntentState, WriteIntent, ordered_zone_write

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .api_client import TadoApiClient
    from .coordinator import TadoDataUpdateCoordinator
    from .zone_write_ledger import ResendEntry

_LOGGER = logging.getLogger(__name__)


class TadoClimate(TadoClimateBase):
    """Tado CE Heating Climate Entity."""

    _LOG_LABEL = "Climate Heating"
    _ZONE_TYPE = "HEATING"
    _write_timing_zone_kind = "heating"
    _DEFAULT_TARGET = 20.0

    def __init__(self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, home_id: str) -> None:
        super().__init__(
            coordinator, zone_id, zone_name, home_id, registry_key="climate_heating", device_zone_type="HEATING",
        )
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.PRESET_MODE
        )
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
        self._attr_preset_modes = [PRESET_HOME, PRESET_AWAY]
        self._attr_target_temperature_step = 0.5

        # Per-zone min/max temp (set in _update_temp_limits() from zone_config_manager,
        # called on add-to-hass and on every zone-config change)
        self._attr_min_temp = 5
        self._attr_max_temp = 25

        self._attr_native_current_temperature = None
        self._attr_native_target_temperature = None
        self._attr_hvac_mode = None
        self._attr_hvac_action = None
        self._attr_current_humidity = None

        # Extra attributes
        self._prev_off_state: tuple[Any, Any, str] | None = None
        self._heating_power = None
        self._offset_celsius = None  # Temperature offset (optional, enabled in config)
        self._attr_preset_mode = PRESET_HOME
        # Preset taps whose PUT is running, and the preset Tado held when the first of them started or the last one accepted.
        self._presets_in_flight = 0
        self._preset_baseline: str | None = PRESET_HOME

        self._power_source = "cloud"
        self._target_source = "cloud"

    def _calculate_hvac_action(
        self, target_temp: float | None = None, hvac_mode: HVACMode | None = None,
        *, homekit_target_newer: bool = False, ignore_expected: bool = False,
    ) -> HVACAction:
        """Calculate hvac_action for heating zone.

        Priority: OFF mode > optimistic target_temp > expected action > bridge band (a HomeKit target newer than the poll) >
        heating_power > temperature-aware HEAT fallback > IDLE. The
        target_temp branch must run before _expected_hvac_action so a
        new optimistic update overrides a stale expected action.
        ignore_expected skips the expected rung to derive the action from Tado's data.
        """
        mode = hvac_mode if hvac_mode is not None else self._attr_hvac_mode

        # OFF mode always returns OFF
        if mode == HVACMode.OFF:
            return HVACAction.OFF

        # Optimistic call: judge the new target against the room, before the stale expected action
        if target_temp is not None and mode == HVACMode.HEAT:
            current = self._attr_native_current_temperature
            heated = bool(self._heating_power and self._heating_power > 0)
            if current is None:
                return HVACAction.HEATING if heated else HVACAction.IDLE
            return HVACAction.HEATING if target_temp > current + 0.5 or (heated and target_temp > current) else HVACAction.IDLE

        # If we have optimistic state with expected action, use it
        # This ensures optimistic updates work even when current temp >= target
        if self._expected_hvac_action is not None and not ignore_expected:
            return self._expected_hvac_action

        # A bridge target newer than the cloud poll outdates heating_power, so an open valve counts only while the room is below the target
        if homekit_target_newer:
            target = self._attr_native_target_temperature
            current = self._attr_native_current_temperature
            heated = bool(self._heating_power and self._heating_power > 0)
            if target is not None and current is not None and (target > current + 0.5 or (heated and target > current)):
                return HVACAction.HEATING
            return HVACAction.IDLE

        # API confirms heating (highest priority when available)
        if self._heating_power and self._heating_power > 0:
            return HVACAction.HEATING

        # Temperature-aware fallback for HEAT mode
        # This handles the case where API hasn't updated heating_power yet
        if mode == HVACMode.HEAT:
            target = self._attr_native_target_temperature
            current = self._attr_native_current_temperature
            if target is not None and current is not None:
                # 0.5°C buffer for hysteresis to prevent flip-flopping
                if target > current + 0.5:
                    return HVACAction.HEATING

        return HVACAction.IDLE

    def _shown_power(self, view_power: str | None) -> str | None:
        """Return the power the card shows; a displayed OFF holds an own OFF write pending against a stored setpoint."""
        return "OFF" if self._attr_hvac_mode == HVACMode.OFF else view_power

    @callback
    def _handle_homekit_update(self, zone_id: str) -> None:
        """Handle HomeKit data update for this zone."""
        if zone_id != self._zone_id:
            return
        fresh = self.coordinator.is_entity_fresh(self.entity_id)
        homekit_target_newer = False

        zone_data = get_zone_state(self.coordinator.data, self._zone_id) or {}
        self._update_sensor_data(zone_data)

        view = zone_view(
            self.coordinator, self._zone_id, zone_data=zone_data, sequence_pending=self._optimistic_sequence is not None,
        )
        power, self._power_source = view.power, view.power_source
        power_off = power == "OFF"
        adopted = False
        if power_off and self._optimistic_sequence is None and self._attr_hvac_mode != HVACMode.OFF:
            self._attr_hvac_mode = HVACMode.OFF
            self._off_branch_target()
            adopted = True
        elif power == "ON" and self._optimistic_sequence is None and self._attr_hvac_mode == HVACMode.OFF:
            # Level-triggered return from a shown OFF: the cloud mapping and target, never derived beyond them.
            self._attr_hvac_mode = self._cloud_on_mode(zone_data)
            cloud_celsius = ((zone_data.get("setting") or {}).get("temperature") or {}).get("celsius")
            if cloud_celsius is not None:
                self._attr_native_target_temperature = cloud_celsius
            adopted = True

        # Skipped inside the post-write window: a stale bridge value would undo the write.
        if self.coordinator.state_reconciler is not None and not fresh:
            # The cloud value is not passed: only a HomeKit result is applied, anything else leaves the card as it is.
            home_target, target_src = zone_view_target(
                self.coordinator, view, self._shown_power(power), homekit_only=True,
            )
            if target_src == "homekit" and home_target is not None:
                previous_target = self._attr_native_target_temperature
                apply_homekit_target(self, home_target)
                adopted = True
                if home_target != previous_target:
                    _LOGGER.debug(
                        "Climate Heating: %s target %s → %s°C (source %s)",
                        self._zone_name, previous_target, home_target, target_src,
                    )
            homekit_target_newer = target_src == "homekit"

        # The handler only ever moves the mode to OFF: HomeKit cannot express AUTO (schedule-following), so ON and AUTO come from the cloud poll.

        self._attr_hvac_action = self._calculate_hvac_action(
            hvac_mode=self._attr_hvac_mode, homekit_target_newer=homekit_target_newer,
        )
        # The device holds what the bridge showed; a hold still standing is a write of ours, whose value is not the device's yet.
        if adopted and self._optimistic_sequence is None:
            self._observe_accepted()
        self.async_write_ha_state()

        # Record temperature for heating cycle analysis with fresh target
        if self._attr_native_target_temperature is not None and not power_off:
            self._schedule_heating_cycle_update(self._attr_native_target_temperature)

    @callback
    def _update_temp_limits(self) -> None:
        """Update min/max temp from zone config.

        Only applies user-explicit overrides (has_zone_override check);
        otherwise defaults to 5°C / 25°C, consistent with the AC code
        path.
        """
        zone_config_manager = self.coordinator.zone_config_manager
        if zone_config_manager:
            if zone_config_manager.has_zone_override(self._zone_id, "min_temp"):
                self._attr_min_temp = zone_config_manager.get_zone_value(self._zone_id, "min_temp", 5.0)
            else:
                self._attr_min_temp = 5.0

            if zone_config_manager.has_zone_override(self._zone_id, "max_temp"):
                self._attr_max_temp = zone_config_manager.get_zone_value(self._zone_id, "max_temp", 25.0)
            else:
                self._attr_max_temp = 25.0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        attrs: dict[str, Any] = {
            "overlay_type": _format_overlay_type(self._overlay_type),
            "heating_power": self._heating_power,
            "zone_id": self._zone_id,
            "temperature_source": self._temperature_source,
            "power_source": self._power_source,
            "humidity_source": self._humidity_source,
            **source_update_attrs(
                self.coordinator, self._zone_id, self._temperature_source, self._humidity_source,
            ),
            "external_temp_sensor": self._external_temp_sensor,
            "external_humidity_sensor": self._external_humidity_sensor,
            "last_write_source": self._last_write_source,
            "homekit_target": homekit_target_attribute(
                self.coordinator, self._zone_id, self._attr_native_target_temperature,
            ),
        }
        # Only include offset_celsius if enabled and available
        if self._offset_celsius is not None:
            attrs["offset_celsius"] = self._offset_celsius
            # Offset Sync clamp signal: user-visible when the physical
            # gap needed a correction outside Tado's ±10°C device limit.
            clamp_direction = update_offset_clamp(self.coordinator, self._zone_id)
            if clamp_direction is not None:
                attrs["offset_clamped"] = clamp_direction != "none"
                attrs["offset_clamp_direction"] = clamp_direction
        # Present only while a change sent over HomeKit during a cloud outage is held; a test double's ledger answers with a non-intent, which counts as none.
        ledger = self._write_ledger()
        owed = ledger.owed(self._zone_id) if ledger is not None else None
        if isinstance(owed, WriteIntent) and owed.state is IntentState.HELD_LOCAL:
            attrs["write_held_locally"] = True
            if owed.created_at is not None:
                attrs["write_held_since"] = owed.created_at.isoformat()
        # Schedule Preview: show current schedule target temperature
        scheduled_temp = get_current_schedule_target(
            self._zone_id,
            data_loader=self.coordinator.data_loader,
        )
        if scheduled_temp is not None:
            attrs["scheduled_target_temperature"] = scheduled_temp
        active_timetable = get_active_timetable(self._zone_id, self.coordinator.data_loader)
        if active_timetable is not None:
            attrs["active_timetable"] = active_timetable

        # Smart Valve Control attributes
        controller = self.coordinator.valve_controllers.get(self._zone_id)
        if controller is not None:
            attrs.update(controller.get_attributes())
        else:
            os_controller = self.coordinator.offset_sync_controllers.get(self._zone_id)
            if os_controller is not None:
                attrs.update(os_controller.get_attributes())
            else:
                attrs["valve_control_active"] = False

        return attrs

    def _update_sensor_data(self, zone_data: dict[str, Any]) -> tuple[float | None, str, float | None, str]:
        """Merge the sensors through the base, then clear heating-cycle state on a missing temperature."""
        merged_temp, temp_source, merged_hum, hum_source = super()._update_sensor_data(zone_data)
        hcc = self.coordinator.heating_cycle_coordinator
        if merged_temp is None and hcc is not None:
            hcc.clear_zone_state(self._zone_id)

        # Log source changes for diagnostic tracing
        if temp_source != getattr(self, "_prev_temp_source", None) or hum_source != getattr(self, "_prev_hum_source", None):
            sensor_data = zone_data.get("sensorDataPoints") or {}
            _LOGGER.debug(
                "Climate Heating: %s merge, temp=%s (%s), humidity=%s "
                "(%s), cloud_temp=%s, cloud_hum=%s",
                self._zone_name, merged_temp, temp_source, merged_hum, hum_source,
                (sensor_data.get("insideTemperature") or {}).get("celsius"),
                (sensor_data.get("humidity") or {}).get("percentage"),
            )
            self._prev_temp_source = temp_source
            self._prev_hum_source = hum_source
        return merged_temp, temp_source, merged_hum, hum_source

    def _determine_api_hvac_mode(self, view: ZoneView, zone_data: dict[str, Any]) -> HVACMode:
        """Determine HVAC mode from API power state and overlay type."""
        self._overlay_type = zone_data.get("overlayType")

        if view.power == "ON":
            self._prev_off_state = None
            temp, self._target_source = zone_view_target(self.coordinator, view, self._shown_power(view.power))
            self._attr_native_target_temperature = temp
            self._schedule_heating_cycle_update(temp)
            return self._cloud_on_mode(zone_data)

        self._target_source = "cloud"
        self._off_branch_target()
        if self._overlay_type == "MANUAL":
            self._prev_off_state = None
            return HVACMode.OFF
        off_state = (view.power, self._overlay_type, repr(zone_data.get("setting")))
        if off_state != self._prev_off_state:
            self._prev_off_state = off_state
            _LOGGER.debug(
                "Climate Heating: %s is off without a manual overlay, power=%s overlayType=%s setting=%s",
                self._zone_name, view.power, self._overlay_type, zone_data.get("setting"),
            )
        return HVACMode.OFF

    @staticmethod
    def _cloud_on_mode(zone_data: dict[str, Any]) -> HVACMode:
        """Return the mode the cloud overlay type gives an ON zone."""
        return HVACMode.HEAT if zone_data.get("overlayType") == "MANUAL" else HVACMode.AUTO

    def _off_branch_target(self) -> None:
        """Show the frost target for every OFF zone, so a schedule change does not move an OFF card."""
        self._attr_native_target_temperature = OPEN_WINDOW_DEFAULT_TEMP

    def _schedule_heating_cycle_update(self, temp: float | None) -> None:
        """Schedule async heating cycle coordinator update if applicable."""
        if temp is None or self._attr_native_current_temperature is None:
            return
        heating_cycle_coordinator = self.coordinator.heating_cycle_coordinator
        if not heating_cycle_coordinator:
            return

        # Current temperature is captured now so the task feeds the value this update saw.
        self.hass.async_create_task(
            self._async_feed_heating_cycle(temp, self._attr_native_current_temperature),
        )

    async def _async_feed_heating_cycle(self, target: float, current: float | None) -> None:
        """Feed the heating-cycle coordinator with this tick's heating evidence, skipping when there is no room temperature."""
        heating_cycle_coordinator = self.coordinator.heating_cycle_coordinator
        if heating_cycle_coordinator is None or current is None:
            return
        # Heating evidence of this tick: a cycle that ends with none is dropped.
        heating = bool(self._heating_power and self._heating_power > 0) or self._attr_hvac_action == HVACAction.HEATING
        try:
            await heating_cycle_coordinator.on_zone_update(self._zone_id, target, current, heating=heating)
        except (KeyError, TypeError, ValueError):
            _LOGGER.debug(
                "Climate Heating: %s heating-cycle update failed, skipping this tick",
                self._zone_name,
                exc_info=True,
            )

    def _apply_optimistic_or_api_state(
        self,
        api_hvac_mode: HVACMode,
        api_hvac_action: HVACAction,
        power: str | None,
        api_target_temperature: float | None = None,
        *,
        overlay_present: bool,
    ) -> None:
        """Apply optimistic or API state based on sequence comparison.

        api_target_temperature is added to api_values when non-None so
        optimistic resolution preserves the user's target until the API
        propagates it (usually one poll cycle). hvac_action is derived, so Tado never confirms it.
        """
        api_values: dict[str, Any] = (
            {"overlay_present": overlay_present}
            if self._expected_overlay_present is not None
            else {"hvac_mode": api_hvac_mode}
        )
        if api_target_temperature is not None:
            api_values["target_temperature"] = api_target_temperature

        result = resolve_optimistic_update(
            self,
            api_values=api_values,
            entry_id=self._entry_id,
        )

        if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
            self._attr_hvac_mode = self._expected_hvac_mode
            self._attr_hvac_action = self._expected_hvac_action
            # Also preserve target_temperature when its expected value is set
            if self._expected_target_temperature is not None:
                self._attr_native_target_temperature = self._expected_target_temperature
            _LOGGER.debug(
                "Climate Heating: %s holding optimistic state, "
                "mode=%s action=%s target=%s",
                self._zone_name,
                self._attr_hvac_mode,
                self._attr_hvac_action,
                self._attr_native_target_temperature,
            )
        else:
            # Cloud state first, so a resend started below keeps its optimistic state
            self._attr_hvac_mode = api_hvac_mode
            self._attr_hvac_action = api_hvac_action
            if power != "ON" and api_hvac_mode == HVACMode.OFF:
                self._attr_hvac_action = HVACAction.OFF
            # Before a resend starts below: its failure goes back to what Tado reports.
            self._observe_accepted()

            if result == OptimisticUpdateResult.EXPIRED:
                # Record failure if this was a HomeKit write, and retry it
                # via cloud PUT: the local write may well have succeeded, the
                # bridge→cloud sync just hasn't caught up within the window.
                if self._last_write_source == "homekit":
                    started = self.coordinator.last_zone_fetch_ts()
                    put_at = put_time(self)
                    elapsed = (started - put_at).total_seconds() if started and put_at else 0.0
                    held_locally = is_held_locally(self)
                    if held_locally:
                        _LOGGER.info(
                            "%s: Tado's data %.0f s after the change made while Tado was unreachable "
                            "does not show it yet; showing Tado's reported state, the change stays held",
                            self._zone_name, elapsed,
                        )
                    else:
                        _LOGGER.warning(
                            "%s: Tado's data %.0f s after the HomeKit change did not "
                            "show it, resending through the cloud and showing Tado's "
                            "reported state until then",
                            self._zone_name, elapsed,
                        )
                    write_tracker = self.coordinator.write_health_for(self._zone_id)
                    # A held write was never sent to the cloud, so Tado's old data says nothing about the bridge.
                    if write_tracker is not None and not held_locally:
                        write_tracker.record_failure()
                    # Always taken so a stale payload is dropped; a held change is judged by the ledger, so nothing is resent for it.
                    pending_write = take_resend_payload(self)
                    if held_locally:
                        pending_write = None
                    entry = None
                    if pending_write is not None:
                        # Taken before the put record is cleared: the resend is judged against the zone as it was at the put.
                        power, celsius = self._resend_value(pending_write)
                        entry = resend_entry(self, power=power, celsius=celsius)
                    clear_homekit_write(self)
                    if pending_write is not None:
                        self._spawn_retry_task(self._retry_expired_write_via_cloud(pending_write, entry=entry))

            elif result == OptimisticUpdateResult.ACCEPT_API and self._last_write_source == "homekit":
                # API confirmed the HomeKit write
                write_tracker = self.coordinator.write_health_for(self._zone_id)
                if write_tracker is not None:
                    write_tracker.record_success()
                self._last_write_source = ""

    def _record_smart_comfort(self) -> None:
        """Record temperature for Smart Comfort analytics."""
        _scm = self.coordinator.smart_comfort_manager
        if not (_scm and _scm.is_enabled and self._attr_native_current_temperature is not None):
            return
        try:
            _scm.record_temperature(
                zone_id=self._zone_id,
                zone_name=self._zone_name,
                temperature=self._attr_native_current_temperature,
                is_heating=(self._heating_power is not None and self._heating_power > 0),
                target_temperature=self._attr_native_target_temperature,
            )
        except (KeyError, TypeError, ValueError) as e:
            _LOGGER.debug(
                "Climate Heating: %s could not record Smart Comfort "
                "data (%s), skipping this sample",
                self._zone_name, e,
            )

    @callback
    def update(self) -> None:
        """Update climate state from the coordinator's cached zone data."""
        if self._skip_update_while_fresh(has_state=self._attr_native_current_temperature is not None):
            return

        try:
            coord_data = self.coordinator.data or {}
            config = coord_data.get("config")
            if config:
                self._home_id = config.get("home_id")

            zone_data = get_zone_state(coord_data, self._zone_id)

            if not zone_data:
                self._data_present = False
                return

            self._update_sensor_data(zone_data)

            # Heating power
            activity_data = zone_data.get("activityDataPoints") or {}
            self._heating_power = (activity_data.get("heatingPower") or {}).get("percentage", 0)

            # Determine API state
            setting = zone_data.get("setting") or {}
            view = zone_view(
                self.coordinator, self._zone_id, zone_data=zone_data,
                sequence_pending=self._optimistic_sequence is not None,
            )
            power, self._power_source = view.power, view.power_source
            api_hvac_mode = self._determine_api_hvac_mode(view, zone_data)

            # API-reported target temperature. None when zone is OFF /
            # frost-protection (no celsius key). The resolver then skips
            # target_temperature matching, unchanged behavior.
            api_target_temp = (setting.get("temperature") or {}).get("celsius")

            # Calculate hvac_action
            api_hvac_action = self._calculate_hvac_action(
                hvac_mode=api_hvac_mode, ignore_expected=True,
                homekit_target_newer=self._target_source == "homekit",
            )

            # Apply state
            self._apply_optimistic_or_api_state(
                api_hvac_mode, api_hvac_action, power,
                api_target_temperature=api_target_temp,
                overlay_present=zone_data.get("overlayType") is not None,
            )
            self._data_present = True

            self._record_smart_comfort()
            self._update_preset_mode()
            self._update_offset()

        except Exception as e:
            _LOGGER.warning(
                "Climate Heating: %s update failed (%s), entity marked "
                "unavailable, will retry on next poll",
                self.name, e,
            )
            self._data_present = False

    @callback
    def _update_offset(self) -> None:
        """Update temperature offset from cached offsets file."""
        self._offset_celsius = update_offset(self.coordinator, self._zone_id)  # type: ignore[assignment]

    @callback
    def _update_preset_mode(self) -> None:
        """Update preset mode based on home state."""
        result = update_preset_mode(self.coordinator)
        if result is not None:
            self._attr_preset_mode = result
            # What Tado reports is what a failing tap in flight goes back to.
            if self._presets_in_flight:
                self._preset_baseline = result

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set preset mode (Home/Away)."""
        # Action Guard: skip if preset already matches current state
        if ActionGuard.should_skip_preset_mode(preset_mode, self._attr_preset_mode):
            _LOGGER.debug(
                "Climate Heating: %s preset already %s, skipping API call",
                self._zone_name, preset_mode,
            )
            return

        await _check_bootstrap_reserve_or_raise(self.hass, self._zone_name, coordinator=self.coordinator)

        client = self.coordinator.api_client
        state = "AWAY" if preset_mode == PRESET_AWAY else "HOME"

        # The value Tado holds is the card's only while no earlier tap is in flight (a preset arms no optimistic window, update() rebuilds it).
        if not self._presets_in_flight:
            self._preset_baseline = self._attr_preset_mode
        self._attr_preset_mode = preset_mode
        self.async_write_ha_state()

        from .api_client import raising_network_errors

        api_success = False
        unknown_outcome = False
        typed_error: TadoAuthError | TadoRateLimitError | TadoBlockedError | None = None
        # Right before the try so the decrement in its finally always pairs with this increment.
        self._presets_in_flight += 1
        try:
            with raising_network_errors():
                async with asyncio.timeout(10):
                    api_success = await client.set_presence_lock(state)
        except TimeoutError:
            unknown_outcome = True
            _LOGGER.warning(
                "Climate Heating: %s preset-mode call timed out after 10s",
                self._zone_name,
            )
        except Exception as e:
            # Broad on purpose: any call failure reverts the preset and logs, and a typed rejection is dispatched after the revert.
            unknown_outcome = isinstance(e, aiohttp.ClientError)
            _LOGGER.warning(
                "Climate Heating: %s preset-mode call failed (%s)",
                self._zone_name, e,
            )
            if isinstance(e, (TadoAuthError, TadoRateLimitError, TadoBlockedError)):
                typed_error = e
        finally:
            self._presets_in_flight -= 1

        if api_success:
            self._preset_baseline = preset_mode
            # With no newer tap in flight, an earlier tap that reverted past this accepted value is corrected.
            if not self._presets_in_flight and self._attr_preset_mode != preset_mode:
                self._attr_preset_mode = preset_mode
                self.async_write_ha_state()
            _LOGGER.debug(
                "Climate Heating: %s preset mode set to %s",
                self._zone_name, preset_mode,
            )
            # Inject home_state locally so other climate entities pick
            # up the preset change immediately, even when Home State
            # Sync is disabled.
            await inject_presence_state(self.coordinator, state, locked=True)
            await async_trigger_immediate_refresh(self.hass, self.entity_id, "preset_mode_change")
        else:
            _LOGGER.warning(
                "Climate Heating: %s preset-mode change failed, reverted "
                "to previous state",
                self._zone_name,
            )
            # While a newer tap is in flight the card shows its value and only a card still showing this tap's goes back; the last tap out shows what Tado holds.
            if not self._presets_in_flight or self._attr_preset_mode == preset_mode:
                self._attr_preset_mode = self._preset_baseline
                self.async_write_ha_state()
            if unknown_outcome:
                self.coordinator.request_forced_fetch(FORCEABLE_FETCH_HOME_STATE)
                await async_trigger_immediate_refresh(self.hass, self.entity_id, "write_unknown")
            if typed_error is not None:
                dispatch_to_service_call(typed_error, self.coordinator.config_entry, self.hass)

    async def _write_via_cloud(
        self,
        *,
        power_on: bool,
        temperature: float | None,
        termination: dict[str, Any],
        hvac_mode: HVACMode,
        hvac_action: HVACAction,
        reason: str,
        capture_source: str | None = "set_temperature",
        sequence: int | None = None,
        gate_pending: bool = False,
        held_refused: bool = False,
    ) -> None:
        """Send a heating overlay write via the cloud API, with optimistic rollback.

        Shared by the three cloud-fallback call sites (temperature write,
        HEAT mode, OFF mode) and the EXPIRED-retry path
        (`_retry_expired_write_via_cloud`), which calls this when a
        HomeKit write's cloud-sync confirmation didn't land inside the
        optimistic window.
        """
        client = self.coordinator.api_client
        if power_on:
            setting: dict[str, Any] = {
                "type": "HEATING",
                "power": "ON",
                "temperature": {"celsius": temperature},
            }
        else:
            setting = {"type": "HEATING", "power": "OFF"}

        await api_call_with_rollback(
            self,
            client.set_zone_overlay(self._zone_id, setting, termination),
            hvac_mode=hvac_mode,
            hvac_action=hvac_action,
            target_temp=temperature,
            reason=reason,
            capture_source=capture_source,
            sequence=sequence,
            gate=self._cloud_gate(gate_pending=gate_pending, held_refused=held_refused),
        )
        self._last_write_source = "cloud"

    def _cloud_gate(self, *, gate_pending: bool, held_refused: bool) -> Callable[[], Awaitable[None]] | None:
        """The check a cloud write meets after its card was painted: a refused held put fails it, else the reserve gate the entry point skipped for a HomeKit route."""
        if held_refused:
            return self._refuse_held_write
        if gate_pending:
            return lambda: _check_bootstrap_reserve_or_raise(self.hass, self._zone_name, coordinator=self.coordinator)
        return None

    async def _refuse_held_write(self) -> None:
        # The cloud is not trusted while a change is held, so a bridge that refused it fails the write like a cloud failure and restores the card.
        raise cloud_write_failed(self)

    def _route_decision(self, zone_was_off: bool) -> RouteDecision:
        return decide_route(self.hass, self.coordinator, self._zone_id, entry_id=self._entry_id, zone_was_off=zone_was_off, allow_held=True)

    def _timed_termination(self) -> bool:
        """Whether the zone's overlay mode ends a change after a set time, which HomeKit cannot carry."""
        return get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id).get("type") == "TIMER"

    def _entry_route(self, zone_was_off: bool) -> Route:
        """The route at an entry point; a held write whose overlay mode is a timer is refused before anything is painted, like a duration timer."""
        route = self._route_decision(zone_was_off).route
        if route is Route.HOMEKIT_HELD and self._timed_termination():
            raise cloud_write_failed(self)
        return route

    def _held_on_send(self, zone_was_off: bool, temperature: float, *, mode_only: bool = False) -> Callable[[], Awaitable[PutOutcome]]:
        """The put for a held ON write: power and target together for an OFF zone (a lone target leaves the power at 0), else the one characteristic."""
        provider = self.coordinator.homekit_provider
        if zone_was_off:
            return lambda: provider.set_power_and_target(self._zone_id, temperature)
        if mode_only:
            return lambda: provider.set_hvac_mode(self._zone_id, 1)
        return lambda: provider.set_temperature(self._zone_id, temperature)

    async def _hold_via_homekit(
        self,
        send: Callable[[], Awaitable[PutOutcome]],
        *,
        kind: IntentKind,
        power_on: bool,
        temperature: float | None,
        termination: dict[str, Any],
        sequence: int | None,
        label: str,
    ) -> bool:
        """Send one HomeKit put while the cloud is down and hold the change; False means the bridge did not take it and the caller fails the write."""
        # Backstop for a mode that changed after the entry check: nothing is sent for a write that would run for the whole outage.
        if termination.get("type") == "TIMER":
            return False
        write_tracker = self.coordinator.write_health_for(self._zone_id)
        assert write_tracker is not None  # the caller's route decision was a held HomeKit write  # noqa: S101
        outcome = await self._send_homekit_put(
            send, raised_msg="Climate Heating: %s HomeKit write raised an exception while the cloud is down",
        )
        if outcome is PutOutcome.ACCEPTED:
            self.coordinator._homekit_write_successes += 1
        else:
            # A timed-out put may have landed and is held; it still counts against the bridge.
            write_tracker.record_failure()
            if outcome is not PutOutcome.AMBIGUOUS:
                _LOGGER.debug("Climate Heating: %s HomeKit %s write was not taken while the cloud is down", self._zone_name, label)
                return False
        # Cleared first: the put record keeps an earlier put's payload, and nothing is resent for a held change.
        stash_resend(self)
        mark_homekit_put(self)
        if outcome is PutOutcome.ACCEPTED:
            self._accept_homekit_put(sequence)
        # The own-OFF mark follows the power this put asked for, as a cloud write's does: ON clears an earlier OFF, OFF sets it.
        self.coordinator.record_own_power_write(self._zone_id, powered_off=not power_on)
        target: dict[str, Any] = {"power": "ON" if power_on else "OFF"}
        if power_on:
            target["temperature"] = temperature
        hold_locally(self, kind=kind, target=target, termination=termination, ambiguous=outcome is PutOutcome.AMBIGUOUS)
        _LOGGER.info(
            "Climate Heating: %s %s sent over HomeKit and held while the Tado cloud state is %s",
            self._zone_name, label, self.coordinator.cloud_health.state.value,
        )
        return True

    @staticmethod
    def _resend_value(payload: dict[str, Any]) -> tuple[str, float | None]:
        """The power and target a heating resend payload asks for."""
        return ("ON", payload["temperature"]) if payload["power_on"] else ("OFF", None)

    async def _retry_expired_write_via_cloud(
        self, payload: dict[str, Any], *, quota_rearmed: bool = False, entry: ResendEntry | None = None,
    ) -> None:
        # Retry a HomeKit write via cloud PUT after optimistic window expired.
        async def send_resend() -> None:
            termination = get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id)
            power_on = payload["power_on"]
            temperature = payload["temperature"]
            if power_on:
                hvac_mode = HVACMode.HEAT
                hvac_action = self._calculate_hvac_action(target_temp=temperature, hvac_mode=HVACMode.HEAT)
                reason = "HomeKit write confirmation expired, retrying via cloud"
                capture_source = "set_temperature"
            else:
                hvac_mode, hvac_action = HVACMode.OFF, HVACAction.OFF
                reason = "HomeKit OFF write confirmation expired, retrying via cloud"
                capture_source = "set_hvac_mode"
            await self._write_via_cloud(
                power_on=power_on,
                temperature=temperature,
                termination=termination,
                hvac_mode=hvac_mode,
                hvac_action=hvac_action,
                reason=reason,
                capture_source=capture_source,
            )

        await self._run_expired_resend(
            self._LOG_LABEL,
            lambda rearmed, held: self._retry_expired_write_via_cloud(payload, quota_rearmed=rearmed, entry=held),
            quota_rearmed=quota_rearmed,
            send=send_resend,
            value=self._resend_value(payload),
            entry=entry,
        )

    async def _execute_set_temp_ordered(self, *args: Any, **kwargs: Any) -> None:
        """Send the target write as one ordered step: the HomeKit attempt and its cloud fallback hold the zone's slot together."""
        async with ordered_zone_write(self.coordinator, self._zone_id):
            await self._execute_set_temp_api(*args, **kwargs)

    async def _execute_set_temp_api(
        self,
        client: TadoApiClient,
        setting: dict[str, Any],
        termination: dict[str, Any],
        temperature: float,
        *,
        zone_was_off: bool = False,
        raise_on_failure: bool = False,
        sequence: int | None = None,
        gate_pending: bool = False,
    ) -> None:
        """Execute set_zone_overlay API call with rollback on failure."""
        # Local-first: a HomeKit route tries the bridge before the cloud API
        local_success = False
        held_refused = False
        route = self._route_decision(zone_was_off).route
        if route is Route.HOMEKIT:
            local_success = await self._try_homekit_write(
                lambda: self.coordinator.homekit_provider.set_temperature(self._zone_id, temperature),
                raised_msg="Climate Heating: %s HomeKit write raised an exception, falling back to cloud API",
                failed_msg="Climate Heating: %s HomeKit write failed, falling back to cloud API",
            )
        elif route is Route.HOMEKIT_HELD:
            if await self._hold_via_homekit(
                self._held_on_send(zone_was_off, temperature), kind=IntentKind.TEMPERATURE, power_on=True, temperature=temperature, termination=termination,
                sequence=sequence, label="target",
            ):
                await self._async_feed_heating_cycle(temperature, self._attr_native_current_temperature)
                return
            held_refused = True

        if local_success:
            payload = {"power_on": True, "temperature": temperature}
            self.coordinator.record_homekit_write_saved(self._zone_id, payload_to_time(self.coordinator, self._zone_id, payload))
            stash_resend(self, payload=payload)
            _LOGGER.debug(
                "Climate Heating: %s target set to %s°C via HomeKit",
                self._zone_name, temperature,
            )
            mark_homekit_put(self)
            self._accept_homekit_put(sequence)
            await self._async_feed_heating_cycle(temperature, self._attr_native_current_temperature)
            # Schedule cloud verification to confirm write reached Tado server
            self._schedule_cloud_verification()
            return

        # Cloud fallback
        try:
            await self._write_via_cloud(
                power_on=True,
                temperature=temperature,
                termination=termination,
                hvac_mode=HVACMode.HEAT,
                hvac_action=self._calculate_hvac_action(target_temp=temperature, hvac_mode=HVACMode.HEAT),
                reason=f"Set temperature to {temperature}°C",
                capture_source="set_temperature",
                sequence=sequence,
                gate_pending=gate_pending,
                held_refused=held_refused,
            )
        except HomeAssistantError:
            if raise_on_failure:
                raise
            return

        await self._async_feed_heating_cycle(temperature, self._attr_native_current_temperature)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        # Handle hvac_mode without temperature (delegate to set_hvac_mode)
        if hvac_mode is not None and temperature is None:
            await self.async_set_hvac_mode(hvac_mode)
            return

        # Handle OFF/AUTO mode specially (no temperature needed)
        if hvac_mode in (HVACMode.OFF, HVACMode.AUTO):
            await self.async_set_hvac_mode(hvac_mode)
            return

        if temperature is None:
            return

        # Action Guard: skip if temp + mode already match current state
        if ActionGuard.should_skip_temperature(
            temperature, self._attr_native_target_temperature,
            HVACMode.HEAT, self._attr_hvac_mode,
            optimistic_active=self._optimistic_sequence is not None or held_data_unconfirmed(self.coordinator, self._zone_id) or zone_owes_change(self),
        ):
            _LOGGER.debug(
                "Climate Heating: %s already at %s°C, skipping API call",
                self._zone_name, temperature,
            )
            return

        # Read before the gate: the route decides whether the gate runs now or only if the bridge fails.
        zone_was_off = not zone_power_is_on(self.coordinator, self._zone_id)
        gate_pending = self._entry_route(zone_was_off) is not Route.CLOUD
        if not gate_pending:
            await _check_bootstrap_reserve_or_raise(self.hass, self._zone_name, coordinator=self.coordinator)

        # Before anything is assigned: a first write with no poll yet has to go back to the card as it is now.
        self._seed_accepted()
        self._attr_native_target_temperature = temperature
        self._attr_hvac_mode = HVACMode.HEAT
        self._overlay_type = "MANUAL"
        new_hvac_action = self._calculate_hvac_action(target_temp=temperature)
        self._attr_hvac_action = new_hvac_action
        await set_optimistic_fields(
            self, self.coordinator,
            source="cloud",
            expected={
                "hvac_mode": HVACMode.HEAT,
                "hvac_action": new_hvac_action,
                "target_temperature": temperature,
            },
        )
        _LOGGER.debug(
            "Climate Heating: %s optimistic update, target=%s°C, "
            "action=%s",
            self._zone_name, temperature, self._attr_hvac_action,
        )
        self.async_write_ha_state()
        # Taken at the stamp: the HomeKit await below can let a newer write stamp first.
        stamped_sequence = self._optimistic_sequence

        # Build API call parameters
        client = self.coordinator.api_client
        setting = {
            "type": "HEATING",
            "power": "ON",
            "temperature": {"celsius": temperature},
        }
        termination = get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id)

        # Smart Actions debounce
        debounce_window = self.coordinator.config_manager.get_smart_actions_debounce_seconds()
        if debounce_window > 0:
            async def _execute_api_call() -> None:
                """Execute the debounced API call."""
                await self._run_queued_write(
                    lambda: self._execute_set_temp_ordered(
                        client, setting, termination, temperature,
                        zone_was_off=zone_was_off, sequence=stamped_sequence, gate_pending=gate_pending,
                    ),
                )

            self._announce_queued_write(CLIMATE_STATE_NAMES, stamped_sequence)
            await self.coordinator.action_debouncer.debounce(
                self._zone_id, _execute_api_call, window=float(debounce_window),
            )
        else:
            await self._execute_set_temp_ordered(
                client, setting, termination, temperature,
                zone_was_off=zone_was_off, raise_on_failure=True,
                sequence=stamped_sequence, gate_pending=gate_pending,
            )

    async def _try_homekit_hvac_mode_write(
        self, mode_value: int, mode_label: str, *, zone_was_off: bool = False,
    ) -> bool:
        """Write the target-heating-state characteristic when the route is HomeKit; False means fall back to cloud."""
        if self._route_decision(zone_was_off).route is not Route.HOMEKIT:
            return False

        return await self._try_homekit_write(
            lambda: self.coordinator.homekit_provider.set_hvac_mode(self._zone_id, mode_value),
            raised_msg=f"Climate Heating: %s HomeKit {mode_label} write raised an exception, falling back to cloud",
            failed_msg=f"Climate Heating: %s HomeKit {mode_label} write failed, falling back to cloud API",
        )

    def _resolve_heat_target(self) -> float:
        """Pick the HEAT target; an OFF zone shows the frost target, so HEAT resolves to the cached schedule, else 20."""
        if self._attr_hvac_mode == HVACMode.OFF:
            scheduled = get_current_schedule_target(
                self._zone_id, data_loader=self.coordinator.data_loader,
            )
            if scheduled is None:
                _LOGGER.debug(
                    "Climate Heating: %s has no usable schedule target, HEAT from an off zone uses 20°C",
                    self._zone_name,
                )
                return 20.0
            return scheduled
        return self._attr_native_target_temperature or 20.0

    def _heat_target_to_record(self, card_target: float, *, zone_was_off: bool) -> float:
        """A HEAT on a running room keeps the room's newest own target: the card's while a write is pending, else the owed one, else the thermostat's reading on a held route."""
        if zone_was_off or (self._optimistic_sequence is not None and self._attr_hvac_mode != HVACMode.OFF):
            return card_target
        if (owed_target := owed_held_target(self.coordinator, self._zone_id, ambiguous_ok=True)) is not None:
            return owed_target
        if self._route_decision(zone_was_off).route is not Route.HOMEKIT_HELD:
            return card_target
        provider = self.coordinator.homekit_provider
        reading = provider.get_target_temperature(self._zone_id)[0] if provider is not None and provider.is_connected else None
        return float(reading) if isinstance(reading, int | float) and not isinstance(reading, bool) else card_target

    async def _paint_hvac_mode_before_put(
        self, hvac_mode: HVACMode, hvac_action: HVACAction, target_temp: float, *, zone_was_off: bool,
    ) -> int | None:
        """Arm and paint a HEAT or OFF card at the tap when the bridge looks usable now (re-checked inside the slot), like the temperature path; returns the hold, else None."""
        if self._route_decision(zone_was_off).route is Route.CLOUD:
            return None
        # Before anything is assigned: a failure has to go back to the card as it is now.
        self._seed_accepted()
        self._attr_hvac_mode = hvac_mode
        self._attr_hvac_action = hvac_action
        self._overlay_type = "MANUAL"
        self._attr_native_target_temperature = target_temp
        await set_optimistic_fields(
            self, self.coordinator,
            source="cloud",
            expected={"hvac_mode": hvac_mode, "hvac_action": hvac_action, "target_temperature": target_temp},
        )
        self.async_write_ha_state()
        sequence: int | None = self._optimistic_sequence
        return sequence

    async def _apply_optimistic_hvac_mode_write(
        self, hvac_mode: HVACMode, hvac_action: HVACAction,
        *, payload: dict[str, Any], target_temp: float | None = None, sequence: int | None = None,
    ) -> None:
        """Apply the optimistic mode the cloud path already applies, so the card updates now; with a painted `sequence` only the put is recorded."""
        if sequence is not None:
            mark_homekit_put(self, payload=payload)
            self._accept_homekit_put(sequence)
            self._schedule_cloud_verification()
            return
        self._attr_hvac_mode = hvac_mode
        self._attr_hvac_action = hvac_action
        self._overlay_type = "MANUAL"
        expected: dict[str, Any] = {"hvac_mode": hvac_mode, "hvac_action": hvac_action}
        if target_temp is not None:
            self._attr_native_target_temperature = target_temp
            expected["target_temperature"] = target_temp
        await set_optimistic_fields(
            self, self.coordinator,
            source="homekit",
            expected=expected,
        )
        self.async_write_ha_state()
        mark_homekit_put(self, payload=payload)
        self._accept_homekit_put(self._optimistic_sequence)
        self._schedule_cloud_verification()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new HVAC mode."""
        cancelled: dict[str, Any] | None = None
        tap_stamp = self._queued_tap_stamp()
        # Read before the AUTO below ends the entry: a change still owed is not in the card's data, so no guard may skip on it.
        owed_before = zone_owes_change(self)
        # Before the ActionGuard: a card still showing OFF must not skip the OFF and let a pending HEAT fire.
        if hvac_mode in (HVACMode.OFF, HVACMode.AUTO):
            cancelled = self._cancel_pending_write()
        if hvac_mode == HVACMode.AUTO:
            # Before the ActionGuard and the reserve check: an AUTO the reserve check refuses must still end the debt of a held change.
            supersede_owed(self)
        try:
            # The API reports overlayType MANUAL for any overlay whatever its termination.
            skip_for_auto = hvac_mode == HVACMode.AUTO and self._overlay_type is not None
            # Action Guard: skip if mode already matches current state
            if not skip_for_auto and ActionGuard.should_skip_hvac_mode(
                hvac_mode, self._attr_hvac_mode,
                optimistic_active=self._optimistic_sequence is not None or held_data_unconfirmed(self.coordinator, self._zone_id) or owed_before,
            ):
                _LOGGER.debug(
                    "Climate Heating: %s already in %s mode, skipping API call",
                    self._zone_name, hvac_mode,
                )
                return

            # Read before the gate: the route decides whether the gate runs now or only if the bridge fails (AUTO is always cloud).
            zone_was_off = hvac_mode == HVACMode.HEAT and not zone_power_is_on(self.coordinator, self._zone_id)
            gate_pending = hvac_mode in (HVACMode.HEAT, HVACMode.OFF) and self._entry_route(zone_was_off) is not Route.CLOUD
            if not gate_pending:
                await _check_bootstrap_reserve_or_raise(self.hass, self._zone_name, coordinator=self.coordinator)
        except HomeAssistantError:
            # The cancelled tap will never be sent, so the card goes back to what Tado holds.
            self._restore_after_refusal(cancelled, tap_stamp)
            raise

        client = self.coordinator.api_client

        # When the bridge is tried, HEAT and OFF hold the slot across the put and its cloud fallback as one ordered step; otherwise run_cloud_write takes it.
        if hvac_mode == HVACMode.HEAT:
            temp = self._resolve_heat_target()
            temp = self._heat_target_to_record(temp, zone_was_off=zone_was_off)
            # Painted at the tap when the bridge is tried; a cloud-only HEAT arms inside run_cloud_write as before.
            painted = await self._paint_hvac_mode_before_put(
                HVACMode.HEAT, self._calculate_hvac_action(target_temp=temp, hvac_mode=HVACMode.HEAT), temp, zone_was_off=zone_was_off,
            )
            async with ordered_zone_write(self.coordinator, self._zone_id if painted is not None else ""):
                held_refused = False
                local_success = False
                if self._route_decision(zone_was_off).route is Route.HOMEKIT_HELD:
                    sent = await self._hold_via_homekit(
                        self._held_on_send(zone_was_off, temp, mode_only=True), kind=IntentKind.POWER_ON, power_on=True,
                        temperature=temp, termination=get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id),
                        sequence=painted, label="HEAT",
                    )
                    if sent:
                        return
                    held_refused = True
                else:
                    local_success = await self._try_homekit_hvac_mode_write(1, "HEAT", zone_was_off=zone_was_off)
                if local_success:
                    payload = {"power_on": True, "temperature": temp}
                    self.coordinator.record_homekit_write_saved(self._zone_id, payload_to_time(self.coordinator, self._zone_id, payload))
                    self.coordinator.record_own_power_write(self._zone_id, powered_off=False)
                    _LOGGER.debug(
                        "Climate Heating: %s set to HEAT via HomeKit",
                        self._zone_name,
                    )

                    self._attr_hvac_mode = HVACMode.HEAT
                    new_hvac_action = self._calculate_hvac_action(target_temp=temp)
                    await self._apply_optimistic_hvac_mode_write(
                        HVACMode.HEAT, new_hvac_action, payload=payload, target_temp=temp, sequence=painted,
                    )
                    return

                # Cloud fallback
                termination = get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id)
                new_hvac_action = self._calculate_hvac_action(target_temp=temp, hvac_mode=HVACMode.HEAT)

                async def _send_heat() -> None:
                    await self._write_via_cloud(
                        power_on=True,
                        temperature=temp,
                        termination=termination,
                        hvac_mode=HVACMode.HEAT,
                        hvac_action=new_hvac_action,
                        reason=f"Set HEAT mode at {temp}°C",
                        capture_source="set_hvac_mode",
                        sequence=painted,
                        gate_pending=gate_pending,
                        held_refused=held_refused,
                    )

                debounce_window = self.coordinator.config_manager.get_smart_actions_debounce_seconds()
                # A refused held put fails the tap now; there is nothing to queue.
                if zone_was_off and debounce_window > 0 and not held_refused:
                    async def _send_heat_debounced() -> None:
                        try:
                            await self._run_queued_write(lambda: _send_heat())
                        except HomeAssistantError:
                            # Same as a debounced set_temperature: the rollback already ran, the caller is gone.
                            return

                    # The newest intent supersedes an older unconfirmed HomeKit write, so its resend cannot undo the pending HEAT.
                    clear_homekit_write(self)
                    # The pending HEAT stamps no hold, so a refused action has nothing of its own to restore.
                    self._seed_accepted()
                    self._announce_queued_write(CLIMATE_STATE_NAMES, None)
                    await self.coordinator.action_debouncer.debounce(
                        self._zone_id, _send_heat_debounced, window=float(debounce_window),
                    )
                else:
                    await _send_heat()

        elif hvac_mode == HVACMode.OFF:
            off_painted = await self._paint_hvac_mode_before_put(
                HVACMode.OFF, HVACAction.OFF, OPEN_WINDOW_DEFAULT_TEMP, zone_was_off=False,
            )
            async with ordered_zone_write(self.coordinator, self._zone_id if off_painted is not None else ""):
                held_refused = False
                local_success = False
                if self._route_decision(False).route is Route.HOMEKIT_HELD:
                    provider = self.coordinator.homekit_provider
                    if await self._hold_via_homekit(
                        lambda: provider.set_hvac_mode(self._zone_id, 0), kind=IntentKind.POWER_OFF, power_on=False,
                        temperature=None, termination=get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id),
                        sequence=off_painted, label="OFF",
                    ):
                        return
                    held_refused = True
                else:
                    local_success = await self._try_homekit_hvac_mode_write(0, "OFF")
                if local_success:
                    payload = {"power_on": False, "temperature": OPEN_WINDOW_DEFAULT_TEMP}
                    self.coordinator.record_homekit_write_saved(self._zone_id, payload)
                    self.coordinator.record_own_power_write(self._zone_id, powered_off=True)
                    _LOGGER.debug(
                        "Climate Heating: %s set to OFF via HomeKit",
                        self._zone_name,
                    )

                    await self._apply_optimistic_hvac_mode_write(
                        HVACMode.OFF, HVACAction.OFF, payload=payload, target_temp=OPEN_WINDOW_DEFAULT_TEMP, sequence=off_painted,
                    )
                    return

                # Cloud fallback
                termination = get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id)
                await self._write_via_cloud(
                    power_on=False,
                    temperature=OPEN_WINDOW_DEFAULT_TEMP,
                    termination=termination,
                    hvac_mode=HVACMode.OFF,
                    hvac_action=HVACAction.OFF,
                    reason="Set OFF mode",
                    capture_source="set_hvac_mode",
                    sequence=off_painted,
                    gate_pending=gate_pending,
                    held_refused=held_refused,
                )

        elif hvac_mode == HVACMode.AUTO:
            await api_call_with_rollback(
                self,
                client.delete_zone_overlay(self._zone_id),
                hvac_mode=HVACMode.AUTO,
                hvac_action=HVACAction.IDLE,
                overlay_type=None,
                reason="Set AUTO mode (deleted overlay)",
            )
            self._last_write_source = "cloud"

    async def async_set_timer(
        self, *, temperature: float, duration_minutes: int | None = None, overlay: str | None = None,
    ) -> bool:
        """Set temperature with timer or overlay type."""
        tap_stamp = self._queued_tap_stamp()
        cancelled = self._cancel_pending_write()
        # A timer never goes over HomeKit while the cloud is healthy; only a held write meets the reserve gate later, if its put fails.
        zone_was_off = not zone_power_is_on(self.coordinator, self._zone_id)
        held = self._route_decision(zone_was_off).route is Route.HOMEKIT_HELD
        if not held:
            try:
                await _check_bootstrap_reserve_or_raise(self.hass, self._zone_name, coordinator=self.coordinator)
            except HomeAssistantError:
                # The cancelled tap will never be sent, so the card goes back to what Tado holds.
                self._restore_after_refusal(cancelled, tap_stamp)
                raise

        # Capture current state before overlay (state restoration)
        client = self.coordinator.api_client

        setting = {
            "type": "HEATING",
            "power": "ON",
            "temperature": {"celsius": temperature},
        }

        termination = build_timer_termination(
            duration_minutes=duration_minutes,
            overlay=overlay,
            hass=self.hass,
            zone_id=self._zone_id,
            entry_id=self._entry_id,
        )
        if duration_minutes:
            term_desc = f"for {duration_minutes} minutes"
        elif overlay and overlay.upper() == "NEXT_TIME_BLOCK":
            term_desc = "until next schedule block"
        else:
            term_desc = "manually"

        hvac_action = self._calculate_hvac_action(target_temp=temperature, hvac_mode=HVACMode.HEAT)
        painted = None
        if held and termination.get("type") == "TIMER":
            # HomeKit carries no end time, so a timed overlay would run for the whole outage: it is refused before the card moves, as a failed cloud timer is today.
            self._restore_after_refusal(cancelled, tap_stamp)
            return False
        if held:
            painted = await self._paint_hvac_mode_before_put(HVACMode.HEAT, hvac_action, temperature, zone_was_off=zone_was_off)
        async with ordered_zone_write(self.coordinator, self._zone_id if painted is not None else ""):
            gate = None
            if held:
                held_now = self._route_decision(zone_was_off).route is Route.HOMEKIT_HELD
                sent = held_now and await self._hold_via_homekit(
                    self._held_on_send(zone_was_off, temperature), kind=IntentKind.TIMER, power_on=True, temperature=temperature,
                    termination=termination, sequence=painted, label="timer",
                )
                if sent:
                    return True
                gate = self._cloud_gate(gate_pending=True, held_refused=held_now)
            if not await self._async_timer_result(
                api_call_with_rollback(
                    self,
                    client.set_zone_overlay(self._zone_id, setting, termination),
                    hvac_mode=HVACMode.HEAT,
                    hvac_action=hvac_action,
                    target_temp=temperature,
                    reason=f"set timer at {temperature}°C {term_desc}",
                    capture_source="set_timer",
                    sequence=painted,
                    gate=gate,
                ),
            ):
                return False
        self._last_write_source = "cloud"
        return True
