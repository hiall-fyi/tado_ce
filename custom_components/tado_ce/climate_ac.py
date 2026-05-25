"""Tado CE air-conditioning climate entity — AC mode + fan + swing control.

One entity per AIR_CONDITIONING zone. Carries the same optimistic-
update + rollback pattern as the heating entity but adds fan-mode,
swing-mode, and per-mode capability handling driven by the cloud
`/capabilities` endpoint. Capability shape varies per AC unit, so
mode lists are built dynamically from cached capabilities rather
than hardcoded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ATTR_HVAC_MODE, ClimateEntity  # type: ignore[attr-defined]
from homeassistant.components.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .climate_helpers import (
    api_call_with_rollback,
    read_external_sensor,
    setup_climate_external_sensor_subscription,
    unsubscribe_external_sensors,
)
from .const import DOMAIN, SIGNAL_HOMEKIT_UPDATE
from .device_manager import get_zone_device_info
from .entity_registry import ENTITY_REGISTRY
from .format_helpers import (
    format_overlay_type as _format_overlay_type,
)
from .format_helpers import (
    format_zone_type as _format_zone_type,
)
from .helpers import (
    async_trigger_immediate_refresh,
    build_timer_termination,
    get_zone_overlay_termination,
    get_zone_state,
)
from .optimistic_helpers import (
    OptimisticUpdateResult,
    clear_optimistic_state,
    resolve_optimistic_update,
    set_optimistic_fields,
)
from .ratelimit import async_check_bootstrap_reserve_or_raise as _check_bootstrap_reserve_or_raise
from .schedule_helpers import get_current_schedule_target
from .write_optimizer import ActionGuard

if TYPE_CHECKING:
    from collections.abc import Callable

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AC HVAC / Fan mode mappings
# ---------------------------------------------------------------------------

TADO_TO_HA_HVAC_MODE = {
    "COOL": HVACMode.COOL,
    "HEAT": HVACMode.HEAT,
    "DRY": HVACMode.DRY,
    "FAN": HVACMode.FAN_ONLY,
    "AUTO": HVACMode.HEAT_COOL,
}

HA_TO_TADO_HVAC_MODE = {v: k for k, v in TADO_TO_HA_HVAC_MODE.items()}

# HomeKit Target Heating State → HA HVACMode mapping for AC zones
# 0=Off, 1=Heat, 2=Cool, 3=Auto (maps to HEAT_COOL for AC)
_HOMEKIT_TARGET_STATE_TO_HVAC_AC: dict[int, HVACMode] = {
    0: HVACMode.OFF,
    1: HVACMode.HEAT,
    2: HVACMode.COOL,
    3: HVACMode.HEAT_COOL,
}

# Fan level mapping - Tado uses SILENT, LEVEL1-5, AUTO
# Map to HA's limited fan modes (auto, low, medium, high)
TADO_TO_HA_FAN = {
    "AUTO": FAN_AUTO,
    "SILENT": FAN_LOW,
    "LEVEL1": FAN_LOW,
    "LEVEL2": FAN_LOW,
    "LEVEL3": FAN_MEDIUM,
    "LEVEL4": FAN_HIGH,
    "LEVEL5": FAN_HIGH,
    # Legacy mappings
    "HIGH": FAN_HIGH,
    "MIDDLE": FAN_MEDIUM,
    "LOW": FAN_LOW,
}

HA_TO_TADO_FAN = {
    FAN_AUTO: "AUTO",
    FAN_LOW: "LEVEL1",
    FAN_MEDIUM: "LEVEL3",
    FAN_HIGH: "LEVEL5",
}

# Canonical ordering of Tado fan levels (quietest → loudest)
_TADO_FAN_ORDER = [
    "SILENT",
    "LOW",
    "LEVEL1",
    "ONE",
    "MIDDLE",
    "LEVEL2",
    "TWO",
    "LEVEL3",
    "THREE",
    "LEVEL4",
    "FOUR",
    "HIGH",
    "LEVEL5",
]


def _assign_fan_bucket(index: int, low_end: int, high_start: int) -> str:
    """Assign a fan level index to a low/medium/high HA bucket."""
    if index < low_end:
        return FAN_LOW
    if index >= high_start:
        return FAN_HIGH
    return FAN_MEDIUM


def build_fan_mapping(fan_levels: set[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build bidirectional fan level mapping from actual AC capabilities.

    Strategy:
      1. AUTO always maps to FAN_AUTO
      2. SILENT always maps to FAN_LOW (quietest)
      3. Remaining levels sorted and divided evenly into low/medium/high buckets
      4. ha→tado picks the HIGHEST tado level in each bucket
    """
    tado_to_ha: dict[str, str] = {}
    ha_to_tado: dict[str, str] = {}

    if "AUTO" in fan_levels:
        tado_to_ha["AUTO"] = FAN_AUTO
        ha_to_tado[FAN_AUTO] = "AUTO"

    if "SILENT" in fan_levels:
        tado_to_ha["SILENT"] = FAN_LOW

    other_levels = sorted(
        [f for f in fan_levels if f not in ("AUTO", "SILENT")],
        key=lambda x: _TADO_FAN_ORDER.index(x) if x in _TADO_FAN_ORDER else 99,
    )

    n = len(other_levels)
    if n == 0:
        if "SILENT" in fan_levels:
            ha_to_tado[FAN_LOW] = "SILENT"
        return tado_to_ha, ha_to_tado

    low_end = max(1, n // 3)
    high_start = n - max(1, n // 3)

    for i, level in enumerate(other_levels):
        tado_to_ha[level] = _assign_fan_bucket(i, low_end, high_start)

    for ha_mode in [FAN_LOW, FAN_MEDIUM, FAN_HIGH]:
        candidates = [lvl for lvl, ha in tado_to_ha.items() if ha == ha_mode and lvl not in ("AUTO", "SILENT")]
        if candidates:
            ha_to_tado[ha_mode] = candidates[-1]

    if FAN_LOW not in ha_to_tado and "SILENT" in fan_levels:
        ha_to_tado[FAN_LOW] = "SILENT"

    return tado_to_ha, ha_to_tado


def _build_fan_modes(
    ac_caps: dict[str, Any], zone_id: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Build fan mode mappings from AC capabilities.

    Returns (tado_to_ha, ha_to_tado, fan_modes_list).
    """
    fan_levels: set[str] = set()
    for mode_caps in ac_caps.values():
        if isinstance(mode_caps, dict):
            if "fanLevel" in mode_caps:
                fan_levels.update(mode_caps["fanLevel"])
            elif "fanSpeeds" in mode_caps:
                fan_levels.update(mode_caps["fanSpeeds"])

    if fan_levels:
        tado_to_ha, ha_to_tado = build_fan_mapping(fan_levels)
        fan_modes = list(dict.fromkeys(tado_to_ha.values()))
        _LOGGER.debug(
            "Climate AC: zone %s fan modes %s (from %s) — ha→tado %s",
            zone_id, fan_modes, fan_levels, ha_to_tado,
        )
        return tado_to_ha, ha_to_tado, fan_modes

    return dict(TADO_TO_HA_FAN), dict(HA_TO_TADO_FAN), [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]


def _build_swing_axis_modes(
    ac_caps: dict[str, Any],
    axis: str,
    zone_id: str,
) -> list[str] | None:
    """Build the swing-mode list for one axis from the union of supported values across all AC modes.

    `axis` is `"verticalSwing"` or `"horizontalSwing"`. Returns `None`
    when the axis isn't supported by any mode — the caller uses that
    to decide whether to advertise the corresponding `SWING_MODE` /
    `SWING_HORIZONTAL_MODE` feature flag.
    """
    all_values: set[str] = set()
    for mode_caps in ac_caps.values():
        if isinstance(mode_caps, dict) and axis in mode_caps:
            all_values.update(mode_caps[axis])
    if not all_values:
        return None
    result = sorted(v.lower() for v in all_values)
    _LOGGER.debug(
        "Climate AC: zone %s %s modes %s (raw=%s)",
        zone_id, axis, result, all_values,
    )
    return result


class TadoACClimate(CoordinatorEntity["TadoDataUpdateCoordinator"], ClimateEntity, RestoreEntity):
    """Tado CE Air Conditioning Climate Entity."""

    _attr_has_entity_name = True

    @staticmethod
    def _build_hvac_modes(ac_caps: dict[str, Any]) -> list[HVACMode]:
        """Build HVAC modes list from AC capabilities."""
        modes: list[HVACMode] = [HVACMode.OFF]
        for tado_mode in ["COOL", "HEAT", "DRY", "FAN"]:
            if tado_mode in ac_caps:
                ha_mode = TADO_TO_HA_HVAC_MODE.get(tado_mode)
                if ha_mode and ha_mode not in modes:
                    modes.append(ha_mode)
        if "AUTO" in ac_caps and HVACMode.HEAT_COOL not in modes:
            modes.append(HVACMode.HEAT_COOL)
        return modes

    @staticmethod
    def _extract_temp_range(ac_caps: dict[str, Any]) -> tuple[float, float, float]:
        """Extract min/max/step temperature from AC capabilities."""
        for mode in ["COOL", "HEAT", "AUTO", "DRY"]:
            if mode in ac_caps and "temperatures" in ac_caps[mode]:
                temp_caps = ac_caps[mode]["temperatures"].get("celsius") or {}
                return temp_caps.get("min", 16), temp_caps.get("max", 30), temp_caps.get("step", 1)
        return 16, 30, 1

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        capabilities: dict[str, Any],
        home_id: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = home_id
        self._capabilities = capabilities
        self._entry_id = coordinator.config_entry.entry_id
        self._entity_type = "climate_ac"

        _meta = ENTITY_REGISTRY["climate_ac"]
        self._attr_name = None
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "AIR_CONDITIONING", home_id)

        ac_caps = capabilities.get("ac_capabilities") or {}

        # Build supported features
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        )
        has_fan = any(
            (ac_caps.get(mode) or {}).get("fanLevel") or (ac_caps.get(mode) or {}).get("fanSpeeds")
            for mode in ["COOL", "HEAT", "DRY", "FAN", "AUTO"]
        )
        if has_fan:
            features |= ClimateEntityFeature.FAN_MODE
        v_modes = _build_swing_axis_modes(ac_caps, "verticalSwing", zone_id)
        h_modes = _build_swing_axis_modes(ac_caps, "horizontalSwing", zone_id)
        if v_modes is not None:
            features |= ClimateEntityFeature.SWING_MODE
        if h_modes is not None:
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
        self._attr_supported_features = features

        self._attr_hvac_modes = self._build_hvac_modes(ac_caps)
        _LOGGER.debug(
            "Climate AC: zone %s HVAC modes %s",
            zone_id, self._attr_hvac_modes,
        )

        self._tado_to_ha_fan, self._ha_to_tado_fan, self._attr_fan_modes = _build_fan_modes(ac_caps, zone_id)
        self._attr_swing_modes = v_modes
        self._attr_swing_horizontal_modes = h_modes

        self._attr_min_temp, self._attr_max_temp, self._attr_target_temperature_step = self._extract_temp_range(ac_caps)

        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_hvac_mode = None
        self._attr_hvac_action = None
        self._attr_fan_mode = self._attr_fan_modes[0] if self._attr_fan_modes else None
        self._attr_swing_mode = v_modes[0] if v_modes else None
        self._attr_swing_horizontal_mode = h_modes[0] if h_modes else None
        self._attr_available = False
        self._attr_current_humidity = None

        self._overlay_type = None
        self._ac_power_percentage = None

        self._temperature_source = "cloud"
        self._humidity_source = "cloud"
        self._external_temp_sensor = ""
        self._external_humidity_sensor = ""
        self._last_write_source = ""

        self._optimistic_set_at: float | None = None
        self._optimistic_sequence: int | None = None
        self._optimistic_preserved: dict[str, Any] | None = None
        self._expected_hvac_mode: HVACMode | None = None
        self._expected_hvac_action: HVACAction | None = None
        self._expected_target_temperature: float | None = None

        # Unsubscribe callback for zones_updated signal
        self._unsub_zones_updated = None

        # Unsubscribe callback for zone config changes
        self._unsub_zone_config = None

        # Unsubscribe callback for AC capabilities updated signal
        self._unsub_ac_caps = None

        # Unsubscribe callbacks for external sensor state change listeners
        self._unsub_external_sensors: list[CALLBACK_TYPE] = []

        # Unsubscribe callback for HomeKit dispatcher signal
        self._unsub_homekit_signal: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Public API (TadoZoneEntity Protocol — see entity_types.py)
    # ------------------------------------------------------------------

    @property
    def zone_id(self) -> str:
        """Return the Tado zone ID as a string."""
        return str(self._zone_id)

    @property
    def zone_type(self) -> str:
        """Return the zone type — always AIR_CONDITIONING for this entity."""
        return "AIR_CONDITIONING"

    @property
    def entity_type(self) -> str:
        """Return the entity type tag for state-capture routing."""
        return self._entity_type

    def _calculate_hvac_action(self, hvac_mode: HVACMode | None = None, ac_power_on: bool | None = None) -> HVACAction:
        """Resolve hvac_action for the AC zone.

        Priority: explicit OFF mode → optimistic-window expected action →
        API-confirmed power-off → mode-driven default. `ac_power_on=None`
        means caller couldn't read the API state and we assume ON
        (typical of optimistic-window calls).
        """
        mode = hvac_mode if hvac_mode is not None else self._attr_hvac_mode

        if mode == HVACMode.OFF:
            return HVACAction.OFF

        if self._expected_hvac_action is not None:
            return self._expected_hvac_action

        if ac_power_on is False:
            return HVACAction.IDLE

        if mode == HVACMode.COOL:
            return HVACAction.COOLING
        if mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if mode == HVACMode.DRY:
            return HVACAction.DRYING
        if mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        # HEAT_COOL maps to Tado AUTO — AC decides per zone, no committed
        # action yet. Default to IDLE so the card doesn't lie.
        if mode == HVACMode.HEAT_COOL:
            return HVACAction.IDLE

        return HVACAction.IDLE

    async def async_added_to_hass(self) -> None:
        """Restore last target temperature and wire HomeKit / config listeners.

        CoordinatorEntity already subscribes to the coordinator, so we
        only attach the zone-config and HomeKit-update listeners here.
        """
        await super().async_added_to_hass()

        # Restore last known target temperature across HA restarts
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get(ATTR_TEMPERATURE) is not None:
            self._attr_target_temperature = last_state.attributes[ATTR_TEMPERATURE]
            _LOGGER.debug(
                "Climate AC: %s restored target %s°C from previous HA state",
                self._zone_name,
                self._attr_target_temperature,
            )
        elif self._attr_target_temperature is None:
            # First install / no previous state — default to 24°C so the
            # climate card controls are usable immediately.
            self._attr_target_temperature = 24.0
            _LOGGER.debug(
                "Climate AC: %s has no previous state — defaulting target "
                "to %s°C",
                self._zone_name,
                self._attr_target_temperature,
            )

        # Listen for zone config changes
        zone_config_manager = self.coordinator.zone_config_manager
        if zone_config_manager:

            @callback
            def _handle_zone_config_change(zone_id: str, key: str, value: Any) -> None:
                """Handle zone config change."""
                if zone_id == self._zone_id and key in ("min_temp", "max_temp"):
                    self._update_temp_limits()
                    self.async_write_ha_state()
                    _LOGGER.debug(
                        "Climate AC: %s zone config %s changed to %s",
                        self._zone_name, key, value,
                    )

            self._unsub_zone_config = zone_config_manager.add_listener(_handle_zone_config_change)  # type: ignore[assignment]
            # Initial update of temp limits
            self._update_temp_limits()

        # Subscribe to external sensor state changes for real-time updates
        self._subscribe_external_sensors()

        # Subscribe to HomeKit dispatcher signal for real-time sensor updates
        self._unsub_homekit_signal = async_dispatcher_connect(
            self.hass,
            SIGNAL_HOMEKIT_UPDATE.format(home_id=self._home_id),
            self._handle_homekit_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Tear down zone-config / external-sensor / HomeKit listeners."""
        self._unsubscribe_external_sensors()
        if self._unsub_homekit_signal:
            self._unsub_homekit_signal()
            self._unsub_homekit_signal = None
        if self._unsub_zone_config:
            self._unsub_zone_config()
            self._unsub_zone_config = None
        await super().async_will_remove_from_hass()

    @callback
    def _subscribe_external_sensors(self) -> None:
        """Subscribe to external sensor state changes for real-time updates."""
        self._unsub_external_sensors = setup_climate_external_sensor_subscription(
            self, self._zone_id, self._unsub_external_sensors, label=f"AC {self._zone_name}",
        )

    @callback
    def _unsubscribe_external_sensors(self) -> None:
        """Unsubscribe from external sensor state change listeners."""
        unsubscribe_external_sensors(self._unsub_external_sensors)

    @callback
    def _handle_homekit_update(self, zone_id: str) -> None:
        """Refresh AC sensor + target merge when the HomeKit bridge fires."""
        if zone_id != self._zone_id:
            return
        if self.coordinator.is_entity_fresh(self.entity_id):
            return

        zone_data = get_zone_state(self.coordinator.data, self._zone_id) or {}
        self._update_ac_sensor_data(zone_data)

        # Merge target temperature and HVAC mode from HomeKit (if fresh)
        reconciler = self.coordinator.state_reconciler
        provider = self.coordinator.homekit_provider
        if reconciler and provider and provider.is_connected:
            reconciler.local_provider = provider

            # Target temperature
            cloud_target = self._attr_target_temperature
            merged_target, target_src = reconciler.merge_zone_target_temperature(
                self._zone_id, cloud_target,
            )
            if merged_target is not None and merged_target != self._attr_target_temperature:
                self._attr_target_temperature = merged_target
                _LOGGER.debug(
                    "Climate AC: %s target %s → %s°C (source %s)",
                    self._zone_name, cloud_target, merged_target, target_src,
                )

            # HVAC mode — reconciler's changed-timestamp check handles
            # stale-vs-fresh. AC zones don't have the frost-protection
            # semantic that heating zones do, so OFF here just means
            # "user turned it off"; a legitimate OFF → COOL / OFF → HEAT
            # via the Tado app must still reflect here.
            merged_state, state_src = reconciler.merge_zone_target_heating_state(
                self._zone_id, None,  # No cloud value in real-time path
            )
            if merged_state is not None and state_src == "homekit":
                new_mode = _HOMEKIT_TARGET_STATE_TO_HVAC_AC.get(merged_state)
                if new_mode is not None and new_mode != self._attr_hvac_mode:
                    old_mode = self._attr_hvac_mode
                    self._attr_hvac_mode = new_mode
                    self._attr_hvac_action = self._calculate_hvac_action(hvac_mode=new_mode)
                    _LOGGER.debug(
                        "Climate AC: %s HVAC mode %s → %s (source %s)",
                        self._zone_name, old_mode, new_mode, state_src,
                    )

        self.async_write_ha_state()

    @callback
    def _update_temp_limits(self) -> None:
        """Apply user-set min/max overrides on top of capability limits.

        Falling back to capability limits for un-overridden values is
        deliberate — `DEFAULT_ZONE_CONFIG` caps at 25°C, which would
        clip ACs that support up to 30°C.
        """
        zone_config_manager = self.coordinator.zone_config_manager
        if zone_config_manager:
            caps_min = self._get_capabilities_temp_limit("min", 16)
            caps_max = self._get_capabilities_temp_limit("max", 30)

            if zone_config_manager.has_zone_override(self._zone_id, "min_temp"):
                # User explicitly set min_temp — clamp to hardware minimum
                user_min = zone_config_manager.get_zone_value(self._zone_id, "min_temp", caps_min)
                self._attr_min_temp = max(float(user_min), caps_min)
            else:
                self._attr_min_temp = caps_min

            if zone_config_manager.has_zone_override(self._zone_id, "max_temp"):
                # User explicitly set max_temp — clamp to hardware maximum
                user_max = zone_config_manager.get_zone_value(self._zone_id, "max_temp", caps_max)
                self._attr_max_temp = min(float(user_max), caps_max)
            else:
                self._attr_max_temp = caps_max

    def _get_capabilities_temp_limit(self, limit_type: str, default: float) -> float:
        """Read `min` / `max` temperature for the first mode that exposes one."""
        ac_caps = self._capabilities.get("ac_capabilities") or {}
        for mode in ["COOL", "HEAT", "AUTO", "DRY"]:
            if mode in ac_caps and "temperatures" in ac_caps[mode]:
                temp_caps = ac_caps[mode]["temperatures"].get("celsius") or {}
                if limit_type in temp_caps:
                    return temp_caps[limit_type]  # type: ignore[no-any-return]
        return default

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        attrs: dict[str, Any] = {
            "overlay_type": _format_overlay_type(self._overlay_type),
            "ac_power_percentage": self._ac_power_percentage,
            "zone_id": self._zone_id,
            "zone_type": _format_zone_type("AIR_CONDITIONING"),
            "temperature_source": self._temperature_source,
            "humidity_source": self._humidity_source,
            "external_temp_sensor": self._external_temp_sensor,
            "external_humidity_sensor": self._external_humidity_sensor,
            "last_write_source": self._last_write_source,
        }
        # Schedule Preview — show current schedule target temperature
        scheduled_temp = get_current_schedule_target(
            self._zone_id,
            data_loader=self.coordinator.data_loader,
        )
        if scheduled_temp is not None:
            attrs["scheduled_target_temperature"] = scheduled_temp
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update.

        Replaces manual SIGNAL_ZONES_UPDATED and
        SIGNAL_AC_CAPABILITIES_UPDATED handlers. CoordinatorEntity calls
        this automatically after each coordinator poll.
        Also reloads AC capabilities from coordinator.data if changed.
        """
        # Reload AC capabilities from coordinator data (replaces SIGNAL_AC_CAPABILITIES_UPDATED)
        coord_data = self.coordinator.data or {}
        ac_caps_all = coord_data.get("ac_capabilities") or {}
        zone_caps = ac_caps_all.get(self._zone_id)
        if zone_caps and zone_caps != self._capabilities.get("ac_capabilities"):
            self._capabilities["ac_capabilities"] = zone_caps
            # Rebuild fan mapping from new capabilities
            fan_levels = set()
            for mode_caps in zone_caps.values():
                if isinstance(mode_caps, dict):
                    if "fanLevel" in mode_caps:
                        fan_levels.update(mode_caps["fanLevel"])
                    elif "fanSpeeds" in mode_caps:
                        fan_levels.update(mode_caps["fanSpeeds"])
            if fan_levels:
                self._tado_to_ha_fan, self._ha_to_tado_fan = build_fan_mapping(fan_levels)
                self._attr_fan_modes = list(dict.fromkeys(self._tado_to_ha_fan.values()))
                _LOGGER.debug(
                    "Climate AC: %s rebuilt fan mapping — %s",
                    self._zone_name, self._ha_to_tado_fan,
                )

        self.update()
        self.async_write_ha_state()

    def _extract_ac_power_state(
        self,
        setting: dict[str, Any],
        zone_data: dict[str, Any],
        ac_power_value: str | None,
    ) -> None:
        """Extract AC power-on state from zone data into entity attributes."""
        # Temperature
        api_target_temp = (setting.get("temperature") or {}).get("celsius")
        self._attr_target_temperature = api_target_temp

        # Mode
        tado_mode = setting.get("mode")
        self._attr_hvac_mode = TADO_TO_HA_HVAC_MODE.get(tado_mode, HVACMode.AUTO)  # type: ignore[arg-type]

        # Fan - API returns fanLevel (newer firmware) or fanSpeed (older firmware)
        fan_level = setting.get("fanLevel") or setting.get("fanSpeed")
        self._attr_fan_mode = self._tado_to_ha_fan.get(fan_level) or TADO_TO_HA_FAN.get(fan_level, FAN_AUTO)  # type: ignore[arg-type]

        # Swing — raw axis values, mapped lower-case for HA display
        v_swing = setting.get("verticalSwing")
        h_swing = setting.get("horizontalSwing")
        self._attr_swing_mode = v_swing.lower() if v_swing else None
        self._attr_swing_horizontal_mode = h_swing.lower() if h_swing else None

        self._overlay_type = zone_data.get("overlayType")

        # HVAC action
        ac_power_on = ac_power_value == "ON"
        api_hvac_action = self._calculate_hvac_action(hvac_mode=self._attr_hvac_mode, ac_power_on=ac_power_on)

        # Build api_values for optimistic resolution. Include
        # target_temperature so that a user's recent set_temperature call
        # stays visible until the API propagates the new target.
        api_values: dict[str, Any] = {
            "hvac_mode": self._attr_hvac_mode,
            "hvac_action": api_hvac_action,
        }
        if api_target_temp is not None:
            api_values["target_temperature"] = api_target_temp

        # Sequence-based optimistic state handling
        result = resolve_optimistic_update(
            self,
            api_values=api_values,
            entry_id=self._entry_id,
        )

        if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
            self._attr_hvac_mode = self._expected_hvac_mode
            self._attr_hvac_action = self._expected_hvac_action
            if self._expected_target_temperature is not None:
                self._attr_target_temperature = self._expected_target_temperature
            if self._optimistic_preserved:
                if self._optimistic_preserved.get("fan_mode") is not None:
                    self._attr_fan_mode = self._optimistic_preserved["fan_mode"]
                if self._optimistic_preserved.get("swing_mode") is not None:
                    self._attr_swing_mode = self._optimistic_preserved["swing_mode"]
                if self._optimistic_preserved.get("swing_horizontal_mode") is not None:
                    self._attr_swing_horizontal_mode = self._optimistic_preserved["swing_horizontal_mode"]
            _LOGGER.debug(
                "Climate AC: %s holding optimistic state — mode=%s "
                "action=%s target=%s",
                self._zone_name,
                self._attr_hvac_mode,
                self._attr_hvac_action,
                self._attr_target_temperature,
            )
        else:
            self._update_write_tracker_from_result(result)
            self._attr_hvac_action = api_hvac_action

    def _update_write_tracker_from_result(self, result: OptimisticUpdateResult) -> None:
        """Account a HomeKit-source write as success / failure on poll resolution.

        Mirrors the heating-side helper. `_last_write_source` is
        cleared straight after so the next poll can't double-count
        the same write.
        """
        if self._last_write_source != "homekit":
            return
        write_tracker = self.coordinator.write_health_tracker
        if write_tracker is None:
            self._last_write_source = ""
            return
        if result == OptimisticUpdateResult.ACCEPT_API:
            write_tracker.record_success()
            self._last_write_source = ""
        elif result == OptimisticUpdateResult.EXPIRED:
            _LOGGER.warning(
                "Climate AC: %s — HomeKit write not confirmed by the cloud "
                "in time, counted as a failed write",
                self._zone_name,
            )
            write_tracker.record_failure()
            self._last_write_source = ""

    def _handle_ac_off_state(self) -> None:
        """Resolve API-reported OFF against any in-flight optimistic write."""
        if self._optimistic_sequence is not None:
            result = resolve_optimistic_update(
                self,
                api_values={"hvac_mode": HVACMode.OFF, "hvac_action": HVACAction.OFF},
                entry_id=self._entry_id,
            )
            if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
                _LOGGER.debug(
                    "Climate AC: %s holding optimistic mode %s — cloud "
                    "still reports OFF",
                    self._zone_name,
                    self._expected_hvac_mode,
                )
                self._attr_hvac_mode = self._expected_hvac_mode
                self._attr_hvac_action = self._expected_hvac_action
            else:
                self._update_write_tracker_from_result(result)
                self._attr_hvac_mode = HVACMode.OFF
                self._attr_hvac_action = HVACAction.OFF
        else:
            self._attr_hvac_mode = HVACMode.OFF
            self._attr_hvac_action = HVACAction.OFF

    def _update_ac_sensor_data(self, zone_data: dict[str, Any]) -> None:
        """Pick current temperature / humidity using external > HomeKit > cloud priority."""
        sensor_data = zone_data.get("sensorDataPoints") or {}
        cloud_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
        cloud_humidity = (sensor_data.get("humidity") or {}).get("percentage")

        zcm = self.coordinator.zone_config_manager
        ext_temp = read_external_sensor(self.hass, zcm, self._zone_id, "external_temp_sensor")
        ext_hum = read_external_sensor(self.hass, zcm, self._zone_id, "external_humidity_sensor")

        # Priority chain via StateReconciler (external > homekit if fresh > cloud)
        reconciler = self.coordinator.state_reconciler
        provider = self.coordinator.homekit_provider
        if reconciler and provider and provider.is_connected:
            reconciler.local_provider = provider
            merged_temp, temp_source = reconciler.merge_zone_temperature(
                self._zone_id, cloud_temp, external_value=ext_temp,
            )
            merged_hum, hum_source = reconciler.merge_zone_humidity(
                self._zone_id, cloud_humidity, external_value=ext_hum,
            )
        elif ext_temp is not None:
            merged_temp, temp_source = ext_temp, "external"
            merged_hum, hum_source = (ext_hum, "external") if ext_hum is not None else (cloud_humidity, "cloud")
        elif ext_hum is not None:
            merged_temp, temp_source = cloud_temp, "cloud"
            merged_hum, hum_source = ext_hum, "external"
        else:
            merged_temp, temp_source = cloud_temp, "cloud"
            merged_hum, hum_source = cloud_humidity, "cloud"

        self._attr_current_temperature = merged_temp
        self._temperature_source = temp_source
        self._attr_current_humidity = merged_hum
        self._humidity_source = hum_source

        if zcm:
            zc = zcm.get_zone_config(self._zone_id)
            self._external_temp_sensor = zc.get("external_temp_sensor", "")
            self._external_humidity_sensor = zc.get("external_humidity_sensor", "")

    @callback
    def update(self) -> None:
        """Refresh AC entity state from the latest coordinator data."""
        if self.coordinator.is_entity_fresh(self.entity_id):
            # Safety net: don't skip when the entity has no data yet —
            # boot-time freshness flips true before the first poll has
            # populated anything.
            if self._attr_current_temperature is not None:
                _LOGGER.debug(
                    "Climate AC: %s skipping update — entity still fresh",
                    self._zone_name,
                )
                return
            _LOGGER.debug(
                "Climate AC: %s marked fresh but has no data yet — "
                "updating anyway",
                self._zone_name,
            )

        try:
            coord_data = self.coordinator.data or {}
            config = coord_data.get("config")
            if config:
                self._home_id = config.get("home_id")

            zone_data = get_zone_state(coord_data, self._zone_id)

            if not zone_data:
                self._attr_available = False
                return

            self._update_ac_sensor_data(zone_data)

            activity_data = zone_data.get("activityDataPoints") or {}
            ac_power = activity_data.get("acPower") or {}
            ac_power_value = ac_power.get("value")
            self._ac_power_percentage = ac_power.get("percentage")

            setting = zone_data.get("setting") or {}
            power = setting.get("power")

            if power == "ON":
                self._extract_ac_power_state(setting, zone_data, ac_power_value)
            else:
                self._handle_ac_off_state()

            self._attr_available = True

            _scm = self.coordinator.smart_comfort_manager
            if _scm and _scm.is_enabled and self._attr_current_temperature is not None:
                try:
                    _scm.record_temperature(
                        zone_id=self._zone_id, zone_name=self._zone_name,
                        temperature=self._attr_current_temperature,
                        is_heating=(ac_power_value == "ON"),
                        target_temperature=self._attr_target_temperature,
                    )
                except (KeyError, TypeError, ValueError) as e:
                    _LOGGER.debug(
                        "Climate AC: %s could not record Smart Comfort "
                        "sample (%s)",
                        self._zone_name, e,
                    )

        except Exception as e:
            _LOGGER.warning(
                "Climate AC: %s update failed (%s) — entity marked "
                "unavailable until the next poll",
                self.name, e,
                exc_info=True,
            )
            self._attr_available = False

    def _resolve_ac_mode_for_temp_change(
        self, hvac_mode: HVACMode | None, temperature: float,
    ) -> HVACMode:
        """Pick HEAT or COOL when the user adjusts target on an OFF AC.

        Heat-pump units support both directions, so we use current vs.
        requested temperature to guess intent rather than always
        defaulting to COOL.
        """
        if hvac_mode is not None:
            return hvac_mode
        if (
            self._attr_current_temperature is not None
            and HVACMode.HEAT in self._attr_hvac_modes
            and temperature > self._attr_current_temperature
        ):
            return HVACMode.HEAT
        return HVACMode.COOL

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature, optionally folding in an HVAC mode change.

        Bundles temperature + mode into a single overlay write when
        both are supplied so the cloud sees one transition rather
        than two.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        # Mode-only or OFF / AUTO transitions go through the dedicated path.
        if hvac_mode is not None and temperature is None:
            await self.async_set_hvac_mode(hvac_mode)
            return
        if hvac_mode in (HVACMode.OFF, HVACMode.AUTO):
            await self.async_set_hvac_mode(hvac_mode)
            return
        if temperature is None:
            return

        if self._attr_hvac_mode != HVACMode.OFF and ActionGuard.should_skip_temperature(
            temperature, self._attr_target_temperature,
            hvac_mode or self._attr_hvac_mode, self._attr_hvac_mode,
            optimistic_active=self._optimistic_sequence is not None,
        ):
            _LOGGER.debug(
                "Climate AC: %s skipping set_temperature — already at "
                "%s°C (Action Guard)",
                self._zone_name, temperature,
            )
            return

        await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)
        await self.coordinator.async_capture_state(self._zone_id, self._entity_type, "set_temperature")

        tado_mode = HA_TO_TADO_HVAC_MODE.get(hvac_mode) if hvac_mode else None

        old_temp = self._attr_target_temperature
        old_mode = self._attr_hvac_mode
        old_action = self._attr_hvac_action

        self._attr_target_temperature = temperature
        if hvac_mode is not None:
            self._attr_hvac_mode = hvac_mode
        if old_mode == HVACMode.OFF:
            self._attr_hvac_mode = self._resolve_ac_mode_for_temp_change(hvac_mode, temperature)

        new_hvac_action = self._calculate_hvac_action()
        self._attr_hvac_action = new_hvac_action
        self._overlay_type = "MANUAL"  # type: ignore[assignment]
        await set_optimistic_fields(
            self, self.coordinator,
            expected={
                "hvac_mode": self._attr_hvac_mode,
                "hvac_action": new_hvac_action,
                "target_temperature": temperature,
            },
            preserved_attrs={"fan_mode": self._attr_fan_mode, "swing_mode": self._attr_swing_mode, "swing_horizontal_mode": self._attr_swing_horizontal_mode},
        )
        self.async_write_ha_state()

        debounce_window = self.coordinator.config_manager.get_smart_actions_debounce_seconds()

        async def _execute_api_call() -> None:
            """Run the AC temperature write (HomeKit-first then cloud)."""
            await self._execute_ac_temp_api(
                temperature, tado_mode, old_temp, old_mode, old_action,
                raise_on_failure=(debounce_window <= 0),
            )

        if debounce_window > 0:
            await self.coordinator.action_debouncer.debounce(
                self._zone_id, _execute_api_call, window=float(debounce_window),
            )
        else:
            await _execute_api_call()

    async def _execute_ac_temp_api(
        self,
        temperature: float,
        tado_mode: str | None,
        old_temp: float | None,
        old_mode: HVACMode | None,
        old_action: HVACAction | None,
        *,
        raise_on_failure: bool = False,
    ) -> None:
        """Send the AC temperature write — HomeKit first, cloud fallback, rollback on failure."""
        # Mode changes can't go through HomeKit (no characteristic for
        # COOL→HEAT), so a "simple" write is temp-only on a running AC.
        local_success = False
        is_simple_temp_write = (
            tado_mode is None
            and self._attr_hvac_mode not in (HVACMode.OFF, HVACMode.AUTO)
        )
        write_tracker = self.coordinator.write_health_tracker
        if (
            is_simple_temp_write
            and self.coordinator.homekit_provider
            and self.coordinator.homekit_provider.is_connected
            and write_tracker is not None
            and write_tracker.should_try_homekit()
        ):
            import time as _time

            self.coordinator._homekit_write_attempts += 1
            t0 = _time.monotonic()
            try:
                local_success = await self.coordinator.homekit_provider.set_temperature(
                    self._zone_id, temperature,
                )
            except Exception:
                _LOGGER.debug(
                    "Climate AC: %s HomeKit temperature write failed — "
                    "falling back to cloud",
                    self._zone_name, exc_info=True,
                )
            elapsed_ms = (_time.monotonic() - t0) * 1000
            self.coordinator._homekit_write_latency_sum += elapsed_ms
            self.coordinator._homekit_write_latency_count += 1
            if local_success:
                # Tracker success is deferred until the cloud poll
                # confirms the change — local ack alone isn't proof.
                self.coordinator._homekit_write_successes += 1
            else:
                write_tracker.record_failure()
                self.coordinator._homekit_write_fallbacks += 1

        if local_success:
            self.coordinator.record_homekit_write_saved(self._zone_id)
            self._last_write_source = "homekit"
            _LOGGER.debug(
                "Climate AC: %s set target %s°C via HomeKit",
                self._zone_name, temperature,
            )
            return

        api_success = False
        logged_failure = False
        try:
            async with asyncio.timeout(10):
                api_success = await self._async_set_ac_overlay(temperature=temperature, mode=tado_mode)
        except TimeoutError:
            _LOGGER.warning(
                "Climate AC: %s temperature write timed out after 10s — "
                "rolling back optimistic state",
                self._zone_name,
            )
            logged_failure = True
        except Exception as e:
            _LOGGER.warning(
                "Climate AC: %s temperature write failed (%s) — rolling "
                "back optimistic state",
                self._zone_name, e,
            )
            logged_failure = True

        if api_success:
            self._last_write_source = "cloud"
            # Tell the reconciler we just wrote, so a HomeKit bridge
            # update during the protection window can't push a stale
            # value back over the user's change.
            if self.coordinator.state_reconciler:
                self.coordinator.state_reconciler.record_local_write(self._zone_id)
            _LOGGER.debug(
                "Climate AC: %s set target %s°C via cloud",
                self._zone_name, temperature,
            )
            await async_trigger_immediate_refresh(self.hass, self.entity_id, "temperature_change")
        else:
            if not logged_failure:
                _LOGGER.warning(
                    "Climate AC: %s temperature write rejected by Tado — "
                    "rolling back optimistic state",
                    self._zone_name,
                )
            self._attr_target_temperature = old_temp
            self._attr_hvac_mode = old_mode
            self._attr_hvac_action = old_action
            clear_optimistic_state(self)
            self.async_write_ha_state()
            if raise_on_failure:
                raise HomeAssistantError(
                    f"AC {self._zone_name}: Set temperature failed",
                    translation_domain=DOMAIN,
                )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the AC mode (OFF / AUTO / cooling / heating / etc.)."""
        if ActionGuard.should_skip_hvac_mode(
            hvac_mode, self._attr_hvac_mode,
            optimistic_active=self._optimistic_sequence is not None,
        ):
            _LOGGER.debug(
                "Climate AC: %s skipping set_hvac_mode — already %s "
                "(Action Guard)",
                self._zone_name, hvac_mode,
            )
            return

        await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)

        client = self.coordinator.api_client

        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_capture_state(
                self._zone_id, self._entity_type, "set_hvac_mode",
            )

            setting = {
                "type": "AIR_CONDITIONING",
                "power": "OFF",
            }
            termination = get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id)
            await api_call_with_rollback(
                self,
                client.set_zone_overlay(self._zone_id, setting, termination),
                hvac_mode=HVACMode.OFF,
                hvac_action=HVACAction.OFF,
                reason="set OFF mode",
            )
            self._last_write_source = "cloud"

        elif hvac_mode == HVACMode.AUTO:
            await api_call_with_rollback(
                self,
                client.delete_zone_overlay(self._zone_id),
                hvac_mode=HVACMode.AUTO,
                hvac_action=HVACAction.IDLE,
                overlay_type=None,
                reason="resume schedule (delete overlay)",
            )
            self._last_write_source = "cloud"
        else:
            await self.coordinator.async_capture_state(
                self._zone_id, self._entity_type, "set_hvac_mode",
            )

            old_mode = self._attr_hvac_mode
            old_temp = self._attr_target_temperature
            old_fan = self._attr_fan_mode
            old_action = self._attr_hvac_action

            self._attr_hvac_mode = hvac_mode
            self._overlay_type = "MANUAL"  # type: ignore[assignment]

            tado_mode = HA_TO_TADO_HVAC_MODE.get(hvac_mode, "COOL")

            ac_caps = self._capabilities.get("ac_capabilities") or {}
            mode_caps = ac_caps.get(tado_mode) or {}
            mode_has_temp = "temperatures" in mode_caps

            if tado_mode == "FAN" or not mode_has_temp:
                # FAN and any mode without a temperature characteristic
                # don't carry a target — clear it so the card stops
                # showing a stale target value.
                self._attr_target_temperature = None
            elif not self._attr_target_temperature:
                # Midpoint of the capability range — better than a
                # hardcoded 24°C for ACs that span 16–30°C.
                self._attr_target_temperature = (self._attr_min_temp + self._attr_max_temp) / 2

            if not self._attr_fan_mode:
                self._attr_fan_mode = "auto"

            new_hvac_action = self._calculate_hvac_action()
            self._attr_hvac_action = new_hvac_action

            await set_optimistic_fields(
                self, self.coordinator,
                expected={"hvac_mode": hvac_mode, "hvac_action": new_hvac_action},
                preserved_attrs={"fan_mode": self._attr_fan_mode, "swing_mode": self._attr_swing_mode, "swing_horizontal_mode": self._attr_swing_horizontal_mode},
            )
            self.async_write_ha_state()

            api_success = False
            logged_failure = False
            try:
                async with asyncio.timeout(10):
                    api_success = await self._async_set_ac_overlay(mode=tado_mode)
            except TimeoutError:
                _LOGGER.warning(
                    "Climate AC: %s %s mode write timed out after 10s — "
                    "rolling back optimistic state",
                    self._zone_name, hvac_mode,
                )
                logged_failure = True
            except Exception as e:
                _LOGGER.warning(
                    "Climate AC: %s %s mode write failed (%s) — rolling "
                    "back optimistic state",
                    self._zone_name, hvac_mode, e,
                )
                logged_failure = True

            if api_success:
                self._last_write_source = "cloud"
                if self.coordinator.state_reconciler:
                    self.coordinator.state_reconciler.record_local_write(self._zone_id)
                _LOGGER.debug(
                    "Climate AC: %s set HVAC mode to %s",
                    self._zone_name, hvac_mode,
                )
                await async_trigger_immediate_refresh(self.hass, self.entity_id, "hvac_mode_change")
            else:
                if not logged_failure:
                    _LOGGER.warning(
                        "Climate AC: %s %s mode write rejected by Tado — "
                        "rolling back optimistic state",
                        self._zone_name, hvac_mode,
                    )
                self._attr_hvac_mode = old_mode
                self._attr_target_temperature = old_temp
                self._attr_fan_mode = old_fan
                self._attr_hvac_action = old_action
                clear_optimistic_state(self)
                self.async_write_ha_state()
                raise HomeAssistantError(
                    f"AC {self._zone_name}: Set {hvac_mode} mode failed",
                    translation_domain=DOMAIN,
                )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the AC fan mode (auto / low / medium / high)."""
        if ActionGuard.should_skip_fan_mode(fan_mode, self._attr_fan_mode):
            _LOGGER.debug(
                "Climate AC: %s skipping set_fan_mode — already %s "
                "(Action Guard)",
                self._zone_name, fan_mode,
            )
            return

        await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)

        await self.coordinator.async_capture_state(
            self._zone_id, self._entity_type, "set_fan_mode",
        )

        old_fan = self._attr_fan_mode
        old_mode = self._attr_hvac_mode
        old_action = self._attr_hvac_action

        self._attr_fan_mode = fan_mode

        # Touching fan on an OFF AC also powers it on — default to
        # COOL so the unit has a usable mode while the user adjusts.
        if self._attr_hvac_mode == HVACMode.OFF:
            self._attr_hvac_mode = HVACMode.COOL
            self._overlay_type = "MANUAL"  # type: ignore[assignment]

        new_hvac_action = self._calculate_hvac_action()
        self._attr_hvac_action = new_hvac_action

        await set_optimistic_fields(
            self, self.coordinator,
            expected={"hvac_mode": self._attr_hvac_mode, "hvac_action": new_hvac_action},
            preserved_attrs={"fan_mode": self._attr_fan_mode, "swing_mode": self._attr_swing_mode, "swing_horizontal_mode": self._attr_swing_horizontal_mode},
        )
        self.async_write_ha_state()

        tado_fan = self._ha_to_tado_fan.get(fan_mode)
        if not tado_fan:
            _LOGGER.warning(
                "Climate AC: %s has no Tado fan mapping for '%s' — "
                "falling back to AUTO",
                self._zone_name, fan_mode,
            )
            tado_fan = "AUTO"

        api_success = False
        logged_failure = False
        try:
            async with asyncio.timeout(10):
                api_success = await self._async_set_ac_overlay(fan_level=tado_fan)
        except TimeoutError:
            _LOGGER.warning(
                "Climate AC: %s fan mode write timed out after 10s — "
                "rolling back optimistic state",
                self._zone_name,
            )
            logged_failure = True
        except Exception as e:
            _LOGGER.warning(
                "Climate AC: %s fan mode write failed (%s) — rolling "
                "back optimistic state",
                self._zone_name, e,
            )
            logged_failure = True

        if api_success:
            _LOGGER.debug(
                "Climate AC: %s set fan mode to %s",
                self._zone_name, fan_mode,
            )
            await async_trigger_immediate_refresh(self.hass, self.entity_id, "fan_mode_change")
        else:
            if not logged_failure:
                _LOGGER.warning(
                    "Climate AC: %s fan mode write rejected by Tado — "
                    "rolling back optimistic state",
                    self._zone_name,
                )
            self._attr_fan_mode = old_fan
            self._attr_hvac_mode = old_mode
            self._attr_hvac_action = old_action
            clear_optimistic_state(self)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"AC {self._zone_name}: Set fan mode failed",
                translation_domain=DOMAIN,
            )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set the AC vertical-axis swing position (raw capability value, e.g. 'up', 'mid_down', 'on')."""
        # Compat shim — accept v4.0 unified values for one cycle.
        legacy = self._migrate_legacy_swing_mode(swing_mode)
        if legacy is not None:
            v_value, h_value = legacy
            if (
                v_value.lower() == self._attr_swing_mode
                and h_value.lower() == self._attr_swing_horizontal_mode
            ):
                _LOGGER.debug(
                    "Climate AC: %s skipping legacy swing_mode '%s' — "
                    "already in target state",
                    self._zone_name, swing_mode,
                )
                return
            await self._async_apply_axis_change(
                v_swing=v_value, h_swing=h_value, source_label=swing_mode,
            )
            return

        if ActionGuard.should_skip_swing_mode(swing_mode, self._attr_swing_mode):
            _LOGGER.debug(
                "Climate AC: %s skipping set_swing_mode — already %s "
                "(Action Guard)",
                self._zone_name, swing_mode,
            )
            return

        await self._async_apply_axis_change(
            v_swing=swing_mode.upper(),
            h_swing=None,
            source_label=swing_mode,
        )

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        """Set the AC horizontal-axis swing position."""
        if ActionGuard.should_skip_swing_mode(
            swing_horizontal_mode, self._attr_swing_horizontal_mode,
        ):
            _LOGGER.debug(
                "Climate AC: %s skipping set_swing_horizontal_mode — "
                "already %s (Action Guard)",
                self._zone_name, swing_horizontal_mode,
            )
            return

        await self._async_apply_axis_change(
            v_swing=None,
            h_swing=swing_horizontal_mode.upper(),
            source_label=swing_horizontal_mode,
        )

    async def _async_apply_axis_change(
        self,
        v_swing: str | None,
        h_swing: str | None,
        source_label: str,
    ) -> None:
        """Apply a one-or-both-axis swing change with optimistic state + rollback on failure."""
        await _check_bootstrap_reserve_or_raise(
            self.hass, f"AC {self._zone_name}", coordinator=self.coordinator,
        )

        await self.coordinator.async_capture_state(
            self._zone_id, self._entity_type, "set_swing_mode",
        )

        old_swing = self._attr_swing_mode
        old_h_swing = self._attr_swing_horizontal_mode
        old_mode = self._attr_hvac_mode
        old_action = self._attr_hvac_action

        if v_swing is not None:
            self._attr_swing_mode = v_swing.lower()
        if h_swing is not None:
            self._attr_swing_horizontal_mode = h_swing.lower()

        # Touching swing on an OFF AC also powers it on — default to
        # COOL so the unit has a usable mode while the user adjusts.
        if self._attr_hvac_mode == HVACMode.OFF:
            self._attr_hvac_mode = HVACMode.COOL
            self._overlay_type = "MANUAL"  # type: ignore[assignment]

        new_hvac_action = self._calculate_hvac_action()
        self._attr_hvac_action = new_hvac_action

        await set_optimistic_fields(
            self, self.coordinator,
            expected={"hvac_mode": self._attr_hvac_mode, "hvac_action": new_hvac_action},
            preserved_attrs={
                "fan_mode": self._attr_fan_mode,
                "swing_mode": self._attr_swing_mode,
                "swing_horizontal_mode": self._attr_swing_horizontal_mode,
            },
        )
        self.async_write_ha_state()

        api_success = False
        logged_failure = False
        try:
            async with asyncio.timeout(10):
                api_success = await self._async_set_ac_overlay(
                    vertical_swing=v_swing, horizontal_swing=h_swing,
                )
        except TimeoutError:
            _LOGGER.warning(
                "Climate AC: %s swing mode write timed out after 10s — "
                "rolling back optimistic state",
                self._zone_name,
            )
            logged_failure = True
        except Exception as e:
            _LOGGER.warning(
                "Climate AC: %s swing mode write failed (%s) — rolling "
                "back optimistic state",
                self._zone_name, e,
            )
            logged_failure = True

        if api_success:
            _LOGGER.debug(
                "Climate AC: %s set swing %s",
                self._zone_name, source_label,
            )
            await async_trigger_immediate_refresh(self.hass, self.entity_id, "swing_mode_change")
            return

        if not logged_failure:
            _LOGGER.warning(
                "Climate AC: %s swing mode write rejected by Tado — "
                "rolling back optimistic state",
                self._zone_name,
            )
        self._attr_swing_mode = old_swing
        self._attr_swing_horizontal_mode = old_h_swing
        self._attr_hvac_mode = old_mode
        self._attr_hvac_action = old_action
        clear_optimistic_state(self)
        self.async_write_ha_state()
        raise HomeAssistantError(
            f"AC {self._zone_name}: Set swing mode failed",
            translation_domain=DOMAIN,
        )

    def _migrate_legacy_swing_mode(self, swing_mode: str) -> tuple[str, str] | None:
        """Translate v4.0 unified values to (v_swing, h_swing); returns None for raw axis values.

        `'on'` is intentionally NOT in the legacy set — it collides with the
        HA Core `SWING_ON` constant, which is also a valid v4.1 raw axis
        value. Treating `'on'` as legacy would silently set both axes when
        the user clicks "On" in the v4.1 dropdown for a single axis.
        """
        legacy = {
            "off": ("OFF", "OFF"),
            "vertical": ("ON", "OFF"),
            "horizontal": ("OFF", "ON"),
            "both": ("ON", "ON"),
        }
        if swing_mode not in legacy:
            return None
        _LOGGER.warning(
            "Climate AC: %s received legacy swing_mode '%s'. Update your "
            "automations to call set_swing_mode (vertical) and "
            "set_swing_horizontal_mode separately. The compat shim is "
            "removed in v4.2.",
            self._zone_name, swing_mode,
        )
        return legacy[swing_mode]

    def _resolve_fan_level(
        self,
        mode_caps: dict[str, Any],
        fan_level: str | None,
    ) -> tuple[str | None, str | None]:
        """Pick the (key, value) pair for the fan field in the overlay payload.

        Returns `(None, None)` when the AC's current mode doesn't
        support a fan field at all.
        """
        fan_key = "fanLevel" if "fanLevel" in mode_caps else ("fanSpeeds" if "fanSpeeds" in mode_caps else None)
        if not fan_key:
            return None, None

        supported = mode_caps.get(fan_key) or []
        if fan_level:
            if fan_level in supported:
                return fan_key, fan_level
            if supported:
                fallback = "AUTO" if "AUTO" in supported else supported[0]
                _LOGGER.warning(
                    "Climate AC: %s fan level %s not supported by this "
                    "mode — using %s instead",
                    self._zone_name, fan_level, fallback,
                )
                return fan_key, fallback
        elif self._attr_fan_mode:
            tado_fan = self._ha_to_tado_fan.get(self._attr_fan_mode) or HA_TO_TADO_FAN.get(
                self._attr_fan_mode, "AUTO",
            )
            if tado_fan in supported:
                return fan_key, tado_fan
            if supported:
                fallback = "AUTO" if "AUTO" in supported else supported[-1]
                _LOGGER.debug(
                    "Climate AC: %s mapped fan %s→%s not in %s — using %s",
                    self._zone_name, self._attr_fan_mode, tado_fan, supported, fallback,
                )
                return fan_key, fallback
        elif "AUTO" in supported:
            return fan_key, "AUTO"
        elif supported:
            return fan_key, supported[0]
        return None, None

    def _resolve_ac_mode(self, mode: str | None) -> str:
        """Pick the Tado mode string to send in the overlay payload."""
        if mode:
            return mode
        if self._attr_hvac_mode and self._attr_hvac_mode not in (HVACMode.OFF, HVACMode.AUTO):
            return HA_TO_TADO_HVAC_MODE.get(self._attr_hvac_mode, "COOL")
        return "COOL"

    def _build_ac_setting(
        self,
        temperature: float | None,
        mode: str | None,
        fan_level: str | None,
        vertical_swing: str | None,
        horizontal_swing: str | None,
    ) -> dict[str, Any]:
        """Assemble the AC overlay payload from the requested change + current state."""
        setting: dict[str, Any] = {"type": "AIR_CONDITIONING", "power": "ON"}
        current_mode = self._resolve_ac_mode(mode)
        setting["mode"] = current_mode

        ac_caps = self._capabilities.get("ac_capabilities") or {}
        mode_caps = ac_caps.get(current_mode) or {}

        # Temperature
        if current_mode != "FAN" and "temperatures" in mode_caps:
            if temperature:
                setting["temperature"] = {"celsius": temperature}
            elif self._attr_target_temperature:
                setting["temperature"] = {"celsius": self._attr_target_temperature}
            else:
                setting["temperature"] = {"celsius": (self._attr_min_temp + self._attr_max_temp) / 2}

        # Fan level
        fan_key, fan_value = self._resolve_fan_level(mode_caps, fan_level)
        if fan_key and fan_value:
            setting[fan_key] = fan_value

        # Swing — pass explicit raw axis values straight through; skip
        # axes the current AC mode doesn't expose, or values not in
        # the capability set
        for swing_dir, swing_explicit in [
            ("verticalSwing", vertical_swing),
            ("horizontalSwing", horizontal_swing),
        ]:
            if swing_explicit is None:
                continue
            supported = mode_caps.get(swing_dir) or []
            if swing_explicit in supported:
                setting[swing_dir] = swing_explicit

        return setting

    async def _async_set_ac_overlay(
        self,
        temperature: float | None = None,
        mode: str | None = None,
        fan_level: str | None = None,
        vertical_swing: str | None = None,
        horizontal_swing: str | None = None,
        duration_minutes: int | None = None,
        overlay: str | None = None,
    ) -> bool:
        """Send a Tado AC overlay write — temperature, mode, fan, swing, termination."""
        client = self.coordinator.api_client
        setting = self._build_ac_setting(temperature, mode, fan_level, vertical_swing, horizontal_swing)

        termination = build_timer_termination(
            duration_minutes=duration_minutes, overlay=overlay,
            hass=self.hass, zone_id=self._zone_id, entry_id=self._entry_id,
        )

        _LOGGER.debug(
            "Climate AC: %s overlay payload — setting=%s termination=%s",
            self._zone_name, setting, termination,
        )

        if await client.set_zone_overlay(self._zone_id, setting, termination):
            _LOGGER.debug(
                "Climate AC: %s overlay applied — %s",
                self._zone_name, setting,
            )
            return True
        return False

    async def async_set_timer(
        self, temperature: float, duration_minutes: int | None = None, overlay: str | None = None,
    ) -> bool:
        """Set the AC target with a timed or "next time block" overlay.

        `overlay='next_time_block'` resumes the schedule at the next
        block boundary; `overlay='manual'` keeps the override until
        the user clears it.
        """
        await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)

        await self.coordinator.async_capture_state(
            self._zone_id, self._entity_type, "set_timer",
        )

        api_success = False
        try:
            async with asyncio.timeout(10):
                api_success = await self._async_set_ac_overlay(
                    temperature=temperature,
                    mode=None,
                    duration_minutes=duration_minutes,
                    overlay=overlay,
                )
        except TimeoutError:
            _LOGGER.warning(
                "Climate AC: %s set_timer write timed out after 10s",
                self._zone_name,
            )
        except Exception as e:
            _LOGGER.warning(
                "Climate AC: %s set_timer write failed (%s)",
                self._zone_name, e,
            )

        if api_success:
            await async_trigger_immediate_refresh(self.hass, self.entity_id, "set_timer")

        return api_success
