"""Tado CE configuration manager: typed reads from `config_entry.options` with validation.

Reads live from `config_entry.options` on every access so users get
real-time effect when they flip a runtime-only toggle, falling back to
a snapshot taken at construction only when the live options are empty.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .const import MAX_CUSTOM_INTERVAL

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Default configuration values
DEFAULT_WEATHER_ENABLED = False
DEFAULT_MOBILE_DEVICES_ENABLED = False
DEFAULT_MOBILE_DEVICES_FREQUENT_SYNC = False
DEFAULT_OFFSET_ENABLED = False
DEFAULT_QUOTA_RESERVE_ENABLED = True  # Quota Reserve Protection default ON
DEFAULT_DAY_START_HOUR = 7
DEFAULT_NIGHT_START_HOUR = 23
MIN_HOUR: int = 0
MAX_HOUR: int = 23
DEFAULT_API_HISTORY_RETENTION_DAYS = 14  # 0 = keep forever
DEFAULT_HOT_WATER_TIMER_DURATION = 60  # minutes
DEFAULT_REFRESH_DEBOUNCE_SECONDS = 15  # Debounce delay for immediate refresh
MIN_REFRESH_DEBOUNCE_SECONDS: int = 1
MAX_REFRESH_DEBOUNCE_SECONDS: int = 60
DEFAULT_SCHEDULE_CALENDAR_ENABLED = False  # Schedule Calendar (opt-in)
DEFAULT_SMART_COMFORT_ENABLED = False  # Smart Comfort analytics (opt-in)
DEFAULT_OUTDOOR_TEMP_ENTITY = ""  # Outdoor temperature entity for weather compensation
DEFAULT_BOILER_FLOW_TEMP_ENTITY = ""  # External boiler flow-temp entity for bridgeless homes
DEFAULT_WEATHER_COMPENSATION = "none"  # Weather compensation preset
DEFAULT_USE_FEELS_LIKE = False  # Use feels-like temperature instead of actual
DEFAULT_SMART_COMFORT_HISTORY_DAYS = 7  # Days of temperature history to keep for rate calculation
DEFAULT_MOLD_RISK_WINDOW_TYPE = "double_pane"  # Window type for mold risk surface temperature calculation

# Validation constants
MIN_HOUR = 0
MAX_HOUR = 23
MIN_TIMER_DURATION = 1  # minutes
MAX_TIMER_DURATION = 1440  # 24 hours
MIN_SMART_COMFORT_HISTORY_DAYS = 1
MAX_SMART_COMFORT_HISTORY_DAYS = 30


class ConfigurationManager:
    """Manages configuration settings for Tado CE integration."""

    def __init__(self, config_entry: ConfigEntry, hass: HomeAssistant = None) -> None:  # type: ignore[assignment]
        self._config_entry = config_entry
        self._options: Mapping[str, Any] = config_entry.options or {}
        self._hass = hass

    def _get_option(self, key: str, default: Any) -> Any:
        """Read option live from config_entry.options for real-time effect, falling back to cached snapshot."""
        if self._config_entry and self._config_entry.options:
            return self._config_entry.options.get(key, default)
        return self._options.get(key, default)

    def _get_int_option(self, key: str, default: int, min_val: int, max_val: int) -> int:
        """Get integer option with float→int conversion and range validation.

        Handles HA NumberSelector (returns float), legacy TextSelector (returns str),
        and out-of-range values (returns default with warning).
        """
        value = self._get_option(key, default)
        if isinstance(value, float):
            value = int(value)
        elif isinstance(value, str):
            if not value.strip():
                return default
            try:
                value = int(float(value))
            except (ValueError, TypeError, OverflowError):
                _LOGGER.warning(
                    "Config: %s value %r could not be parsed as int "
                    ", falling back to default %s",
                    key, value, default,
                )
                return default
        if not isinstance(value, int) or value < min_val or value > max_val:
            _LOGGER.warning(
                "Config: %s value %r outside range %d–%d, falling "
                "back to default %s",
                key, value, min_val, max_val, default,
            )
            return default
        return value

    def _get_float_option(self, key: str, default: float, min_val: float, max_val: float) -> float:
        """Read a float option, falling back to `default` when missing or out of range."""
        value = self._get_option(key, default)
        if isinstance(value, (int, float)) and min_val <= value <= max_val:
            return float(value)
        _LOGGER.warning(
            "Config: %s value %r outside range %s–%s, falling back "
            "to default %s",
            key, value, min_val, max_val, default,
        )
        return default

    def get_weather_enabled(self) -> bool:
        """Check if weather sensors are enabled."""
        return self._get_option("weather_enabled", DEFAULT_WEATHER_ENABLED)  # type: ignore[no-any-return]

    def get_comfort_heat_vulnerable_group(self) -> bool:
        """Alert one heat-index band earlier for homes with vulnerable occupants."""
        return self._get_option("comfort_heat_vulnerable_group", False)  # type: ignore[no-any-return]

    def get_heating_circuit_enabled(self) -> bool:
        """Check if the per-zone heating-circuit select is enabled."""
        return self._get_option("heating_circuit_enabled", False)  # type: ignore[no-any-return]

    def get_mobile_devices_enabled(self) -> bool:
        """Check if mobile device tracking is enabled."""
        return self._get_option("mobile_devices_enabled", DEFAULT_MOBILE_DEVICES_ENABLED)  # type: ignore[no-any-return]

    def get_mobile_devices_frequent_sync(self) -> bool:
        """Check if mobile devices should be synced every quick sync."""
        return self._get_option("mobile_devices_frequent_sync", DEFAULT_MOBILE_DEVICES_FREQUENT_SYNC)  # type: ignore[no-any-return]

    def get_presence_min_refresh_minutes(self) -> int:
        """Get minimum refresh interval for home presence / home state (minutes)."""
        from .const import (
            DEFAULT_PRESENCE_MIN_REFRESH_MINUTES,
            MAX_PRESENCE_MIN_REFRESH_MINUTES,
            MIN_PRESENCE_MIN_REFRESH_MINUTES,
        )
        return self._get_int_option(
            "presence_min_refresh_minutes",
            DEFAULT_PRESENCE_MIN_REFRESH_MINUTES,
            MIN_PRESENCE_MIN_REFRESH_MINUTES,
            MAX_PRESENCE_MIN_REFRESH_MINUTES,
        )

    def get_mobile_devices_min_refresh_minutes(self) -> int:
        """Get minimum refresh interval for mobile device locations (minutes)."""
        from .const import (
            DEFAULT_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
            MAX_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
            MIN_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
        )
        return self._get_int_option(
            "mobile_devices_min_refresh_minutes",
            DEFAULT_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
            MIN_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
            MAX_MOBILE_DEVICES_MIN_REFRESH_MINUTES,
        )

    def get_weather_min_refresh_minutes(self) -> int:
        """Get minimum refresh interval for weather data (minutes)."""
        from .const import (
            DEFAULT_WEATHER_MIN_REFRESH_MINUTES,
            MAX_WEATHER_MIN_REFRESH_MINUTES,
            MIN_WEATHER_MIN_REFRESH_MINUTES,
        )
        return self._get_int_option(
            "weather_min_refresh_minutes",
            DEFAULT_WEATHER_MIN_REFRESH_MINUTES,
            MIN_WEATHER_MIN_REFRESH_MINUTES,
            MAX_WEATHER_MIN_REFRESH_MINUTES,
        )

    def get_offset_enabled(self) -> bool:
        """Check if temperature offset attribute is enabled on climate entities."""
        return self._get_option("offset_enabled", DEFAULT_OFFSET_ENABLED)  # type: ignore[no-any-return]

    def get_home_state_sync_enabled(self) -> bool:
        """Check if home state sync is enabled (for away mode switch and climate presets)."""
        return self._get_option("home_state_sync_enabled", False)  # type: ignore[no-any-return]

    def get_quota_reserve_enabled(self) -> bool:
        """Check if Quota Reserve Protection is enabled (pauses polling at low quota, blocks manual actions at critical)."""
        return self._get_option("quota_reserve_enabled", DEFAULT_QUOTA_RESERVE_ENABLED)  # type: ignore[no-any-return]

    def get_day_start_hour(self) -> int:
        """Get configured day start hour (default 7am)."""
        return self._get_int_option("day_start_hour", DEFAULT_DAY_START_HOUR, MIN_HOUR, MAX_HOUR)

    def get_night_start_hour(self) -> int:
        """Get configured night start hour (default 11pm)."""
        return self._get_int_option("night_start_hour", DEFAULT_NIGHT_START_HOUR, MIN_HOUR, MAX_HOUR)

    def _get_optional_interval(self, key: str) -> int | None:
        """Read an optional polling interval; `None` when missing, blank, or invalid."""
        interval = self._get_option(key, None)
        if interval is None:
            return None

        if isinstance(interval, float):
            interval = int(interval)
        elif isinstance(interval, str):
            if not interval.strip():
                return None
            try:
                interval = int(float(interval))
            except (ValueError, TypeError, OverflowError):
                _LOGGER.warning(
                    "Config: %s value %r could not be parsed as int "
                    ", ignoring this interval, polling will use the "
                    "automatic schedule",
                    key, interval,
                )
                return None

        if not isinstance(interval, int) or interval < 1 or interval > MAX_CUSTOM_INTERVAL:
            _LOGGER.warning(
                "Config: %s value %r outside range 1–%d, ignoring "
                "this interval, polling will use the automatic "
                "schedule",
                key, interval, MAX_CUSTOM_INTERVAL,
            )
            return None
        return interval

    def get_custom_day_interval(self) -> int | None:
        """Get custom day polling interval in minutes (None if not configured)."""
        return self._get_optional_interval("custom_day_interval")

    def get_custom_night_interval(self) -> int | None:
        """Get custom night polling interval in minutes (None if not configured)."""
        return self._get_optional_interval("custom_night_interval")

    def get_api_history_retention_days(self) -> int:
        """Get API call history retention period in days (0 = keep forever, default 14)."""
        return self._get_int_option("api_history_retention_days", DEFAULT_API_HISTORY_RETENTION_DAYS, 0, 365)

    def get_hot_water_timer_duration(self) -> int:
        """Get hot water timer duration in minutes (1-1440, default 60)."""
        return self._get_int_option("hot_water_timer_duration", DEFAULT_HOT_WATER_TIMER_DURATION, MIN_TIMER_DURATION, MAX_TIMER_DURATION)

    def get_refresh_debounce_seconds(self) -> int:
        """Get refresh debounce delay in seconds (higher = fewer API calls, slower UI; default 15)."""
        return self._get_int_option("refresh_debounce_seconds", DEFAULT_REFRESH_DEBOUNCE_SECONDS, MIN_REFRESH_DEBOUNCE_SECONDS, MAX_REFRESH_DEBOUNCE_SECONDS)

    def get_schedule_calendar_enabled(self) -> bool:
        """Check if Schedule Calendar is enabled (opt-in feature)."""
        return self._get_option("schedule_calendar_enabled", DEFAULT_SCHEDULE_CALENDAR_ENABLED)  # type: ignore[no-any-return]

    def get_smart_comfort_enabled(self) -> bool:
        """Check if Smart Comfort analytics is enabled (opt-in)."""
        return self._get_option("smart_comfort_enabled", DEFAULT_SMART_COMFORT_ENABLED)  # type: ignore[no-any-return]

    def get_outdoor_temp_entity(self) -> str:
        """Get the outdoor temperature entity for weather compensation (any provider)."""
        return self._get_option("outdoor_temp_entity", DEFAULT_OUTDOOR_TEMP_ENTITY)  # type: ignore[no-any-return]

    def get_boiler_flow_temp_entity(self) -> str:
        """Get the external boiler flow-temperature entity (bridgeless homes)."""
        return self._get_option(  # type: ignore[no-any-return]
            "boiler_flow_temp_entity", DEFAULT_BOILER_FLOW_TEMP_ENTITY,
        )

    def get_smart_comfort_mode(self) -> str:
        """Get the Smart Comfort mode preset ('none' / 'light' / 'moderate' / 'aggressive')."""
        # Check new key first, fallback to legacy weather_compensation for backward compatibility
        return self._get_option(  # type: ignore[no-any-return]
            "smart_comfort_mode",
            self._get_option("weather_compensation", DEFAULT_WEATHER_COMPENSATION),
        )

    def get_use_feels_like(self) -> bool:
        """Check if feels-like (apparent) temperature should be used for weather compensation."""
        return self._get_option("use_feels_like", DEFAULT_USE_FEELS_LIKE)  # type: ignore[no-any-return]

    def get_smart_comfort_history_days(self) -> int:
        """Get Smart Comfort temperature history retention in days (1-30, default 7)."""
        return self._get_int_option("smart_comfort_history_days", DEFAULT_SMART_COMFORT_HISTORY_DAYS, MIN_SMART_COMFORT_HISTORY_DAYS, MAX_SMART_COMFORT_HISTORY_DAYS)

    def get_mold_risk_window_type(self) -> str:
        """Get the window type for mold risk surface temperature calculation (window U-value)."""
        window_type = self._get_option("mold_risk_window_type", DEFAULT_MOLD_RISK_WINDOW_TYPE)

        # Validate against known window types
        from .const import WINDOW_U_VALUES

        if window_type not in WINDOW_U_VALUES:
            _LOGGER.warning(
                "Config: mold_risk_window_type value %r is not in "
                "WINDOW_U_VALUES, falling back to default %s",
                window_type,
                DEFAULT_MOLD_RISK_WINDOW_TYPE,
            )
            return DEFAULT_MOLD_RISK_WINDOW_TYPE

        return window_type  # type: ignore[no-any-return]

    def get_adaptive_preheat_enabled(self) -> bool:
        """Check if Adaptive Preheat is enabled (local replacement for Tado's cloud Early Start)."""
        return self._get_option("adaptive_preheat_enabled", False)  # type: ignore[no-any-return]

    def get_heating_cycle_min_cycles(self) -> int:
        """Get minimum cycles required for thermal analytics (1-10, default 3)."""
        return self._get_int_option("heating_cycle_min_cycles", 3, 1, 10)

    def get_heating_cycle_history_days(self) -> int:
        """Get heating cycle history retention in days (1-30, default 7)."""
        return self._get_int_option("heating_cycle_history_days", 7, 1, 30)

    def get_heating_cycle_inertia_threshold(self) -> float:
        """Get thermal inertia detection threshold in °C (0.05-0.5, default 0.1)."""
        return self._get_float_option("heating_cycle_inertia_threshold", 0.1, 0.05, 0.5)

    def get_thermal_analytics_enabled(self) -> bool:
        """Check if Thermal Analytics sensors are enabled (opt-in)."""
        return self._get_option("thermal_analytics_enabled", False)  # type: ignore[no-any-return]

    def get_thermal_analytics_zones(self) -> list[str]:
        """Get list of zone IDs enabled for Thermal Analytics (empty list = all zones with heatingPower)."""
        zones = self._get_option("thermal_analytics_zones", [])
        if isinstance(zones, list):
            return [str(z) for z in zones]
        return []


    def get_wc_enabled(self) -> bool:
        """Check if weather compensation is enabled."""
        return self._get_option("wc_enabled", False)  # type: ignore[no-any-return]

    def get_wc_heating_system_preset(self) -> str:
        """Get the heating system preset ('radiators_standard' / 'radiators_low_temp' / 'underfloor' / 'custom')."""
        preset = self._get_option("wc_heating_system_preset", "radiators_standard")
        if preset in ("radiators_standard", "radiators_low_temp", "underfloor", "custom"):
            return preset  # type: ignore[no-any-return]
        return "radiators_standard"

    def get_wc_slope(self) -> float:
        """Get the heating curve slope (0.3-3.0, default 1.5)."""
        return self._get_float_option("wc_slope", 1.5, 0.3, 3.0)

    def get_wc_design_outdoor_temp(self) -> float:
        """Get the design outdoor temperature in °C (-30 to 10, default -5)."""
        return self._get_float_option("wc_design_outdoor_temp", -5.0, -30.0, 10.0)

    def get_wc_max_flow_temp(self) -> float:
        """Get the maximum flow temperature in °C (25-80, default 65)."""
        return self._get_float_option("wc_max_flow_temp", 65.0, 25.0, 80.0)

    def get_wc_min_flow_temp(self) -> float:
        """Get the minimum flow temperature in °C (25-60, default 25)."""
        return self._get_float_option("wc_min_flow_temp", 25.0, 25.0, 60.0)

    def get_wc_shutoff_temp(self) -> float:
        """Get the heating shutoff outdoor temperature in °C (5-30, default 18)."""
        return self._get_float_option("wc_shutoff_temp", 18.0, 5.0, 30.0)

    def get_wc_smoothing_method(self) -> str:
        """Get the outdoor temperature smoothing method ('none' / 'ema' / 'rolling_average', default 'ema')."""
        method = self._get_option("wc_smoothing_method", "ema")
        if method in ("none", "ema", "rolling_average"):
            return method  # type: ignore[no-any-return]
        return "ema"

    def get_wc_smoothing_window(self) -> int:
        """Get the smoothing window duration in minutes (15-1440, default 60)."""
        return self._get_int_option("wc_smoothing_window", 60, 15, 1440)

    def get_wc_room_compensation_enabled(self) -> bool:
        """Check if indoor temperature feedback (room compensation) is enabled."""
        return self._get_option("wc_room_compensation_enabled", False)  # type: ignore[no-any-return]

    def get_wc_room_compensation_factor(self) -> float:
        """Get the room compensation factor in °C flow per °C indoor deviation (1.0-5.0, default 3.0)."""
        return self._get_float_option("wc_room_compensation_factor", 3.0, 1.0, 5.0)

    def get_wc_step_size(self) -> float:
        """Get the flow temperature step size in °C (0.5-2.0, default 1.0)."""
        return self._get_float_option("wc_step_size", 1.0, 0.5, 2.0)

    def get_wc_hysteresis(self) -> float:
        """Get the hysteresis dead band for flow temperature changes in °C (0.5-3.0, default 1.0)."""
        return self._get_float_option("wc_hysteresis", 1.0, 0.5, 3.0)

    # ------------------------------------------------------------------
    # API Write Optimization
    # ------------------------------------------------------------------

    def get_smart_actions_debounce_seconds(self) -> int:
        """Get Smart Actions debounce window in seconds (0-10, default 3; 0 = disabled)."""
        from .const import (
            SMART_ACTIONS_DEBOUNCE_DEFAULT,
            SMART_ACTIONS_DEBOUNCE_MAX,
            SMART_ACTIONS_DEBOUNCE_MIN,
        )

        return self._get_int_option(
            "smart_actions_debounce_seconds",
            SMART_ACTIONS_DEBOUNCE_DEFAULT,
            SMART_ACTIONS_DEBOUNCE_MIN,
            SMART_ACTIONS_DEBOUNCE_MAX,
        )


    def get_device_sync_delay_seconds(self) -> float:
        """Get Device Sync delay between sequential device operations in seconds (0.5-5.0, default 1.0)."""
        from .const import (
            DEVICE_SYNC_DELAY_DEFAULT,
            DEVICE_SYNC_DELAY_MAX,
            DEVICE_SYNC_DELAY_MIN,
        )

        return self._get_float_option(
            "device_sync_delay_seconds",
            DEVICE_SYNC_DELAY_DEFAULT,
            DEVICE_SYNC_DELAY_MIN,
            DEVICE_SYNC_DELAY_MAX,
        )

    def has_bridge_credentials(self) -> bool:
        """Check if Internet Bridge serial + auth key are both stored."""
        return bool(self._get_option("bridge_serial", "")) and bool(
            self._get_option("bridge_auth_key", ""),
        )

    def get_bridge_enabled(self) -> bool:
        """Check if the Internet Bridge toggle is on, defaulting to credential presence."""
        return self._get_option("bridge_enabled", self.has_bridge_credentials())  # type: ignore[no-any-return]

    def get_homekit_enabled(self) -> bool:
        """Check if HomeKit local control is enabled."""
        return self._get_option("homekit_enabled", False)  # type: ignore[no-any-return]

    def get_homekit_cloud_sync_minutes(self) -> int:
        """Get how often to check Tado's servers for cloud-only data when HomeKit is connected (minutes)."""
        from .const import (
            DEFAULT_HOMEKIT_CLOUD_SYNC_MINUTES,
            MAX_HOMEKIT_CLOUD_SYNC_MINUTES,
            MIN_HOMEKIT_CLOUD_SYNC_MINUTES,
        )

        return self._get_int_option(
            "homekit_cloud_sync_minutes",
            DEFAULT_HOMEKIT_CLOUD_SYNC_MINUTES,
            MIN_HOMEKIT_CLOUD_SYNC_MINUTES,
            MAX_HOMEKIT_CLOUD_SYNC_MINUTES,
        )

    def get_all_config(self) -> dict[str, Any]:
        """Return the commonly-used config values as a single dict (a subset, not every getter)."""
        return {
            "weather_enabled": self.get_weather_enabled(),
            "mobile_devices_enabled": self.get_mobile_devices_enabled(),
            "mobile_devices_frequent_sync": self.get_mobile_devices_frequent_sync(),
            "offset_enabled": self.get_offset_enabled(),
            "day_start_hour": self.get_day_start_hour(),
            "night_start_hour": self.get_night_start_hour(),
            "custom_day_interval": self.get_custom_day_interval(),
            "custom_night_interval": self.get_custom_night_interval(),
            "api_history_retention_days": self.get_api_history_retention_days(),
            "hot_water_timer_duration": self.get_hot_water_timer_duration(),
        }
