"""Tado CE air-conditioning climate entity: AC mode + fan + swing control.

One entity per AIR_CONDITIONING zone. Carries the same optimistic-
update + rollback pattern as the heating entity but adds fan-mode,
swing-mode, and per-mode capability handling driven by the cloud
`/capabilities` endpoint. Capability shape varies per AC unit, so
mode lists are built dynamically from cached capabilities rather
than hardcoded.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ATTR_HVAC_MODE
from homeassistant.components.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .climate_base import TadoClimateBase
from .climate_helpers import (
    CloudWriteWording,
    RollbackPlan,
    ac_power_is_on,
    ac_power_percentage,
    api_call_with_rollback,
    apply_homekit_target,
    build_plan,
    capture_climate_rollback_state,
    homekit_target_attribute,
    run_cloud_write,
    source_update_attrs,
)
from .const import DOMAIN
from .format_helpers import (
    format_overlay_type as _format_overlay_type,
)
from .format_helpers import (
    format_zone_type as _format_zone_type,
)
from .helpers import (
    Route,
    RouteDecision,
    build_timer_termination,
    decide_route,
    get_zone_overlay_termination,
    get_zone_state,
    payload_to_time,
)
from .optimistic_helpers import (
    OptimisticUpdateResult,
    clear_homekit_write,
    mark_homekit_put,
    observe_accepted,
    record_sent_attrs,
    resend_entry,
    resolve_optimistic_update,
    set_optimistic_fields,
    stash_resend,
    take_resend_target_temp,
)
from .ratelimit import async_check_bootstrap_reserve_or_raise as _check_bootstrap_reserve_or_raise
from .schedule_helpers import get_active_timetable, get_current_schedule_target
from .write_optimizer import ActionGuard, held_data_unconfirmed
from .zone_signals import zone_power_is_on, zone_view, zone_view_target
from .zone_write_ledger import ordered_zone_write

if TYPE_CHECKING:
    from collections.abc import Callable

    from .coordinator import TadoDataUpdateCoordinator
    from .zone_write_ledger import ResendEntry

_LOGGER = logging.getLogger(__name__)


def _write_wording(label: str) -> CloudWriteWording:
    """Failure log texts of an AC write: one warning per failure."""
    tail = "rolling back optimistic state"
    return CloudWriteWording(
        timeout=f"%(prefix)s: %(zone)s {label} write timed out after 10s, {tail}",
        refused=f"%(prefix)s: %(zone)s {label} write failed (%(error)s), {tail}",
        network=f"%(prefix)s: %(zone)s {label} write failed (%(error)s), {tail}",
        unexpected=f"%(prefix)s: %(zone)s {label} write failed (%(error)s), {tail}",
        failed=f"%(prefix)s: %(zone)s {label} write rejected by Tado, {tail}",
        single=True,
    )


def _write_failure(label: str, translation_key: str) -> Callable[[Any], HomeAssistantError]:
    """Build the error a failed fan, swing or temperature write raises."""

    def _build(entity: Any) -> HomeAssistantError:
        return HomeAssistantError(
            f"AC {entity._zone_name}: Set {label} failed",
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders={"zone_name": entity._zone_name},
        )

    return _build


def _mode_failure(hvac_mode: HVACMode) -> Callable[[Any], HomeAssistantError]:
    """Build the error a failed mode-on write raises."""

    def _build(entity: Any) -> HomeAssistantError:
        return HomeAssistantError(
            f"AC {entity._zone_name}: Set {hvac_mode} mode failed",
            translation_domain=DOMAIN,
            translation_key="ac_set_hvac_mode_failed",
            translation_placeholders={"zone_name": entity._zone_name, "hvac_mode": str(hvac_mode)},
        )

    return _build


def _axis_own_power(assign: dict[str, Any]) -> bool | None:
    """A fan or swing write that switched the card to a cooling mode records the zone as ON; any other records nothing."""
    return False if "_attr_hvac_mode" in assign else None


_FAN_WORDING = _write_wording("fan mode")
_SWING_WORDING = _write_wording("swing mode")
_TEMPERATURE_WORDING = _write_wording("temperature")
_FAN_FAILURE = _write_failure("fan mode", "ac_set_fan_mode_failed")
_SWING_FAILURE = _write_failure("swing mode", "ac_set_swing_mode_failed")
_TEMPERATURE_FAILURE = _write_failure("temperature", "ac_set_temperature_failed")

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
    """Build fan mode mappings from AC capabilities."""
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
            "Climate AC: zone %s fan modes %s (from %s), ha→tado %s",
            zone_id, fan_modes, fan_levels, ha_to_tado,
        )
        return tado_to_ha, ha_to_tado, fan_modes

    return dict(TADO_TO_HA_FAN), dict(HA_TO_TADO_FAN), [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]


# Physical louver-position ordering, matching the Tado app and most AC
# remotes: top-to-bottom for vertical, left-to-right for horizontal,
# with `auto` first and `on` (continuous oscillation) last. The cloud
# returns capability values as an unordered set, so without an explicit
# order the dropdown renders alphabetically, which spaces"Down /
# Mid / Mid (down) / Mid (up) / Up" in a sequence the user can't
# correlate to physical position. Unknown values fall through to the
# tail in alphabetical order so a new value Tado adds remains visible.
_VERTICAL_SWING_ORDER = (
    "auto", "up", "mid_up", "mid", "mid_down", "down", "off", "on",
)
_HORIZONTAL_SWING_ORDER = (
    "auto", "left", "mid_left", "mid", "mid_right", "right", "off", "on",
)


def _build_swing_axis_modes(
    ac_caps: dict[str, Any],
    axis: str,
    zone_id: str,
) -> list[str] | None:
    """Build the swing-mode list for one axis from the union of supported values across all AC modes.

    `axis` is `"verticalSwing"` or `"horizontalSwing"`. Returns `None`
    when the axis isn't supported by any mode. The caller uses that
    to decide whether to advertise the corresponding `SWING_MODE` /
    `SWING_HORIZONTAL_MODE` feature flag.
    """
    all_values: set[str] = set()
    for mode_caps in ac_caps.values():
        if isinstance(mode_caps, dict) and axis in mode_caps:
            all_values.update(mode_caps[axis])
    if not all_values:
        return None
    lowered = {v.lower() for v in all_values}
    canonical_order = (
        _HORIZONTAL_SWING_ORDER if axis == "horizontalSwing"
        else _VERTICAL_SWING_ORDER
    )
    ordered = [v for v in canonical_order if v in lowered]
    unknown = sorted(lowered - set(canonical_order))
    result = ordered + unknown
    _LOGGER.debug(
        "Climate AC: zone %s %s modes %s (raw=%s)",
        zone_id, axis, result, all_values,
    )
    return result


class TadoACClimate(TadoClimateBase):
    """Tado CE Air Conditioning Climate Entity."""

    _LOG_LABEL = "Climate AC"
    _ZONE_TYPE = "AIR_CONDITIONING"
    _write_timing_zone_kind = "ac"
    _DEFAULT_TARGET = 24.0
    _SENSOR_LABEL_PREFIX = "AC "

    def _apply_capabilities(self, ac_caps: dict[str, Any]) -> None:
        """Derive the capability-driven attributes (features, modes, fan, swing, temp range).

        Shared by `__init__` and `_handle_coordinator_update` so a re-pair /
        firmware change that re-fetches capabilities updates the SAME attributes
        the entity was built with, no drift between the build-once path and the
        refresh path. Runtime state (current/target temp, hvac mode) is NOT
        touched here; the caller owns that.
        """
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )
        has_fan = any(
            (ac_caps.get(mode) or {}).get("fanLevel") or (ac_caps.get(mode) or {}).get("fanSpeeds")
            for mode in ["COOL", "HEAT", "DRY", "FAN", "AUTO"]
        )
        if has_fan:
            features |= ClimateEntityFeature.FAN_MODE
        v_modes = _build_swing_axis_modes(ac_caps, "verticalSwing", self._zone_id)
        h_modes = _build_swing_axis_modes(ac_caps, "horizontalSwing", self._zone_id)
        if v_modes is not None:
            features |= ClimateEntityFeature.SWING_MODE
        if h_modes is not None:
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
        self._attr_supported_features = features

        self._attr_hvac_modes = self._build_hvac_modes(ac_caps)
        _LOGGER.debug(
            "Climate AC: zone %s HVAC modes %s",
            self._zone_id, self._attr_hvac_modes,
        )

        self._tado_to_ha_fan, self._ha_to_tado_fan, self._attr_fan_modes = _build_fan_modes(
            ac_caps, self._zone_id,
        )
        self._attr_swing_modes = v_modes
        self._attr_swing_horizontal_modes = h_modes

        self._attr_min_temp, self._attr_max_temp, self._attr_target_temperature_step = (
            self._extract_temp_range(ac_caps)
        )

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
        super().__init__(
            coordinator, zone_id, zone_name, home_id, registry_key="climate_ac", device_zone_type="AIR_CONDITIONING",
        )
        self._capabilities = capabilities

        ac_caps = capabilities.get("zone_capabilities") or {}
        self._apply_capabilities(ac_caps)

        self._attr_native_current_temperature = None
        self._attr_native_target_temperature = None
        self._attr_hvac_mode = None
        self._attr_hvac_action = None
        self._attr_fan_mode = self._attr_fan_modes[0] if self._attr_fan_modes else None
        self._attr_swing_mode = self._attr_swing_modes[0] if self._attr_swing_modes else None
        self._attr_swing_horizontal_mode = (
            self._attr_swing_horizontal_modes[0] if self._attr_swing_horizontal_modes else None
        )
        self._attr_current_humidity = None

        self._ac_power_percentage: int | None = None

        self._optimistic_preserved: dict[str, Any] | None = None
        self._expected_fan_mode: str | None = None
        self._expected_swing_mode: str | None = None
        self._expected_swing_horizontal_mode: str | None = None

    def _calculate_hvac_action(self, hvac_mode: HVACMode | None = None, *, ac_power_on: bool | None = None) -> HVACAction:
        """Resolve hvac_action for the AC zone.

        Priority: explicit OFF mode → API-confirmed power-off → mode-driven
        default. `ac_power_on=None` means caller couldn't read the API state
        and we assume ON (typical of write-time calls).
        """
        mode = hvac_mode if hvac_mode is not None else self._attr_hvac_mode

        if mode == HVACMode.OFF:
            return HVACAction.OFF

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
        # HEAT_COOL maps to Tado AUTO; AC decides per zone, no committed
        # action yet. Default to IDLE so the card doesn't lie.
        if mode == HVACMode.HEAT_COOL:
            return HVACAction.IDLE

        return HVACAction.IDLE

    @callback
    def _handle_homekit_update(self, zone_id: str) -> None:
        """Refresh AC sensor + target merge when the HomeKit bridge fires."""
        if zone_id != self._zone_id:
            return
        fresh = self.coordinator.is_entity_fresh(self.entity_id)

        zone_data = get_zone_state(self.coordinator.data, self._zone_id) or {}
        self._update_sensor_data(zone_data)
        adopted = False

        # Skipped inside the post-write window (a stale bridge value would undo the write), for a cleared target (optimistic FAN or DRY) and for a cloud mode with no temperature.
        cloud_celsius = ((zone_data.get("setting") or {}).get("temperature") or {}).get("celsius")
        if (
            self.coordinator.state_reconciler is not None and not fresh
            and self._attr_native_target_temperature is not None and cloud_celsius is not None
        ):
            # The cloud value is not passed: only a HomeKit result is applied, anything else leaves the card as it is.
            view = zone_view(self.coordinator, self._zone_id, zone_data=zone_data)
            home_target, target_src = zone_view_target(self.coordinator, view, homekit_only=True)
            if target_src == "homekit" and home_target is not None:
                previous_target = self._attr_native_target_temperature
                apply_homekit_target(self, home_target)
                adopted = True
                if home_target != previous_target:
                    _LOGGER.debug(
                        "Climate AC: %s target %s → %s°C (source %s)",
                        self._zone_name, previous_target, home_target, target_src,
                    )

        # HVAC mode is intentionally NOT derived from HomeKit here: it comes from the cloud poll (setting.mode), which knows the true schedule and overlay state.

        # The device holds the bridge's target; a hold still standing is a write of ours, whose value is not the device's yet.
        if adopted and self._optimistic_sequence is None:
            self._observe_accepted()

        self.async_write_ha_state()

    @callback
    def _update_temp_limits(self) -> None:
        """Resolve min/max temp via ZoneConfigManager: user override clamped to the AC's real hardware limits."""
        zone_config_manager = self.coordinator.zone_config_manager
        if zone_config_manager:
            ac_caps = self._capabilities.get("zone_capabilities") or {}
            self._attr_min_temp = zone_config_manager.get_effective_min_temp(self._zone_id, ac_caps)
            self._attr_max_temp = zone_config_manager.get_effective_max_temp(self._zone_id, ac_caps)

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
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update; reload AC capabilities if changed."""
        # Reload AC capabilities from coordinator data on every update, so a
        # cloud refresh (re-pair / firmware change) is picked up without a restart.
        coord_data = self.coordinator.data or {}
        all_zone_caps = coord_data.get("zone_capabilities") or {}
        zone_caps = all_zone_caps.get(self._zone_id)
        if zone_caps and zone_caps != self._capabilities.get("zone_capabilities"):
            self._capabilities["zone_capabilities"] = zone_caps
            # Re-derive every capability-driven attribute (HVAC modes, features,
            # fan, swing, temp range) so a re-pair / firmware change that adds a
            # mode surfaces it, not just the fan mapping.
            self._apply_capabilities(zone_caps)
            _LOGGER.debug(
                "Climate AC: %s capabilities refreshed, modes %s",
                self._zone_name, self._attr_hvac_modes,
            )

        super()._handle_coordinator_update()

    def _extract_ac_power_state(
        self,
        setting: dict[str, Any],
        zone_data: dict[str, Any],
        ac_power_on: bool,
        *,
        overlay_present: bool,
    ) -> None:
        """Extract AC power-on state from zone data into entity attributes."""
        # Temperature
        api_target_temp = (setting.get("temperature") or {}).get("celsius")
        # FAN and DRY carry no temperature: the card shows None and the resolver is not asked.
        self._attr_native_target_temperature = None
        if api_target_temp is not None:
            view = zone_view(self.coordinator, self._zone_id, zone_data=zone_data)
            self._attr_native_target_temperature = zone_view_target(self.coordinator, view)[0]

        # Mode
        tado_mode = setting.get("mode")
        self._attr_hvac_mode = TADO_TO_HA_HVAC_MODE.get(tado_mode, HVACMode.AUTO)  # type: ignore[arg-type]

        # Fan - API returns fanLevel (newer firmware) or fanSpeed (older firmware)
        fan_level = setting.get("fanLevel") or setting.get("fanSpeed")
        self._attr_fan_mode = self._tado_to_ha_fan.get(fan_level) or TADO_TO_HA_FAN.get(fan_level, FAN_AUTO)  # type: ignore[arg-type]

        # Swing: raw axis values, mapped lower-case for HA display
        v_swing = setting.get("verticalSwing")
        h_swing = setting.get("horizontalSwing")
        self._attr_swing_mode = v_swing.lower() if v_swing else None
        self._attr_swing_horizontal_mode = h_swing.lower() if h_swing else None

        # HVAC action
        api_hvac_action = self._calculate_hvac_action(hvac_mode=self._attr_hvac_mode, ac_power_on=ac_power_on)

        # Build api_values for optimistic resolution. Include
        # target_temperature so that a user's recent set_temperature call
        # stays visible until the API propagates the new target.
        # hvac_action is derived, so Tado never confirms it; an AUTO write is confirmed by the overlay being gone.
        api_values: dict[str, Any] = (
            {"overlay_present": overlay_present}
            if self._expected_overlay_present is not None
            else {"hvac_mode": self._attr_hvac_mode}
        )
        if api_target_temp is not None:
            api_values["target_temperature"] = api_target_temp
        api_values["fan_mode"] = self._attr_fan_mode
        api_values["swing_mode"] = self._attr_swing_mode
        api_values["swing_horizontal_mode"] = self._attr_swing_horizontal_mode

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
                self._attr_native_target_temperature = self._expected_target_temperature
            if self._optimistic_preserved:
                if self._optimistic_preserved.get("fan_mode") is not None:
                    self._attr_fan_mode = self._optimistic_preserved["fan_mode"]
                if self._optimistic_preserved.get("swing_mode") is not None:
                    self._attr_swing_mode = self._optimistic_preserved["swing_mode"]
                if self._optimistic_preserved.get("swing_horizontal_mode") is not None:
                    self._attr_swing_horizontal_mode = self._optimistic_preserved["swing_horizontal_mode"]
            _LOGGER.debug(
                "Climate AC: %s holding optimistic state, mode=%s "
                "action=%s target=%s",
                self._zone_name,
                self._attr_hvac_mode,
                self._attr_hvac_action,
                self._attr_native_target_temperature,
            )
        else:
            # Before a resend starts in the tracker call: its failure goes back to what Tado reports.
            self._observe_accepted(_attr_hvac_action=api_hvac_action)
            self._update_write_tracker_from_result(result)
            self._attr_hvac_action = api_hvac_action

    def _update_write_tracker_from_result(self, result: OptimisticUpdateResult) -> None:
        """Count a HomeKit-source write as a success on ACCEPT_API, or a failure plus a cloud resend on EXPIRED."""
        # The source is cleared once judged so the next poll cannot count the same write twice.
        if self._last_write_source != "homekit":
            return
        pending_target = take_resend_target_temp(self)
        write_tracker = self.coordinator.write_health_for(self._zone_id)
        if write_tracker is None:
            self._last_write_source = ""
            return
        if result == OptimisticUpdateResult.ACCEPT_API:
            write_tracker.record_success()
            self._last_write_source = ""
        elif result == OptimisticUpdateResult.EXPIRED:
            _LOGGER.warning(
                "Climate AC: %s. HomeKit write not confirmed by the cloud "
                "in time, counted as a failed write",
                self._zone_name,
            )
            write_tracker.record_failure()
            # Taken before the put record is cleared: the resend is judged against the zone as it was at the put.
            entry = resend_entry(self, power="ON", celsius=pending_target) if pending_target is not None else None
            clear_homekit_write(self)
            if pending_target is not None:
                self._spawn_retry_task(self._retry_expired_temp_via_cloud(pending_target, entry=entry))

    def _handle_ac_off_state(self, *, overlay_present: bool) -> None:
        """Resolve API-reported OFF against any in-flight optimistic write."""
        if self._optimistic_sequence is not None:
            result = resolve_optimistic_update(
                self,
                api_values=(
                    {"overlay_present": overlay_present}
                    if self._expected_overlay_present is not None
                    else {"hvac_mode": HVACMode.OFF}
                ),
                entry_id=self._entry_id,
            )
            if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
                _LOGGER.debug(
                    "Climate AC: %s holding optimistic mode %s, cloud "
                    "still reports OFF",
                    self._zone_name,
                    self._expected_hvac_mode,
                )
                self._attr_hvac_mode = self._expected_hvac_mode
                self._attr_hvac_action = self._expected_hvac_action
            else:
                # The overlay is already Tado's. In production a resend suspends before it reads the card, so it sees the OFF set below; only an eager-task rig reads the held mode.
                observe_accepted(self, {"_overlay_type": self._overlay_type})
                self._update_write_tracker_from_result(result)
                self._attr_hvac_mode = HVACMode.OFF
                self._attr_hvac_action = HVACAction.OFF
                self._observe_accepted()
        else:
            self._attr_hvac_mode = HVACMode.OFF
            self._attr_hvac_action = HVACAction.OFF
            self._observe_accepted()

    @callback
    def update(self) -> None:
        """Refresh AC entity state from the latest coordinator data."""
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

            activity_data = zone_data.get("activityDataPoints") or {}
            ac_on = ac_power_is_on(activity_data)
            self._ac_power_percentage = ac_power_percentage(activity_data)

            setting = zone_data.get("setting") or {}
            power = zone_view(self.coordinator, self._zone_id, zone_data=zone_data).cloud_power

            self._overlay_type = zone_data.get("overlayType")
            overlay_present = self._overlay_type is not None
            if power == "ON":
                self._extract_ac_power_state(setting, zone_data, ac_on, overlay_present=overlay_present)
            else:
                self._handle_ac_off_state(overlay_present=overlay_present)

            self._data_present = True

            _scm = self.coordinator.smart_comfort_manager
            if _scm and _scm.is_enabled and self._attr_native_current_temperature is not None:
                try:
                    _scm.record_temperature(
                        zone_id=self._zone_id, zone_name=self._zone_name,
                        temperature=self._attr_native_current_temperature,
                        is_heating=ac_on,
                        target_temperature=self._attr_native_target_temperature,
                    )
                except (KeyError, TypeError, ValueError) as e:
                    _LOGGER.debug(
                        "Climate AC: %s could not record Smart Comfort "
                        "sample (%s)",
                        self._zone_name, e,
                    )

        except Exception as e:
            _LOGGER.warning(
                "Climate AC: %s update failed (%s), entity marked "
                "unavailable until the next poll",
                self.name, e,
                exc_info=True,
            )
            self._data_present = False

    def _resolve_ac_mode_for_temp_change(
        self, hvac_mode: HVACMode | None, temperature: float,
    ) -> HVACMode:
        """Pick HEAT or COOL when the user adjusts target on an OFF or AUTO (schedule-following) AC.

        Heat-pump units support both directions, so we use current vs.
        requested temperature to guess intent rather than always
        defaulting to COOL.
        """
        if hvac_mode is not None:
            return hvac_mode
        if (
            self._attr_native_current_temperature is not None
            and HVACMode.HEAT in self._attr_hvac_modes
            and temperature > self._attr_native_current_temperature
        ):
            return HVACMode.HEAT
        return HVACMode.COOL

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature, optionally folding in an HVAC mode change.

        Bundles temperature + mode into a single overlay write when
        both are supplied so the cloud sees one transition rather than
        two.
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
            temperature, self._attr_native_target_temperature,
            hvac_mode or self._attr_hvac_mode, self._attr_hvac_mode,
            optimistic_active=self._optimistic_sequence is not None or held_data_unconfirmed(self.coordinator, self._zone_id),
        ):
            _LOGGER.debug(
                "Climate AC: %s skipping set_temperature, already at "
                "%s°C (Action Guard)",
                self._zone_name, temperature,
            )
            return

        tado_mode = HA_TO_TADO_HVAC_MODE.get(hvac_mode) if hvac_mode else None
        old_mode = self._attr_hvac_mode
        # Decided before the gate: the route says whether the gate runs now or only if the bridge fails.
        gate_pending = self._route_for_temp_write(tado_mode, old_mode).route is not Route.CLOUD
        if not gate_pending:
            await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)

        include: tuple[str, ...] = ("_attr_native_target_temperature", "_attr_hvac_action", "_overlay_type")
        if hvac_mode is not None or old_mode in (HVACMode.OFF, HVACMode.AUTO):
            include += ("_attr_hvac_mode",)
        # Before anything is assigned: a first write with no poll yet has to go back to the card as it is now.
        self._seed_accepted(include)

        self._attr_native_target_temperature = temperature
        if hvac_mode is not None:
            self._attr_hvac_mode = hvac_mode
        # OFF and AUTO name no mode to send, so record the one the overlay will carry
        if old_mode in (HVACMode.OFF, HVACMode.AUTO):
            self._attr_hvac_mode = self._resolve_ac_mode_for_temp_change(hvac_mode, temperature)

        new_hvac_action = self._calculate_hvac_action()
        self._attr_hvac_action = new_hvac_action
        self._overlay_type = "MANUAL"
        # The values this write puts on the card; the PUT also carries the card's fan and mode, recorded from the sent payload.
        assigned = capture_climate_rollback_state(self, include)
        await set_optimistic_fields(
            self, self.coordinator,
            source="cloud",
            expected={
                "hvac_mode": self._attr_hvac_mode,
                "hvac_action": new_hvac_action,
                "target_temperature": temperature,
            },
            preserved_attrs={"fan_mode": self._attr_fan_mode, "swing_mode": self._attr_swing_mode, "swing_horizontal_mode": self._attr_swing_horizontal_mode},
        )
        self.async_write_ha_state()
        # Taken at the stamp: the HomeKit await below can let a newer write stamp first.
        stamped_sequence = self._optimistic_sequence

        debounce_window = self.coordinator.config_manager.get_smart_actions_debounce_seconds()

        async def _execute_api_call() -> None:
            """Run the AC temperature write (HomeKit-first then cloud)."""
            await self._run_queued_write(
                lambda: self._execute_ac_temp_ordered(
                    temperature, tado_mode, old_mode,
                    assigned=assigned,
                    sequence=stamped_sequence,
                    gate_pending=gate_pending,
                ),
            )

        if debounce_window > 0:
            self._announce_queued_write(tuple(assigned), stamped_sequence)
            await self.coordinator.action_debouncer.debounce(
                self._zone_id, _execute_api_call, window=float(debounce_window),
            )
        else:
            await self._execute_ac_temp_ordered(
                temperature, tado_mode, old_mode,
                assigned=assigned,
                raise_on_failure=True,
                sequence=stamped_sequence,
                gate_pending=gate_pending,
            )

    def _route_for_temp_write(self, tado_mode: str | None, old_mode: HVACMode | None) -> RouteDecision:
        """The route of an AC target write; a mode change has no HomeKit characteristic, so it is always cloud."""
        if tado_mode is not None or old_mode == HVACMode.AUTO:
            return RouteDecision(Route.CLOUD, ("a mode change has no HomeKit characteristic",))
        return decide_route(
            self.hass, self.coordinator, self._zone_id, entry_id=self._entry_id,
            zone_was_off=not zone_power_is_on(self.coordinator, self._zone_id),
        )

    async def _execute_ac_temp_ordered(self, *args: Any, **kwargs: Any) -> None:
        """Send the AC target write as one ordered step: the HomeKit attempt and its cloud fallback hold the zone's slot together."""
        async with ordered_zone_write(self.coordinator, self._zone_id):
            await self._execute_ac_temp_api(*args, **kwargs)

    async def _execute_ac_temp_api(
        self,
        temperature: float,
        tado_mode: str | None,
        old_mode: HVACMode | None,
        *,
        assigned: dict[str, Any],
        raise_on_failure: bool = False,
        sequence: int | None = None,
        gate_pending: bool = False,
    ) -> None:
        """Send the AC temperature write: HomeKit first, cloud fallback, rollback on failure."""
        local_success = False
        if self._route_for_temp_write(tado_mode, old_mode).route is Route.HOMEKIT:
            local_success = await self._try_homekit_write(
                lambda: self.coordinator.homekit_provider.set_temperature(self._zone_id, temperature),
                raised_msg="Climate AC: %s HomeKit temperature write failed, falling back to cloud",
                failed_msg=None,
            )

        if local_success:
            payload = {"temperature": temperature}
            self.coordinator.record_homekit_write_saved(self._zone_id, payload_to_time(self.coordinator, self._zone_id, payload))
            stash_resend(self, target_temp=temperature)
            mark_homekit_put(self)
            self._accept_homekit_put(sequence, tuple(assigned))
            self._schedule_cloud_verification()
            _LOGGER.debug(
                "Climate AC: %s set target %s°C via HomeKit",
                self._zone_name, temperature,
            )
            return

        # async_set_temperature already assigned and stamped before the HomeKit attempt; `assigned` names what a failure restores.
        # Nothing is accepted from the plan: the payload is recorded when the PUT is answered, with no target when the mode sent has no range, and the tap's call-time values would bring back a stale mode, action or target.
        plan = RollbackPlan(
            optimistic={},
            rollback=tuple(assigned),
            expected={},
            preserved_attrs=None,
            refresh_signal="temperature_change",
            reason="set temperature",
            capture_source="set_temperature",
            capture_zone_id=self._zone_id,
            capture_entity_type=self._entity_type,
            log_prefix="Climate AC",
            sequence=sequence,
            own_power_off=False,
            wording=_TEMPERATURE_WORDING,
            log=_LOGGER,
            failure=_TEMPERATURE_FAILURE,
            # The entry point skipped the reserve gate for a HomeKit route, so the cloud fallback meets it now.
            gate=(lambda: _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)) if gate_pending else None,
        )
        try:
            await run_cloud_write(self, self._async_set_ac_overlay(temperature=temperature, mode=tado_mode), plan)
            # A hold cleared during the await resets the source, a successful cloud write must end as cloud.
            self._last_write_source = "cloud"
        except HomeAssistantError:
            # A debounced write has no caller to raise to, and the transaction already rolled back and recovered.
            if raise_on_failure:
                raise

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the AC mode (OFF / AUTO / cooling / heating / etc.)."""
        # Before the ActionGuard: a card still showing OFF must not skip the OFF and let a pending write fire.
        cancelled: dict[str, Any] | None = None
        tap_stamp = self._queued_tap_stamp()
        if hvac_mode in (HVACMode.OFF, HVACMode.AUTO):
            cancelled = self._cancel_pending_write()
        try:
            # The API reports overlayType MANUAL for any overlay whatever its termination.
            skip_for_auto = hvac_mode == HVACMode.AUTO and self._overlay_type is not None
            if not skip_for_auto and ActionGuard.should_skip_hvac_mode(
                hvac_mode, self._attr_hvac_mode,
                optimistic_active=self._optimistic_sequence is not None or held_data_unconfirmed(self.coordinator, self._zone_id),
            ):
                _LOGGER.debug(
                    "Climate AC: %s skipping set_hvac_mode, already %s "
                    "(Action Guard)",
                    self._zone_name, hvac_mode,
                )
                return

            await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)
        except HomeAssistantError:
            # The cancelled tap will never be sent, so the card goes back to what Tado holds.
            self._restore_after_refusal(cancelled, tap_stamp)
            raise

        client = self.coordinator.api_client

        if hvac_mode == HVACMode.OFF:
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
                capture_source="set_hvac_mode",
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
            tado_mode = HA_TO_TADO_HVAC_MODE.get(hvac_mode, "COOL")

            ac_caps = self._capabilities.get("zone_capabilities") or {}
            mode_caps = ac_caps.get(tado_mode) or {}
            mode_has_temp = "temperatures" in mode_caps

            new_hvac_action = self._calculate_hvac_action(hvac_mode=hvac_mode)
            assign: dict[str, Any] = {
                "_attr_hvac_mode": hvac_mode,
                "_attr_hvac_action": new_hvac_action,
                "_overlay_type": "MANUAL",
            }
            if tado_mode == "FAN" or not mode_has_temp:
                # FAN and any mode without a temperature characteristic
                # don't carry a target, clear it so the card stops
                # showing a stale target value.
                assign["_attr_native_target_temperature"] = None
            elif not self._attr_native_target_temperature:
                # Midpoint of the capability range, better than a
                # hardcoded 24°C for ACs that span 16–30°C.
                assign["_attr_native_target_temperature"] = (self._attr_min_temp + self._attr_max_temp) / 2
            if not self._attr_fan_mode:
                assign["_attr_fan_mode"] = "auto"

            plan = build_plan(
                self, assign,
                expected={"hvac_mode": hvac_mode, "hvac_action": new_hvac_action},
                preserved={
                    "fan_mode": assign.get("_attr_fan_mode", self._attr_fan_mode),
                    "swing_mode": self._attr_swing_mode,
                    "swing_horizontal_mode": self._attr_swing_horizontal_mode,
                },
                refresh_signal="hvac_mode_change",
                reason=f"set {hvac_mode} mode",
                capture_source="set_hvac_mode",
                capture_zone_id=self._zone_id,
                capture_entity_type=self._entity_type,
                log_prefix="Climate AC",
                own_power_off=False,
                wording=_write_wording(f"{hvac_mode} mode"),
                log=_LOGGER,
                failure=_mode_failure(hvac_mode),
            )
            await run_cloud_write(self, self._async_set_ac_overlay(mode=tado_mode), plan)
            # A hold cleared during the await resets the source, a successful cloud write must end as cloud.
            self._last_write_source = "cloud"

    def _axis_assignments(self, axis_attrs: dict[str, Any]) -> tuple[dict[str, Any], HVACAction]:
        """What a fan or swing write assigns: the axis values and the action, plus COOL and a MANUAL overlay when the AC was OFF or on schedule."""
        assign = dict(axis_attrs)
        mode = self._attr_hvac_mode
        # Touching an axis on an OFF or schedule-following AC also sets an overlay, default to
        # COOL so the unit has a usable mode while the user adjusts.
        if mode in (HVACMode.OFF, HVACMode.AUTO):
            mode = HVACMode.COOL
            assign["_attr_hvac_mode"] = mode
            assign["_overlay_type"] = "MANUAL"
        action = self._calculate_hvac_action(hvac_mode=mode)
        assign["_attr_hvac_action"] = action
        return assign, action

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the AC fan mode (auto / low / medium / high)."""
        if ActionGuard.should_skip_fan_mode(
            fan_mode, self._attr_fan_mode, optimistic_active=self._optimistic_sequence is not None or held_data_unconfirmed(self.coordinator, self._zone_id),
        ):
            _LOGGER.debug(
                "Climate AC: %s skipping set_fan_mode, already %s "
                "(Action Guard)",
                self._zone_name, fan_mode,
            )
            return

        await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)

        tado_fan = self._ha_to_tado_fan.get(fan_mode)
        if not tado_fan:
            _LOGGER.warning(
                "Climate AC: %s has no Tado fan mapping for '%s'. "
                "falling back to AUTO",
                self._zone_name, fan_mode,
            )
            tado_fan = "AUTO"

        assign, new_hvac_action = self._axis_assignments({"_attr_fan_mode": fan_mode})
        new_mode = assign.get("_attr_hvac_mode", self._attr_hvac_mode)
        plan = build_plan(
            self, assign,
            expected={"hvac_mode": new_mode, "hvac_action": new_hvac_action, "fan_mode": fan_mode},
            preserved={"fan_mode": fan_mode, "swing_mode": self._attr_swing_mode, "swing_horizontal_mode": self._attr_swing_horizontal_mode},
            refresh_signal="fan_mode_change",
            reason="set fan mode",
            capture_source="set_fan_mode",
            capture_zone_id=self._zone_id,
            capture_entity_type=self._entity_type,
            log_prefix="Climate AC",
            own_power_off=_axis_own_power(assign),
            wording=_FAN_WORDING,
            log=_LOGGER,
            failure=_FAN_FAILURE,
        )
        await run_cloud_write(self, self._async_set_ac_overlay(fan_level=tado_fan), plan)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set the AC vertical-axis swing position (raw capability value, e.g. 'up', 'mid_down', 'on')."""
        if ActionGuard.should_skip_swing_mode(
            swing_mode, self._attr_swing_mode, optimistic_active=self._optimistic_sequence is not None or held_data_unconfirmed(self.coordinator, self._zone_id),
        ):
            _LOGGER.debug(
                "Climate AC: %s skipping set_swing_mode, already %s "
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
            optimistic_active=self._optimistic_sequence is not None or held_data_unconfirmed(self.coordinator, self._zone_id),
        ):
            _LOGGER.debug(
                "Climate AC: %s skipping set_swing_horizontal_mode, "
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
        """Apply a swing change on one axis with optimistic state + rollback on failure."""
        await _check_bootstrap_reserve_or_raise(
            self.hass, f"AC {self._zone_name}", coordinator=self.coordinator,
        )

        axis_attrs: dict[str, Any] = {}
        if v_swing is not None:
            axis_attrs["_attr_swing_mode"] = v_swing.lower()
        if h_swing is not None:
            axis_attrs["_attr_swing_horizontal_mode"] = h_swing.lower()
        assign, new_hvac_action = self._axis_assignments(axis_attrs)
        new_mode = assign.get("_attr_hvac_mode", self._attr_hvac_mode)

        expected: dict[str, Any] = {"hvac_mode": new_mode, "hvac_action": new_hvac_action}
        if v_swing is not None:
            expected["swing_mode"] = v_swing.lower()
        if h_swing is not None:
            expected["swing_horizontal_mode"] = h_swing.lower()
        plan = build_plan(
            self, assign,
            # Both axes come back whichever one was written
            also_restore=("_attr_swing_mode", "_attr_swing_horizontal_mode"),
            expected=expected,
            preserved={
                "fan_mode": self._attr_fan_mode,
                "swing_mode": assign.get("_attr_swing_mode", self._attr_swing_mode),
                "swing_horizontal_mode": assign.get("_attr_swing_horizontal_mode", self._attr_swing_horizontal_mode),
            },
            refresh_signal="swing_mode_change",
            reason=f"set swing {source_label}",
            capture_source="set_swing_mode",
            capture_zone_id=self._zone_id,
            capture_entity_type=self._entity_type,
            log_prefix="Climate AC",
            own_power_off=_axis_own_power(assign),
            wording=_SWING_WORDING,
            log=_LOGGER,
            failure=_SWING_FAILURE,
        )
        await run_cloud_write(
            self, self._async_set_ac_overlay(vertical_swing=v_swing, horizontal_swing=h_swing), plan,
        )

    def _resolve_fan_level(
        self,
        mode_caps: dict[str, Any],
        fan_level: str | None,
    ) -> tuple[str | None, str | None]:
        """Pick the (key, value) pair for the fan field in the overlay payload.

        Returns `(None, None)` when the AC's current mode doesn't
        support a fan field at all.

        Legacy units list their options under the plural `fanSpeeds`
        capability, but the overlay payload field is the singular
        `fanSpeed`; modern units use `fanLevel` for both. Writing the
        plural key is silently dropped by the cloud, so the lookup key
        (read capabilities) and the write key (payload field) are
        resolved separately.
        """
        if "fanLevel" in mode_caps:
            lookup_key, write_key = "fanLevel", "fanLevel"
        elif "fanSpeeds" in mode_caps:
            lookup_key, write_key = "fanSpeeds", "fanSpeed"
        else:
            return None, None

        supported = mode_caps.get(lookup_key) or []
        if fan_level:
            if fan_level in supported:
                return write_key, fan_level
            if supported:
                fallback = "AUTO" if "AUTO" in supported else supported[0]
                _LOGGER.warning(
                    "Climate AC: %s fan level %s not supported by this "
                    "mode, using %s instead",
                    self._zone_name, fan_level, fallback,
                )
                return write_key, fallback
        elif self._attr_fan_mode:
            tado_fan = self._ha_to_tado_fan.get(self._attr_fan_mode) or HA_TO_TADO_FAN.get(
                self._attr_fan_mode, "AUTO",
            )
            if tado_fan in supported:
                return write_key, tado_fan
            if supported:
                fallback = "AUTO" if "AUTO" in supported else supported[-1]
                _LOGGER.debug(
                    "Climate AC: %s mapped fan %s→%s not in %s, using %s",
                    self._zone_name, self._attr_fan_mode, tado_fan, supported, fallback,
                )
                return write_key, fallback
        elif "AUTO" in supported:
            return write_key, "AUTO"
        elif supported:
            return write_key, supported[0]
        return None, None

    def _resolve_ac_mode(self, mode: str | None) -> str:
        """Pick the Tado mode string to send in the overlay payload."""
        if mode:
            return mode
        if self._attr_hvac_mode and self._attr_hvac_mode not in (HVACMode.OFF, HVACMode.AUTO):
            return HA_TO_TADO_HVAC_MODE.get(self._attr_hvac_mode, "COOL")
        return "COOL"

    async def _write_via_cloud(self, temperature: float, reason: str) -> bool:
        """Resend a temperature-only AC overlay through the shared rollback helper, for the EXPIRED resend only."""
        client = self.coordinator.api_client
        setting = self._build_ac_setting(temperature, None, None, None, None)
        termination = get_zone_overlay_termination(self.hass, self._zone_id, entry_id=self._entry_id)
        written_temp = (setting.get("temperature") or {}).get("celsius")
        hvac_mode = self._attr_hvac_mode if self._attr_hvac_mode not in (None, HVACMode.OFF, HVACMode.AUTO) else HVACMode.COOL

        await api_call_with_rollback(
            self,
            client.set_zone_overlay(self._zone_id, setting, termination),
            hvac_mode=hvac_mode,
            hvac_action=self._calculate_hvac_action(hvac_mode=hvac_mode),
            target_temp=written_temp,
            reason=reason,
            capture_source="set_temperature",
        )
        self._last_write_source = "cloud"
        return True

    async def _retry_expired_temp_via_cloud(
        self, target_temp: float, *, quota_rearmed: bool = False, entry: ResendEntry | None = None,
    ) -> None:
        # Retry a HomeKit temperature write via cloud PUT after optimistic window expired.
        async def send_resend() -> None:
            await self._write_via_cloud(target_temp, reason="HomeKit write confirmation expired, retrying via cloud")

        await self._run_expired_resend(
            self._LOG_LABEL,
            lambda rearmed, held: self._retry_expired_temp_via_cloud(target_temp, quota_rearmed=rearmed, entry=held),
            quota_rearmed=quota_rearmed,
            send=send_resend,
            value=("ON", target_temp),
            entry=entry,
        )

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

        ac_caps = self._capabilities.get("zone_capabilities") or {}
        mode_caps = ac_caps.get(current_mode) or {}

        # Temperature
        if current_mode != "FAN" and "temperatures" in mode_caps:
            if temperature:
                setting["temperature"] = {"celsius": temperature}
            elif self._attr_native_target_temperature:
                setting["temperature"] = {"celsius": self._attr_native_target_temperature}
            else:
                setting["temperature"] = {"celsius": (self._attr_min_temp + self._attr_max_temp) / 2}

        # Fan level
        fan_key, fan_value = self._resolve_fan_level(mode_caps, fan_level)
        if fan_key and fan_value:
            setting[fan_key] = fan_value

        # Swing: an axis not named keeps its current value; unsupported axes or values are omitted
        for swing_dir, swing_explicit, swing_current in [
            ("verticalSwing", vertical_swing, self._attr_swing_mode),
            ("horizontalSwing", horizontal_swing, self._attr_swing_horizontal_mode),
        ]:
            swing_value = swing_explicit if swing_explicit is not None else (swing_current or "").upper()
            supported = mode_caps.get(swing_dir) or []
            if swing_value in supported:
                setting[swing_dir] = swing_value

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
        """Send a Tado AC overlay write: temperature, mode, fan, swing, termination."""
        client = self.coordinator.api_client
        setting = self._build_ac_setting(temperature, mode, fan_level, vertical_swing, horizontal_swing)

        termination = build_timer_termination(
            duration_minutes=duration_minutes, overlay=overlay,
            hass=self.hass, zone_id=self._zone_id, entry_id=self._entry_id,
        )

        _LOGGER.debug(
            "Climate AC: %s overlay payload, setting=%s termination=%s",
            self._zone_name, setting, termination,
        )

        if await client.set_zone_overlay(self._zone_id, setting, termination):
            _LOGGER.debug(
                "Climate AC: %s overlay applied: %s",
                self._zone_name, setting,
            )
            record_sent_attrs(self, self._sent_attributes(setting))
            return True
        return False

    def _sent_attributes(self, setting: dict[str, Any]) -> dict[str, Any]:
        """The card attributes an AC overlay payload carried, read from the payload that was sent."""
        fan_level = setting.get("fanLevel") or setting.get("fanSpeed")
        mode = TADO_TO_HA_HVAC_MODE.get(setting.get("mode") or "", HVACMode.AUTO)
        sent: dict[str, Any] = {
            "_overlay_type": "MANUAL",
            "_attr_hvac_mode": mode,
            "_attr_hvac_action": self._calculate_hvac_action(hvac_mode=mode),
            "_attr_native_target_temperature": (setting.get("temperature") or {}).get("celsius"),
        }
        if fan_level:
            sent["_attr_fan_mode"] = self._tado_to_ha_fan.get(fan_level) or TADO_TO_HA_FAN.get(fan_level, FAN_AUTO)
        for payload_key, attr in (("verticalSwing", "_attr_swing_mode"), ("horizontalSwing", "_attr_swing_horizontal_mode")):
            if setting.get(payload_key):
                sent[attr] = setting[payload_key].lower()
        return sent

    async def async_set_timer(
        self, *, temperature: float, duration_minutes: int | None = None, overlay: str | None = None,
    ) -> bool:
        """Set the AC target with a timed or "next time block" overlay.

        `overlay='next_time_block'` resumes the schedule at the next
        block boundary; `overlay='manual'` keeps the override until
        the user clears it.
        """
        tap_stamp = self._queued_tap_stamp()
        cancelled = self._cancel_pending_write()
        try:
            await _check_bootstrap_reserve_or_raise(self.hass, f"AC {self._zone_name}", coordinator=self.coordinator)
        except HomeAssistantError:
            # The cancelled tap will never be sent, so the card goes back to what Tado holds.
            self._restore_after_refusal(cancelled, tap_stamp)
            raise

        client = self.coordinator.api_client
        mode = None
        if self._attr_hvac_mode in (HVACMode.OFF, HVACMode.AUTO):
            # An idle heat pump asked for a target above the room means heat, as in set_temperature
            mode = HA_TO_TADO_HVAC_MODE.get(self._resolve_ac_mode_for_temp_change(None, temperature))
        setting = self._build_ac_setting(
            temperature, mode, None, None, None,
        )
        termination = build_timer_termination(
            duration_minutes=duration_minutes, overlay=overlay,
            hass=self.hass, zone_id=self._zone_id, entry_id=self._entry_id,
        )
        written_mode = TADO_TO_HA_HVAC_MODE.get(setting["mode"], HVACMode.COOL)

        if not await self._async_timer_result(
            api_call_with_rollback(
                self,
                client.set_zone_overlay(self._zone_id, setting, termination),
                hvac_mode=written_mode,
                hvac_action=self._calculate_hvac_action(hvac_mode=written_mode),
                target_temp=(setting.get("temperature") or {}).get("celsius"),
                reason=f"set timer at {temperature}°C",
                capture_source="set_timer",
            ),
        ):
            return False
        self._last_write_source = "cloud"
        return True
