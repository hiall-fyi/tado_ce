"""Tado CE options flow: menu-based configuration UI."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant import config_entries, data_entry_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .config_manager import (
    DEFAULT_DAY_START_HOUR,
    DEFAULT_HOT_WATER_TIMER_DURATION,
    DEFAULT_NIGHT_START_HOUR,
    DEFAULT_REFRESH_DEBOUNCE_SECONDS,
    MAX_HOUR,
    MAX_REFRESH_DEBOUNCE_SECONDS,
    MIN_HOUR,
    MIN_REFRESH_DEBOUNCE_SECONDS,
)
from .const import (
    DEFAULT_HOMEKIT_CLOUD_SYNC_MINUTES,
    DEFAULT_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
    DEFAULT_PRESENCE_MIN_REFRESH_MINUTES,
    DEFAULT_WEATHER_MIN_REFRESH_MINUTES,
    DEVICE_SYNC_DELAY_DEFAULT,
    DEVICE_SYNC_DELAY_MAX,
    DEVICE_SYNC_DELAY_MIN,
    DISPLAY_TEMP_SOURCE_MAP,
    DISPLAY_TEMP_SOURCE_OPTIONS,
    DISPLAY_TEMP_SOURCE_REVERSE_MAP,
    HEATING_TYPE_OPTIONS,
    HEATING_TYPE_RADIATOR,
    MAX_CUSTOM_INTERVAL,
    MAX_HOMEKIT_CLOUD_SYNC_MINUTES,
    MAX_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
    MAX_PRESENCE_MIN_REFRESH_MINUTES,
    MAX_WEATHER_MIN_REFRESH_MINUTES,
    MIN_HOMEKIT_CLOUD_SYNC_MINUTES,
    MIN_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
    MIN_PRESENCE_MIN_REFRESH_MINUTES,
    MIN_WEATHER_MIN_REFRESH_MINUTES,
    OVERLAY_MODE_DEFAULT,
    OVERLAY_MODE_DEFAULT_DISPLAY,
    OVERLAY_MODE_MAP,
    OVERLAY_MODE_OPTIONS,
    OVERLAY_MODE_REVERSE_MAP,
    SMART_ACTIONS_DEBOUNCE_DEFAULT,
    SMART_ACTIONS_DEBOUNCE_MAX,
    SMART_ACTIONS_DEBOUNCE_MIN,
    SMART_COMFORT_MODE_OPTIONS,
    SURFACE_TEMP_OFFSET_MAX,
    SURFACE_TEMP_OFFSET_MIN,
    SURFACE_TEMP_OFFSET_STEP,
    SVC_OFFSET_MIN_CHANGE_MAX,
    SVC_OFFSET_MIN_CHANGE_MIN,
    TIMER_DURATION_DEFAULT,
    TIMER_DURATION_MAX,
    TIMER_DURATION_MIN,
    TIMER_DURATION_OPTIONS,
    WINDOW_DETECTION_MODE_DEFAULT,
    WINDOW_DETECTION_MODE_MAP,
    WINDOW_DETECTION_MODE_OPTIONS,
    WINDOW_DETECTION_MODE_REVERSE_MAP,
    WINDOW_SENSITIVITY_DEFAULT,
    WINDOW_SENSITIVITY_MAP,
    WINDOW_SENSITIVITY_OPTIONS,
    WINDOW_SENSITIVITY_REVERSE_MAP,
    ZONE_TEMP_MAX_CEILING,
    ZONE_TEMP_MIN_FLOOR,
    is_climate_zone,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, ConfigFlowResult

_LOGGER = logging.getLogger(__name__)

# All feature toggle keys (used by reset "everything")
_ALL_TOGGLE_KEYS = (
    "smart_comfort_enabled",
    "thermal_analytics_enabled",
    "adaptive_preheat_enabled",
    "schedule_calendar_enabled",
    "wc_enabled",
    "homekit_enabled",
    "weather_enabled",
    "home_state_sync_enabled",
    "mobile_devices_enabled",
    "offset_enabled",
    "zone_configuration_enabled",
    "heating_circuit_enabled",
)

# Per-scope default values for Reset to Defaults
RESET_DEFAULTS: dict[str, dict[str, Any]] = {
    "smart_comfort": {
        "smart_comfort_mode": "none",
        "use_feels_like": False,
        "mold_risk_window_type": "double_pane",
        "smart_comfort_history_days": 7,
    },
    "external_sensors": {
        "outdoor_temp_entity": "",
    },
    "comfort_safety": {
        "comfort_heat_vulnerable_group": False,
    },
    "thermal_analytics": {
        "thermal_analytics_zones": [],
        "heating_cycle_history_days": 7,
        "heating_cycle_min_cycles": 3,
        "heating_cycle_inertia_threshold": 0.1,
    },
    "weather_compensation": {
        "wc_heating_system_preset": "radiators_standard",
        "wc_slope": 1.5,
        "wc_design_outdoor_temp": -5.0,
        "wc_max_flow_temp": 65.0,
        "wc_min_flow_temp": 25.0,
        "wc_shutoff_temp": 18.0,
        "wc_smoothing_method": "ema",
        "wc_smoothing_window": 60,
        "wc_room_compensation_enabled": False,
        "wc_room_compensation_factor": 3.0,
        "wc_step_size": 1.0,
        "wc_hysteresis": 1.0,
    },
    "bridge": {
        "bridge_serial": "",
        "bridge_auth_key": "",
    },
    "homekit": {
        "homekit_cloud_sync_minutes": 30,
    },
    "polling_api": {
        "day_start_hour": 7,
        "night_start_hour": 23,
        "custom_day_interval": None,
        "custom_night_interval": None,
        "refresh_debounce_seconds": 15,
        "api_history_retention_days": 14,
        "smart_actions_debounce_seconds": 3,
        "device_sync_delay_seconds": 1.0,
        "mobile_devices_frequent_sync": False,
        "hot_water_timer_duration": 60,
        "presence_min_refresh_minutes": 5,
        "weather_min_refresh_minutes": 30,
        "mobile_devices_min_refresh_minutes": 5,
    },
}

# Reset scope options (features with tuning parameters)
_RESET_SCOPE_OPTIONS = [
    "everything",
    "smart_comfort",
    "external_sensors",
    "comfort_safety",
    "thermal_analytics",
    "weather_compensation",
    "bridge",
    "homekit",
    "polling_api",
]


class TadoCEOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Tado CE with menu-based navigation."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__()
        self._selected_zone_id: str | None = None
        self._pending_general_options: dict[str, Any] = {}
        self._reset_scope: str = "everything"

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show navigation menu for options."""
        menu_options = ["general_settings", "advanced_settings"]
        if self.config_entry.options.get("zone_configuration_enabled", False):
            menu_options.append("zone_config")
        menu_options.append("reset_to_defaults")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    def _build_general_schema(self) -> vol.Schema:
        """Build the General Settings form schema: toggles grouped by mental model (Tado / hardware / automation / advanced)."""
        opt = self.config_entry.options.get
        return vol.Schema(
            {
                # === Tado Features (Tado-native functionality) ===
                vol.Required("tado_features"): data_entry_flow.section(
                    vol.Schema(
                        {
                            # presence
                            vol.Optional(
                                "home_state_sync_enabled",
                                default=opt("home_state_sync_enabled", False),
                            ): BooleanSelector(),
                            vol.Optional(
                                "mobile_devices_enabled",
                                default=opt("mobile_devices_enabled", False),
                            ): BooleanSelector(),
                            # data
                            vol.Optional(
                                "weather_enabled",
                                default=opt("weather_enabled", False),
                            ): BooleanSelector(),
                            vol.Optional(
                                "offset_enabled",
                                default=opt("offset_enabled", False),
                            ): BooleanSelector(),
                            vol.Optional(
                                "heating_circuit_enabled",
                                default=opt("heating_circuit_enabled", False),
                            ): BooleanSelector(),
                            # view
                            vol.Optional(
                                "schedule_calendar_enabled",
                                default=opt("schedule_calendar_enabled", False),
                            ): BooleanSelector(),
                        },
                    ),
                    {"collapsed": False},
                ),
                # === Hardware Connections (physical bridges) ===
                vol.Required("hardware_connections"): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Optional(
                                "bridge_enabled",
                                default=bool(opt("bridge_serial", ""))
                                and bool(opt("bridge_auth_key", "")),
                            ): BooleanSelector(),
                            vol.Optional(
                                "homekit_enabled",
                                default=opt("homekit_enabled", False),
                            ): BooleanSelector(),
                        },
                    ),
                    {"collapsed": False},
                ),
                # === Smart Automations (tado_ce value-add features) ===
                vol.Required("smart_automations"): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Optional(
                                "smart_comfort_enabled",
                                default=opt("smart_comfort_enabled", False),
                            ): BooleanSelector(),
                            vol.Optional(
                                "thermal_analytics_enabled",
                                default=opt("thermal_analytics_enabled", False),
                            ): BooleanSelector(),
                            vol.Optional(
                                "adaptive_preheat_enabled",
                                default=opt("adaptive_preheat_enabled", False),
                            ): BooleanSelector(),
                            # wc last: it has the most prerequisites (needs the bridge)
                            vol.Optional(
                                "wc_enabled",
                                default=opt("wc_enabled", False),
                            ): BooleanSelector(),
                        },
                    ),
                    {"collapsed": False},
                ),
                # === Advanced (Per-Zone Configuration) ===
                vol.Required("advanced"): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Optional(
                                "zone_configuration_enabled",
                                default=opt("zone_configuration_enabled", False),
                            ): BooleanSelector(),
                        },
                    ),
                    {"collapsed": False},
                ),
            },
        )

    def _build_advanced_schema(
        self,
        zones_with_heating_power: list[dict[str, str]],
        has_homekit_pairing: bool = False,
    ) -> vol.Schema:
        """Build the Advanced Settings form: conditional tuning per enabled feature; Polling & API always visible."""
        options = self.config_entry.options
        opt = options.get
        sections: dict[vol.Required, Any] = {}

        # Computed up front: Weather Compensation depends on it, Internet Bridge gates on it.
        bridge_enabled = bool(opt("bridge_serial", "")) and bool(opt("bridge_auth_key", ""))

        # Section order mirrors General Settings: hardware, then automations, then always-visible last.

        # --- Internet Bridge (if credentials exist) ---
        if bridge_enabled:
            sections[vol.Required("internet_bridge")] = data_entry_flow.section(
                vol.Schema(
                    {
                        vol.Optional("bridge_serial", description={"suggested_value": opt("bridge_serial", "")}): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                        vol.Optional("bridge_auth_key", description={"suggested_value": opt("bridge_auth_key", "")}): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    },
                ),
                {"collapsed": True},
            )

        # --- HomeKit (if enabled) ---
        # Connection status is surfaced in the section description via
        # description_placeholders (see async_step_advanced_settings),
        # NOT via a pseudo-editable TextSelector field.
        # Show the HomeKit section whenever a pairing exists OR HomeKit is
        # enabled, so both a disabled-but-paired zone can be unpaired AND a
        # zone stuck enabled-but-unpaired can reach the re-pair control.
        if has_homekit_pairing or opt("homekit_enabled", False):
            sections[vol.Required("homekit")] = data_entry_flow.section(
                vol.Schema({
                    vol.Optional(
                        "homekit_cloud_sync_minutes",
                        default=opt("homekit_cloud_sync_minutes", DEFAULT_HOMEKIT_CLOUD_SYNC_MINUTES),
                    ): NumberSelector(NumberSelectorConfig(
                        min=MIN_HOMEKIT_CLOUD_SYNC_MINUTES, max=MAX_HOMEKIT_CLOUD_SYNC_MINUTES, step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )),
                    vol.Optional("homekit_repair", default=False): BooleanSelector(),
                    vol.Optional("homekit_unpair", default=False): BooleanSelector(),
                }),
                {"collapsed": True},
            )

        # --- Smart Comfort (if enabled) ---
        if opt("smart_comfort_enabled", False):
            # First-enable default: if smart_comfort_mode has never been set,
            # suggest "light" as a sensible starting point. Otherwise preserve
            # whatever the user chose (including "none" if they explicitly set
            # that). Legacy `weather_compensation` fallback kept for migration.
            stored_mode = opt("smart_comfort_mode", opt("weather_compensation"))
            smart_comfort_default = stored_mode if stored_mode is not None else "light"
            sections[vol.Required("smart_comfort")] = data_entry_flow.section(
                vol.Schema(
                    {
                        vol.Optional(
                            "smart_comfort_mode",
                            default=smart_comfort_default,
                        ): SelectSelector(SelectSelectorConfig(options=["none", "light", "moderate", "aggressive"], translation_key="smart_comfort_mode", mode=SelectSelectorMode.DROPDOWN)),
                        vol.Optional("use_feels_like", default=opt("use_feels_like", False)): BooleanSelector(),
                        vol.Optional(
                            "mold_risk_window_type",
                            default=opt("mold_risk_window_type", "double_pane"),
                        ): SelectSelector(SelectSelectorConfig(options=["single_pane", "double_pane", "triple_pane", "passive_house"], translation_key="mold_risk_window_type", mode=SelectSelectorMode.DROPDOWN)),
                        vol.Optional("smart_comfort_history_days", default=opt("smart_comfort_history_days", 7)): NumberSelector(NumberSelectorConfig(min=1, max=30, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="d")),
                    },
                ),
                {"collapsed": True},
            )

        # --- External Sensors (always visible) ---
        # Outdoor data is a cross-feature input (window detection, Smart Comfort,
        # mold risk, Air Comfort AQI), so its bindings live in a neutral section,
        # not under whichever feature consumes them.
        sections[vol.Required("external_sensors")] = data_entry_flow.section(
            vol.Schema(
                {
                    vol.Optional(
                        "use_outdoor_temp_entity",
                        default=bool(opt("outdoor_temp_entity", "")),
                    ): BooleanSelector(),
                    vol.Optional(
                        "outdoor_temp_entity",
                        description={"suggested_value": opt("outdoor_temp_entity", "")}
                        if opt("outdoor_temp_entity", "") else None,
                    ): EntitySelector(EntitySelectorConfig(domain=["sensor", "weather"])),
                },
            ),
            {"collapsed": False},
        )
        # --- Thermal Analytics (if enabled) ---
        if opt("thermal_analytics_enabled", False):
            current_thermal_zones = options.get("thermal_analytics_zones", [])
            if not current_thermal_zones and zones_with_heating_power:
                current_thermal_zones = [z["value"] for z in zones_with_heating_power]
            sections[vol.Required("thermal_analytics")] = data_entry_flow.section(
                vol.Schema(
                    {
                        vol.Optional("thermal_analytics_zones", default=current_thermal_zones): SelectSelector(
                            SelectSelectorConfig(options=zones_with_heating_power or [], multiple=True, mode=SelectSelectorMode.DROPDOWN),  # type: ignore[typeddict-item]
                        ),
                        vol.Optional("heating_cycle_history_days", default=opt("heating_cycle_history_days", 7)): NumberSelector(NumberSelectorConfig(min=1, max=30, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="d")),
                        vol.Optional("heating_cycle_min_cycles", default=opt("heating_cycle_min_cycles", 3)): NumberSelector(NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)),
                        vol.Optional("heating_cycle_inertia_threshold", default=opt("heating_cycle_inertia_threshold", 0.1)): NumberSelector(NumberSelectorConfig(min=0.05, max=0.5, step=0.05, mode=NumberSelectorMode.BOX, unit_of_measurement="°C")),
                    },
                ),
                {"collapsed": True},
            )

        # --- Weather Compensation (if enabled AND bridge exists) ---
        if opt("wc_enabled", False) and bridge_enabled:
            sections[vol.Required("weather_compensation")] = data_entry_flow.section(
                vol.Schema(
                    {
                        vol.Optional("wc_heating_system_preset", default=opt("wc_heating_system_preset", "radiators_standard")): SelectSelector(SelectSelectorConfig(options=["radiators_standard", "radiators_low_temp", "underfloor", "custom"], translation_key="wc_heating_system_preset", mode=SelectSelectorMode.DROPDOWN)),
                        vol.Optional("wc_slope", default=opt("wc_slope", 1.5)): NumberSelector(NumberSelectorConfig(min=0.3, max=3.0, step=0.1, mode=NumberSelectorMode.BOX)),
                        vol.Optional("wc_design_outdoor_temp", default=opt("wc_design_outdoor_temp", -5.0)): NumberSelector(NumberSelectorConfig(min=-30, max=10, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="°C")),
                        vol.Optional("wc_max_flow_temp", default=opt("wc_max_flow_temp", 65.0)): NumberSelector(NumberSelectorConfig(min=25, max=80, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="°C")),
                        vol.Optional("wc_min_flow_temp", default=opt("wc_min_flow_temp", 25.0)): NumberSelector(NumberSelectorConfig(min=25, max=60, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="°C")),
                        vol.Optional("wc_shutoff_temp", default=opt("wc_shutoff_temp", 18.0)): NumberSelector(NumberSelectorConfig(min=5, max=30, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="°C")),
                        vol.Optional("wc_smoothing_method", default=opt("wc_smoothing_method", "ema")): SelectSelector(SelectSelectorConfig(options=["none", "ema", "rolling_average"], translation_key="wc_smoothing_method", mode=SelectSelectorMode.DROPDOWN)),
                        vol.Optional("wc_smoothing_window", default=opt("wc_smoothing_window", 60)): NumberSelector(NumberSelectorConfig(min=15, max=MAX_CUSTOM_INTERVAL, step=15, mode=NumberSelectorMode.BOX, unit_of_measurement="min")),
                        vol.Optional("wc_room_compensation_enabled", default=opt("wc_room_compensation_enabled", False)): BooleanSelector(),
                        vol.Optional("wc_room_compensation_factor", default=opt("wc_room_compensation_factor", 3.0)): NumberSelector(NumberSelectorConfig(min=1.0, max=5.0, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="°C/°C")),
                        vol.Optional("wc_step_size", default=opt("wc_step_size", 1.0)): NumberSelector(NumberSelectorConfig(min=0.5, max=2.0, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="°C")),
                        vol.Optional("wc_hysteresis", default=opt("wc_hysteresis", 1.0)): NumberSelector(NumberSelectorConfig(min=0.5, max=3.0, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="°C")),
                    },
                ),
                {"collapsed": True},
            )

        # --- Comfort & Safety (always visible) ---
        sections[vol.Required("comfort_safety")] = data_entry_flow.section(
            vol.Schema(
                {
                    vol.Optional(
                        "comfort_heat_vulnerable_group",
                        default=opt("comfort_heat_vulnerable_group", False),
                    ): BooleanSelector(),
                },
            ),
            {"collapsed": False},
        )

        # --- Polling & API (always visible) ---
        polling_schema_fields: dict[vol.Optional | vol.Required, Any] = {}

        polling_schema_fields[vol.Required("day_start_hour", default=opt("day_start_hour", DEFAULT_DAY_START_HOUR))] = NumberSelector(NumberSelectorConfig(min=MIN_HOUR, max=MAX_HOUR, step=1, mode=NumberSelectorMode.BOX))
        polling_schema_fields[vol.Required("night_start_hour", default=opt("night_start_hour", DEFAULT_NIGHT_START_HOUR))] = NumberSelector(NumberSelectorConfig(min=MIN_HOUR, max=MAX_HOUR, step=1, mode=NumberSelectorMode.BOX))

        custom_day_interval = options.get("custom_day_interval")
        custom_night_interval = options.get("custom_night_interval")
        # suggested_value, not default: default re-injects the old value when the
        # box is cleared, so an emptied field could never return to auto.
        custom_day_schema = (
            vol.Optional("custom_day_interval", description={"suggested_value": custom_day_interval})
            if custom_day_interval is not None else vol.Optional("custom_day_interval")
        )
        custom_night_schema = (
            vol.Optional("custom_night_interval", description={"suggested_value": custom_night_interval})
            if custom_night_interval is not None else vol.Optional("custom_night_interval")
        )

        polling_schema_fields[custom_day_schema] = NumberSelector(NumberSelectorConfig(min=0, max=MAX_CUSTOM_INTERVAL, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"))
        polling_schema_fields[custom_night_schema] = NumberSelector(NumberSelectorConfig(min=0, max=MAX_CUSTOM_INTERVAL, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"))
        # Per-data-type refresh intervals, conditional on each feature toggle,
        # grouped here right after the polling schedule fields they relate to.
        if opt("home_state_sync_enabled", False):
            polling_schema_fields[vol.Optional(
                "presence_min_refresh_minutes",
                default=opt("presence_min_refresh_minutes", DEFAULT_PRESENCE_MIN_REFRESH_MINUTES),
            )] = NumberSelector(NumberSelectorConfig(
                min=MIN_PRESENCE_MIN_REFRESH_MINUTES,
                max=MAX_PRESENCE_MIN_REFRESH_MINUTES,
                step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min",
            ))

        if opt("weather_enabled", False):
            polling_schema_fields[vol.Optional(
                "weather_min_refresh_minutes",
                default=opt("weather_min_refresh_minutes", DEFAULT_WEATHER_MIN_REFRESH_MINUTES),
            )] = NumberSelector(NumberSelectorConfig(
                min=MIN_WEATHER_MIN_REFRESH_MINUTES,
                max=MAX_WEATHER_MIN_REFRESH_MINUTES,
                step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min",
            ))

        if opt("mobile_devices_enabled", False):
            polling_schema_fields[vol.Optional("mobile_devices_frequent_sync", default=opt("mobile_devices_frequent_sync", False))] = BooleanSelector()
            polling_schema_fields[vol.Optional(
                "mobile_devices_min_refresh_minutes",
                default=opt("mobile_devices_min_refresh_minutes", DEFAULT_MOBILE_DEVICES_MIN_REFRESH_MINUTES),
            )] = NumberSelector(NumberSelectorConfig(
                min=MIN_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
                max=MAX_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
                step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min",
            ))

        # Interaction timing: debounce and write-pacing knobs.
        polling_schema_fields[vol.Optional("refresh_debounce_seconds", default=opt("refresh_debounce_seconds", DEFAULT_REFRESH_DEBOUNCE_SECONDS))] = NumberSelector(NumberSelectorConfig(min=MIN_REFRESH_DEBOUNCE_SECONDS, max=MAX_REFRESH_DEBOUNCE_SECONDS, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"))
        polling_schema_fields[vol.Optional("api_history_retention_days", default=opt("api_history_retention_days", 14))] = NumberSelector(NumberSelectorConfig(min=0, max=365, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="d"))
        polling_schema_fields[vol.Optional("smart_actions_debounce_seconds", default=opt("smart_actions_debounce_seconds", SMART_ACTIONS_DEBOUNCE_DEFAULT))] = NumberSelector(NumberSelectorConfig(min=SMART_ACTIONS_DEBOUNCE_MIN, max=SMART_ACTIONS_DEBOUNCE_MAX, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"))
        polling_schema_fields[vol.Optional("device_sync_delay_seconds", default=opt("device_sync_delay_seconds", DEVICE_SYNC_DELAY_DEFAULT))] = NumberSelector(NumberSelectorConfig(min=DEVICE_SYNC_DELAY_MIN, max=DEVICE_SYNC_DELAY_MAX, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="s"))

        # Hot water timer default duration (service-layer default, affects
        # the water_heater.turn_on and set_water_heater_timer service when
        # no explicit duration is given).
        polling_schema_fields[vol.Optional("hot_water_timer_duration", default=opt("hot_water_timer_duration", DEFAULT_HOT_WATER_TIMER_DURATION))] = NumberSelector(NumberSelectorConfig(min=1, max=MAX_CUSTOM_INTERVAL, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"))

        sections[vol.Required("polling_api")] = data_entry_flow.section(
            vol.Schema(polling_schema_fields),
            {"collapsed": True},
        )

        return vol.Schema(sections)

    def _process_general_settings(
        self,
        user_input: dict[str, Any],
        processed: dict[str, Any],
    ) -> None:
        """Flatten General Settings section dicts to top-level toggle keys (sections are mental-model only)."""
        for section_key in (
            "tado_features",
            "hardware_connections",
            "smart_automations",
            "advanced",
        ):
            section = user_input.get(section_key, {})
            for key, value in section.items():
                processed[key] = value
        # Preserve all existing tuning values from current options
        for key, value in self.config_entry.options.items():
            if key not in processed:
                processed[key] = value

    async def _detect_first_enable(self, new_options: dict[str, Any]) -> str | None:
        """Detect if a feature was just enabled, returning the sub-flow step_id, or None if no sub-flow needed."""
        prev = self.config_entry.options

        # Bridge: first enable AND no credentials stored
        if new_options.get("bridge_enabled") and not prev.get("bridge_serial"):
            return "bridge_setup"

        # HomeKit: first enable AND no pairing stored. Ask HomeKitClient
        # (the Store owner) so the check can't drift from where the pairing
        # actually lives.
        if new_options.get("homekit_enabled") and not prev.get("homekit_enabled"):
            from .homekit_client import HomeKitClient

            client = HomeKitClient(
                self.hass, self.config_entry.data.get("home_id") or "default",
            )
            if not await client.async_has_pairing():
                return "homekit_pairing"

        # WC: first enable AND bridge not enabled
        if (
            new_options.get("wc_enabled")
            and not prev.get("wc_enabled")
            and not new_options.get("bridge_enabled")
            and not prev.get("bridge_serial")
        ):
            return "wc_bridge_prompt"

        return None

    async def async_step_general_settings(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle General Settings: feature toggles only."""
        errors: dict[str, str] = {}

        if user_input is not None:
            processed_input: dict[str, Any] = {}
            self._process_general_settings(user_input, processed_input)

            if not errors:
                # Check for first-enable sub-flows
                redirect = await self._detect_first_enable(processed_input)
                if redirect:
                    self._pending_general_options = processed_input
                    return await getattr(self, f"async_step_{redirect}")()  # type: ignore[no-any-return]

                prev_options = self.config_entry.options

                from .entity_cleanup import detect_cleanup_flags

                cleanup_flags = detect_cleanup_flags(dict(prev_options), processed_input)

                if cleanup_flags:
                    coordinator = self.config_entry.runtime_data
                    coordinator._pending_cleanup[self.config_entry.entry_id] = cleanup_flags

                return self.async_create_entry(title="", data=processed_input)

        schema = self._build_general_schema()
        return self.async_show_form(
            step_id="general_settings",
            data_schema=schema,
            errors=errors,
        )

    def _process_advanced_settings_input(
        self, user_input: dict[str, Any], errors: dict[str, str],
    ) -> dict[str, Any]:
        """Process advanced settings form input into flat key-value pairs."""
        processed_input: dict[str, Any] = {}

        self._process_smart_comfort(user_input, processed_input)
        self._process_external_sensors(user_input, processed_input)
        self._process_polling_api(user_input, processed_input, errors)

        # Flatten comfort_safety section
        if "comfort_safety" in user_input:
            section = user_input["comfort_safety"]
            if "comfort_heat_vulnerable_group" in section:
                processed_input["comfort_heat_vulnerable_group"] = section["comfort_heat_vulnerable_group"]

        # Flatten thermal_analytics section
        if "thermal_analytics" in user_input:
            section = user_input["thermal_analytics"]
            for key in (
                "thermal_analytics_zones",
                "heating_cycle_history_days",
                "heating_cycle_min_cycles",
                "heating_cycle_inertia_threshold",
            ):
                if key in section:
                    processed_input[key] = section[key]

        # Flatten homekit section
        if "homekit" in user_input:
            section = user_input["homekit"]
            if "homekit_cloud_sync_minutes" in section:
                processed_input["homekit_cloud_sync_minutes"] = section["homekit_cloud_sync_minutes"]
            # homekit_repair / homekit_unpair trigger redirects (handled in async_step_advanced_settings)
            if section.get("homekit_repair", False):
                self._homekit_repair_requested = True
            if section.get("homekit_unpair", False):
                self._homekit_unpair_requested = True

        return processed_input

    async def async_step_advanced_settings(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle Advanced Settings: tuning parameters for enabled features."""
        errors: dict[str, str] = {}

        if user_input is not None:
            processed_input = self._process_advanced_settings_input(user_input, errors)
            self._process_internet_bridge(user_input, processed_input)
            self._process_weather_compensation(user_input, processed_input, errors)

            # Handle HomeKit re-pair / unpair redirects (re-pair takes precedence)
            if getattr(self, "_homekit_repair_requested", False):
                self._homekit_repair_requested = False
                return await self.async_step_homekit_pairing()
            if getattr(self, "_homekit_unpair_requested", False):
                self._homekit_unpair_requested = False
                return await self.async_step_homekit_unpair()

            if not errors:
                # Preserve toggle states from current options
                for key, value in self.config_entry.options.items():
                    if key not in processed_input:
                        processed_input[key] = value

                return self.async_create_entry(title="", data=processed_input)

        zones_with_heating_power = await self._load_zones_with_heating_power()

        from .homekit_client import HomeKitClient

        hk_client = HomeKitClient(
            self.hass, self.config_entry.data.get("home_id") or "default",
        )
        has_homekit_pairing = await hk_client.async_has_pairing()
        schema = self._build_advanced_schema(
            zones_with_heating_power, has_homekit_pairing=has_homekit_pairing,
        )

        # Compute HomeKit connection status for the section description
        # (rendered via strings.json placeholder {homekit_status}).
        homekit_status = ""
        opt = self.config_entry.options.get
        if opt("homekit_enabled", False):
            coordinator = self.config_entry.runtime_data
            hk_connected = (
                coordinator.homekit_provider is not None
                and coordinator.homekit_provider.is_connected
            )
            homekit_status = "Connected" if hk_connected else "Disconnected"

        return self.async_show_form(
            step_id="advanced_settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={"homekit_status": homekit_status},
        )

    async def async_step_bridge_setup(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle one-time bridge credential input on first enable."""
        errors: dict[str, str] = {}

        if user_input is not None:
            bridge_serial = (user_input.get("bridge_serial") or "").strip()
            bridge_auth_key = (user_input.get("bridge_auth_key") or "").strip()

            if not bridge_serial or not bridge_auth_key:
                errors["base"] = "bridge_credentials_required"
            elif not bridge_serial.upper().startswith("IB"):
                errors["base"] = "bridge_serial_invalid"
            else:
                from .bridge_api import TadoBridgeApiClient

                session = async_get_clientsession(self.hass)
                bridge_client = TadoBridgeApiClient(session, bridge_serial, bridge_auth_key)
                if not await bridge_client.async_validate_credentials():
                    errors["base"] = "bridge_auth_failed"

            if not errors:
                self._pending_general_options["bridge_serial"] = bridge_serial
                self._pending_general_options["bridge_auth_key"] = bridge_auth_key
                return self.async_create_entry(title="", data=self._pending_general_options)

        return self.async_show_form(
            step_id="bridge_setup",
            data_schema=vol.Schema(
                {
                    vol.Required("bridge_serial"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT),
                    ),
                    vol.Required("bridge_auth_key"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD),
                    ),
                },
            ),
            errors=errors,
        )

    async def async_step_homekit_pairing(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle HomeKit pairing sub-step."""
        from .homekit_client import async_step_homekit_pairing

        return await async_step_homekit_pairing(self, user_input)

    async def async_step_homekit_unpair(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle HomeKit unpairing sub-step."""
        from .homekit_client import async_step_homekit_unpair

        return await async_step_homekit_unpair(self, user_input)

    async def async_step_wc_bridge_prompt(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Prompt to also enable bridge when WC enabled without bridge."""
        if user_input is not None:
            if user_input.get("also_enable_bridge", False):
                self._pending_general_options["bridge_enabled"] = True
                return await self.async_step_bridge_setup()
            # Continue without bridge
            return self.async_create_entry(title="", data=self._pending_general_options)

        return self.async_show_form(
            step_id="wc_bridge_prompt",
            data_schema=vol.Schema(
                {
                    vol.Optional("also_enable_bridge", default=False): BooleanSelector(),
                },
            ),
        )

    def _apply_reset(self, scope: str) -> dict[str, Any]:
        """Apply reset defaults for the given scope, returning new options dict."""
        current = dict(self.config_entry.options)
        if scope == "everything":
            for toggle in _ALL_TOGGLE_KEYS:
                current[toggle] = False
            for defaults in RESET_DEFAULTS.values():
                current.update(defaults)
            # Preserve bridge credentials
            prev_serial = self.config_entry.options.get("bridge_serial", "")
            prev_auth = self.config_entry.options.get("bridge_auth_key", "")
            if prev_serial:
                current["bridge_serial"] = prev_serial
            if prev_auth:
                current["bridge_auth_key"] = prev_auth
        elif scope in RESET_DEFAULTS:
            current.update(RESET_DEFAULTS[scope])
        return current

    async def async_step_reset_to_defaults(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle Reset to Defaults: scope selection."""
        if user_input is not None:
            self._reset_scope = user_input.get("reset_scope", "everything")
            return await self.async_step_reset_confirm()

        return self.async_show_form(
            step_id="reset_to_defaults",
            data_schema=vol.Schema(
                {
                    vol.Required("reset_scope", default="everything"): SelectSelector(
                        SelectSelectorConfig(
                            options=_RESET_SCOPE_OPTIONS,
                            translation_key="reset_scope",
                            mode=SelectSelectorMode.LIST,
                        ),
                    ),
                },
            ),
        )

    async def async_step_reset_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle Reset confirmation step."""
        if user_input is not None:
            reset_options = self._apply_reset(self._reset_scope)

            from .entity_cleanup import detect_cleanup_flags

            prev_options = self.config_entry.options
            cleanup_flags = detect_cleanup_flags(dict(prev_options), reset_options)
            if cleanup_flags:
                coordinator = self.config_entry.runtime_data
                coordinator._pending_cleanup[self.config_entry.entry_id] = cleanup_flags

            return self.async_create_entry(title="", data=reset_options)

        return self.async_show_form(
            step_id="reset_confirm",
            data_schema=vol.Schema({}),
        )

    def _process_smart_comfort(
        self,
        user_input: dict[str, Any],
        processed: dict[str, Any],
    ) -> None:
        """Flatten smart_comfort section inputs."""
        if "smart_comfort" not in user_input:
            return
        section = user_input["smart_comfort"]
        for key in [
            "smart_comfort_mode",
            "use_feels_like",
            "mold_risk_window_type",
            "smart_comfort_history_days",
        ]:
            if key in section:
                processed[key] = section[key]

    def _process_external_sensors(
        self, user_input: dict[str, Any], processed: dict[str, Any],
    ) -> None:
        """Flatten the External Sensors section (outdoor temp + AQI bindings).

        A collapsed section omits its fields, so a missing toggle key means
        'collapsed', not 'off': preserve/auto-enable rather than clear. Only an
        explicit toggle-off clears. Mirrors _process_zone_sensor_input's pattern.
        """
        if "external_sensors" not in user_input:
            return
        section = user_input["external_sensors"]
        _LOGGER.debug("external_sensors section submitted: %s", section)
        existing = self.config_entry.options
        for toggle_key, entity_key in (
            ("use_outdoor_temp_entity", "outdoor_temp_entity"),
        ):
            submitted = (section.get(entity_key) or "").strip()
            if toggle_key in section:
                use = section[toggle_key]
            else:  # toggle absent (collapsed) → preserve, do not clear
                use = bool(submitted) or bool(existing.get(entity_key, ""))
            processed[entity_key] = (
                submitted or existing.get(entity_key, "")
            ) if use else ""

    def _process_polling_api(
        self,
        user_input: dict[str, Any],
        processed: dict[str, Any],
        errors: dict[str, str],
    ) -> None:
        """Flatten polling_api section inputs and validate intervals."""
        if "polling_api" in user_input:
            section = user_input["polling_api"]
            for key in [
                "day_start_hour",
                "night_start_hour",
                "refresh_debounce_seconds",
                "api_history_retention_days",
                "smart_actions_debounce_seconds",
                "device_sync_delay_seconds",
                "hot_water_timer_duration",
                "presence_min_refresh_minutes",
                "weather_min_refresh_minutes",
                "mobile_devices_min_refresh_minutes",
            ]:
                if key in section:
                    processed[key] = section[key]

            # Fix persistence bug: explicitly handle custom intervals
            processed["custom_day_interval"] = section.get("custom_day_interval")
            processed["custom_night_interval"] = section.get("custom_night_interval")

            # mobile_devices_frequent_sync (moved from mobile_tracking section)
            if "mobile_devices_frequent_sync" in section:
                processed["mobile_devices_frequent_sync"] = section["mobile_devices_frequent_sync"]

        # Validate custom day interval (0 = auto/adaptive)
        day_interval = processed.get("custom_day_interval")
        if day_interval is not None and day_interval == 0:
            processed["custom_day_interval"] = None
        elif day_interval is not None and (day_interval < 1 or day_interval > MAX_CUSTOM_INTERVAL):
            errors["custom_day_interval"] = "interval_out_of_range"
            processed["custom_day_interval"] = None

        # Validate custom night interval (0 = auto/adaptive)
        night_interval = processed.get("custom_night_interval")
        if night_interval is not None and night_interval == 0:
            processed["custom_night_interval"] = None
        elif night_interval is not None and (night_interval < 1 or night_interval > MAX_CUSTOM_INTERVAL):
            errors["custom_night_interval"] = "interval_out_of_range"
            processed["custom_night_interval"] = None

    def _process_internet_bridge(
        self,
        user_input: dict[str, Any],
        processed: dict[str, Any],
    ) -> None:
        """Flatten internet_bridge section: bridge credentials with collapsed-section preservation."""
        if "internet_bridge" not in user_input:
            return
        section = user_input["internet_bridge"]
        existing = self.config_entry.options
        # Preserve existing credentials if section collapsed (no submitted values)
        bridge_serial = (section.get("bridge_serial") or "").strip()
        bridge_auth_key = (section.get("bridge_auth_key") or "").strip()
        processed["bridge_serial"] = bridge_serial or existing.get("bridge_serial", "")
        processed["bridge_auth_key"] = bridge_auth_key or existing.get("bridge_auth_key", "")

    def _process_weather_compensation(
        self,
        user_input: dict[str, Any],
        processed: dict[str, Any],
        errors: dict[str, str],
    ) -> None:
        """Flatten weather_compensation section: 12 WC tuning fields with min/max validation."""
        if "weather_compensation" not in user_input:
            return
        section = user_input["weather_compensation"]
        for key in (
            "wc_heating_system_preset",
            "wc_slope",
            "wc_design_outdoor_temp",
            "wc_max_flow_temp",
            "wc_min_flow_temp",
            "wc_shutoff_temp",
            "wc_smoothing_method",
            "wc_smoothing_window",
            "wc_room_compensation_enabled",
            "wc_room_compensation_factor",
            "wc_step_size",
            "wc_hysteresis",
        ):
            if key in section:
                processed[key] = section[key]

        # Validate min_flow <= max_flow
        wc_min = processed.get("wc_min_flow_temp", 25.0)
        wc_max = processed.get("wc_max_flow_temp", 65.0)
        if wc_min > wc_max:
            errors["weather_compensation"] = "wc_min_exceeds_max"

    async def _load_zones_with_heating_power(self) -> list[dict[str, str]]:
        """Load zones that have heatingPower for thermal analytics multi-select."""
        if not hasattr(self.config_entry, "runtime_data") or self.config_entry.runtime_data is None:
            return []
        coordinator = self.config_entry.runtime_data
        data_loader = coordinator.data_loader
        zones_info = await self.hass.async_add_executor_job(data_loader.load_zones_info_file)
        zones_data = await self.hass.async_add_executor_job(data_loader.load_zones_file)

        result: list[dict[str, str]] = []
        if zones_data and zones_info:
            zone_states = zones_data.get("zoneStates") or {}
            zone_names_map = {
                str(z.get("id")): z.get("name", f"Zone {z.get('id')}")
                for z in zones_info
            }
            for zone_id, zone_data in zone_states.items():
                activity_data = zone_data.get("activityDataPoints") or {}
                if activity_data.get("heatingPower") is not None:
                    zone_name = zone_names_map.get(zone_id, f"Zone {zone_id}")
                    result.append({"value": zone_id, "label": zone_name})
        return result

    async def async_step_zone_config(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show zone picker for per-zone configuration."""
        coordinator = self.config_entry.runtime_data
        data_loader = coordinator.data_loader
        zones_info = await self.hass.async_add_executor_job(data_loader.load_zones_info_file)

        if not zones_info:
            return self.async_abort(reason="no_zones")

        # Build zone options (exclude HOT_WATER, external sensors are heating/AC only)
        zone_options = [
            {"value": str(z.get("id")), "label": z.get("name", f"Zone {z.get('id')}")}
            for z in zones_info
            if is_climate_zone(z.get("type", ""))
        ]

        if not zone_options:
            return self.async_abort(reason="no_zones")

        if user_input is not None:
            self._selected_zone_id = user_input["zone_id"]
            return await self.async_step_zone_sensor_config()

        return self.async_show_form(
            step_id="zone_config",
            data_schema=vol.Schema(
                {
                    vol.Required("zone_id"): SelectSelector(
                        SelectSelectorConfig(
                            options=zone_options,  # type: ignore[typeddict-item]
                            mode=SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                },
            ),
        )

    def _process_zone_sensor_input(
        self, user_input: dict[str, Any], existing_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Flatten and process zone sensor config form sections into key-value pairs."""
        all_values: dict[str, Any] = {}
        existing = existing_config or {}

        if "heating_section" in user_input:
            s = user_input["heating_section"]
            all_values["heating_type"] = s.get(
                "heating_type", HEATING_TYPE_RADIATOR,
            ).lower()
            all_values["ufh_buffer_minutes"] = int(s.get("ufh_buffer_minutes", 30))
            all_values["adaptive_preheat"] = s.get("adaptive_preheat", "off")

        if "comfort_section" in user_input:
            s = user_input["comfort_section"]
            raw_mode = s.get("smart_comfort_mode", "None")
            all_values["smart_comfort_mode"] = raw_mode.lower() if raw_mode != "None" else "none"
            all_values["window_type"] = s.get("window_type", "double_pane")
            all_values["window_predicted_mode"] = WINDOW_DETECTION_MODE_MAP.get(
                s.get("window_predicted_mode", "auto"), WINDOW_DETECTION_MODE_DEFAULT,
            )
            all_values["window_predicted_sensitivity"] = WINDOW_SENSITIVITY_MAP.get(
                s.get("window_predicted_sensitivity", "Medium"), "medium",
            )

        if "sensor_section" in user_input:
            s = user_input["sensor_section"]
            # A collapsed section omits its fields, so a missing toggle key means
            # "collapsed", not "off": preserve/auto-enable rather than clear.
            submitted_temp = (s.get("external_temp_sensor") or "").strip()
            if "use_external_temp" in s:
                use_ext_temp = s["use_external_temp"]
            else:
                # Toggle not in form (collapsed), auto-enable if entity present
                use_ext_temp = bool(submitted_temp) or bool(existing.get("external_temp_sensor", ""))
            if use_ext_temp:
                all_values["external_temp_sensor"] = (
                    submitted_temp or existing.get("external_temp_sensor", "")
                )
            else:
                all_values["external_temp_sensor"] = ""
            submitted_hum = (s.get("external_humidity_sensor") or "").strip()
            if "use_external_humidity" in s:
                use_ext_hum = s["use_external_humidity"]
            else:
                use_ext_hum = bool(submitted_hum) or bool(existing.get("external_humidity_sensor", ""))
            if use_ext_hum:
                all_values["external_humidity_sensor"] = (
                    submitted_hum or existing.get("external_humidity_sensor", "")
                )
            else:
                all_values["external_humidity_sensor"] = ""
            # SVC Mode select (only present for HEATING zones with external sensor)
            if "svc_mode" in s:
                all_values["svc_mode"] = s["svc_mode"]
            if "svc_offset_min_change" in s:
                raw_min_change = float(s["svc_offset_min_change"])
                all_values["svc_offset_min_change"] = max(
                    SVC_OFFSET_MIN_CHANGE_MIN,
                    min(raw_min_change, SVC_OFFSET_MIN_CHANGE_MAX),
                )

        if "overlay_section" in user_input:
            s = user_input["overlay_section"]
            all_values["overlay_mode"] = OVERLAY_MODE_MAP.get(
                s.get("overlay_mode", OVERLAY_MODE_DEFAULT_DISPLAY), OVERLAY_MODE_DEFAULT,
            )
            raw_timer = int(s.get("timer_duration", str(TIMER_DURATION_DEFAULT)))
            all_values["timer_duration"] = max(
                TIMER_DURATION_MIN, min(raw_timer, TIMER_DURATION_MAX),
            )

        if "temperature_section" in user_input:
            s = user_input["temperature_section"]
            raw_min = float(s.get("min_temp", 5.0))
            raw_max = float(s.get("max_temp", 25.0))
            raw_surface = float(s.get("surface_temp_offset", 0.0))
            # Clamp to absolute bounds so YAML-import / service-call paths
            # that bypass the UI NumberSelector cannot persist out-of-range
            # values (defense in depth).
            clamped_min = max(
                ZONE_TEMP_MIN_FLOOR, min(raw_min, ZONE_TEMP_MAX_CEILING),
            )
            clamped_max = max(
                ZONE_TEMP_MIN_FLOOR, min(raw_max, ZONE_TEMP_MAX_CEILING),
            )
            # Inverted bounds (e.g. min=25, max=10 from hand-edit) would make
            # the valve controller fall back to defaults at runtime. Swap
            # here so the persisted values are at least self-consistent.
            if clamped_min > clamped_max:
                clamped_min, clamped_max = clamped_max, clamped_min
            all_values["min_temp"] = clamped_min
            all_values["max_temp"] = clamped_max
            all_values["surface_temp_offset"] = max(
                SURFACE_TEMP_OFFSET_MIN, min(raw_surface, SURFACE_TEMP_OFFSET_MAX),
            )
            all_values["display_temp_source"] = DISPLAY_TEMP_SOURCE_MAP.get(
                s.get("display_temp_source", "Automatic"), "auto",
            )

        return all_values

    async def async_step_zone_sensor_config(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Configure all per-zone settings for a specific zone."""
        zone_id = self._selected_zone_id
        if not zone_id:
            return self.async_abort(reason="no_zones")

        coordinator = self.config_entry.runtime_data
        zone_config_manager = coordinator.zone_config_manager
        errors: dict[str, str] = {}

        if user_input is not None:
            all_values = self._process_zone_sensor_input(
                user_input, zone_config_manager.get_zone_config(zone_id),
            )

            # SVC-active + sensor clear inline validation.
            # Clearing the external sensor on a zone with an active SVC
            # mode would silently deactivate the controller at runtime.
            # Surface this as an inline schema error so the user knows
            # the right mitigation (set SVC Mode to Off first).
            existing = zone_config_manager.get_zone_config(zone_id)
            prev_sensor = existing.get("external_temp_sensor", "")
            new_sensor = all_values.get("external_temp_sensor", "")
            active_svc_mode = all_values.get(
                "svc_mode", existing.get("svc_mode", "off"),
            )
            if prev_sensor and not new_sensor and active_svc_mode != "off":
                errors["sensor_section"] = "svc_active_sensor_clear"

            if not errors:
                # Skip stamping a key as an explicit override unless the
                # submitted value actually differs from current. window_type's
                # live default can be the global setting, not the raw merged
                # dict, so it needs its own resolved value here.
                existing_display = {
                    **existing,
                    "window_type": zone_config_manager.get_effective_window_type(
                        zone_id, coordinator.config_manager,
                    ),
                }
                for key, value in all_values.items():
                    if value == existing_display.get(key):
                        continue
                    await zone_config_manager.async_set_zone_value(zone_id, key, value)

                # Return to menu (no config entry change, zone_config.json is separate)
                return self.async_create_entry(title="", data=self.config_entry.options)

        # Load current values
        config = zone_config_manager.get_zone_config(zone_id)

        # Get zone name and type for description placeholder
        data_loader = coordinator.data_loader
        zones_info = await self.hass.async_add_executor_job(data_loader.load_zones_info_file)
        zone_name = zone_id
        zone_type = ""
        if zones_info:
            for z in zones_info:
                if str(z.get("id")) == zone_id:
                    zone_name = z.get("name", zone_id)
                    zone_type = z.get("type", "")
                    break

        # Current values with display-friendly transforms
        cur_heating = config.get("heating_type", HEATING_TYPE_RADIATOR).capitalize()
        if cur_heating == "Ufh":
            cur_heating = "UFH"
        cur_ufh_buffer = config.get("ufh_buffer_minutes", 30)
        cur_adaptive = config.get("adaptive_preheat", "off")
        # Handle legacy bool values from pre-migration configs
        if isinstance(cur_adaptive, bool):
            cur_adaptive = "active" if cur_adaptive else "off"
        cur_comfort = config.get("smart_comfort_mode", "none").capitalize()
        if cur_comfort == "None":
            cur_comfort = "None"
        cur_window_type = zone_config_manager.get_effective_window_type(
            zone_id, coordinator.config_manager,
        )
        cur_sensitivity = WINDOW_SENSITIVITY_REVERSE_MAP.get(
            config.get("window_predicted_sensitivity", WINDOW_SENSITIVITY_DEFAULT), "Medium",
        )
        cur_detection_mode = WINDOW_DETECTION_MODE_REVERSE_MAP.get(
            config.get("window_predicted_mode", WINDOW_DETECTION_MODE_DEFAULT), "auto",
        )
        cur_temp_sensor = config.get("external_temp_sensor", "")
        cur_humidity_sensor = config.get("external_humidity_sensor", "")
        cur_svc_mode = config.get("svc_mode", "off")
        cur_svc_offset_min_change = config.get("svc_offset_min_change", 0.5)
        cur_use_ext_temp = bool(cur_temp_sensor)
        cur_use_ext_humidity = bool(cur_humidity_sensor)
        cur_overlay = OVERLAY_MODE_REVERSE_MAP.get(
            config.get("overlay_mode", OVERLAY_MODE_DEFAULT), OVERLAY_MODE_DEFAULT_DISPLAY,
        )
        cur_display_temp_source = DISPLAY_TEMP_SOURCE_REVERSE_MAP.get(
            config.get("display_temp_source", "auto"), "Automatic",
        )
        cur_timer = str(config.get("timer_duration", TIMER_DURATION_DEFAULT))
        cur_min_temp = config.get("min_temp", 5.0)
        cur_max_temp = config.get("max_temp", 25.0)
        cur_surface_offset = config.get("surface_temp_offset", 0.0)

        return self.async_show_form(
            step_id="zone_sensor_config",
            data_schema=vol.Schema(
                {
                    # === Temperature Limits === (fundamental boundaries first)
                    vol.Required("temperature_section"): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Optional(
                                    "min_temp", default=cur_min_temp,
                                ): NumberSelector(
                                    NumberSelectorConfig(
                                        min=5.0, max=25.0, step=0.5,
                                        mode=NumberSelectorMode.BOX,
                                        unit_of_measurement="°C",
                                    ),
                                ),
                                vol.Optional(
                                    "max_temp", default=cur_max_temp,
                                ): NumberSelector(
                                    NumberSelectorConfig(
                                        min=15.0, max=30.0, step=0.5,
                                        mode=NumberSelectorMode.BOX,
                                        unit_of_measurement="°C",
                                    ),
                                ),
                                vol.Optional(
                                    "surface_temp_offset", default=cur_surface_offset,
                                ): NumberSelector(
                                    NumberSelectorConfig(
                                        min=SURFACE_TEMP_OFFSET_MIN,
                                        max=SURFACE_TEMP_OFFSET_MAX,
                                        step=SURFACE_TEMP_OFFSET_STEP,
                                        mode=NumberSelectorMode.BOX,
                                        unit_of_measurement="°C",
                                    ),
                                ),
                                vol.Optional(
                                    "display_temp_source", default=cur_display_temp_source,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=DISPLAY_TEMP_SOURCE_OPTIONS,
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                            },
                        ),
                        {"collapsed": False},
                    ),
                    # === Heating System === (physical hardware)
                    vol.Required("heating_section"): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Optional(
                                    "heating_type", default=cur_heating,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=HEATING_TYPE_OPTIONS,
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                                vol.Optional(
                                    "ufh_buffer_minutes", default=cur_ufh_buffer,
                                ): NumberSelector(
                                    NumberSelectorConfig(
                                        min=0, max=60, step=5,
                                        mode=NumberSelectorMode.BOX,
                                        unit_of_measurement="min",
                                    ),
                                ),
                                vol.Optional(
                                    "adaptive_preheat", default=cur_adaptive,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=["off", "active", "passive"],
                                        mode=SelectSelectorMode.DROPDOWN,
                                        translation_key="adaptive_preheat_mode",
                                    ),
                                ),
                            },
                        ),
                        {"collapsed": False},
                    ),
                    # === External Sensors === (augments Tado's built-in sensors)
                    vol.Required("sensor_section"): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Optional(
                                    "use_external_temp", default=cur_use_ext_temp,
                                ): BooleanSelector(),
                                vol.Optional(
                                    "external_temp_sensor",
                                    description={"suggested_value": cur_temp_sensor}
                                    if cur_temp_sensor else None,
                                ): EntitySelector(
                                    EntitySelectorConfig(
                                        domain="sensor", device_class="temperature",
                                    ),
                                ),
                                vol.Optional(
                                    "use_external_humidity", default=cur_use_ext_humidity,
                                ): BooleanSelector(),
                                vol.Optional(
                                    "external_humidity_sensor",
                                    description={"suggested_value": cur_humidity_sensor}
                                    if cur_humidity_sensor else None,
                                ): EntitySelector(
                                    EntitySelectorConfig(
                                        domain="sensor", device_class="humidity",
                                    ),
                                ),
                                **(
                                    {
                                        vol.Optional(
                                            "svc_mode", default=cur_svc_mode,
                                        ): SelectSelector(
                                            SelectSelectorConfig(
                                                # Ordering signals recommendation:
                                                # Offset Sync before Valve Target.
                                                options=["off", "offset_sync", "valve_target"],
                                                translation_key="svc_mode",
                                                mode=SelectSelectorMode.DROPDOWN,
                                            ),
                                        ),
                                        **(
                                            {
                                                vol.Optional(
                                                    "svc_offset_min_change", default=cur_svc_offset_min_change,
                                                ): NumberSelector(
                                                    NumberSelectorConfig(
                                                        min=0.5, max=3.0, step=0.5,
                                                        mode=NumberSelectorMode.SLIDER,
                                                        unit_of_measurement="°C",
                                                    ),
                                                ),
                                            }
                                            if cur_svc_mode == "offset_sync"
                                            else {}
                                        ),
                                    }
                                    if zone_type == "HEATING" and (cur_temp_sensor or cur_use_ext_temp)
                                    else {}
                                ),
                            },
                        ),
                        {"collapsed": True},
                    ),
                    # === Smart Features === (depends on sensors above)
                    vol.Required("comfort_section"): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Optional(
                                    "smart_comfort_mode", default=cur_comfort,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=SMART_COMFORT_MODE_OPTIONS,
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                                vol.Optional(
                                    "window_type", default=cur_window_type,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=["single_pane", "double_pane", "triple_pane", "passive_house"],
                                        translation_key="mold_risk_window_type",
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                                vol.Optional(
                                    "window_predicted_mode", default=cur_detection_mode,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=WINDOW_DETECTION_MODE_OPTIONS,
                                        translation_key="window_predicted_mode",
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                                vol.Optional(
                                    "window_predicted_sensitivity", default=cur_sensitivity,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=WINDOW_SENSITIVITY_OPTIONS,
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                            },
                        ),
                        {"collapsed": True},
                    ),
                    # === Manual Temperature Override === (runtime control)
                    vol.Required("overlay_section"): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Optional(
                                    "overlay_mode", default=cur_overlay,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=OVERLAY_MODE_OPTIONS,
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                                vol.Optional(
                                    "timer_duration", default=cur_timer,
                                ): SelectSelector(
                                    SelectSelectorConfig(
                                        options=TIMER_DURATION_OPTIONS,
                                        mode=SelectSelectorMode.DROPDOWN,
                                    ),
                                ),
                            },
                        ),
                        {"collapsed": True},
                    ),
                },
            ),
            errors=errors,
            description_placeholders={"zone_name": zone_name},
        )
