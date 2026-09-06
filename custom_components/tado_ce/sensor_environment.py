"""Tado CE environment sensors: mold risk, condensation, comfort, dew point, surface temp."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .calculations import (
    HEAT_INDEX_ACTIVATION_TEMP,
    calculate_heat_index,
    classify_condensation_risk,
    classify_heat_risk_level,
    classify_mold_risk_by_margin,
)
from .calculations import (
    calculate_dew_point as _calculate_dew_point,
)
from .calculations import calculate_surface_rh as _calculate_surface_rh
from .calculations import (
    calculate_surface_temperature as _calculate_surface_temperature,
)
from .const import (
    _COMFORT_COLD_THRESHOLD,
    _COMFORT_COOL_THRESHOLD,
    _COMFORT_FREEZING_THRESHOLD,
    _COMFORT_HOT_THRESHOLD,
    _COMFORT_SWELTERING_THRESHOLD,
    _COMFORT_WARM_THRESHOLD,
    ENTITY_DATA_CONDENSATION_RISK,
)
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .format_helpers import (
    format_comfort_model as _format_comfort_model,
)
from .format_helpers import (
    format_window_type as _format_window_type,
)
from .format_helpers import (
    strip_zone_prefix as _strip_zone_prefix,
)
from .helpers import get_zone_states
from .insights_air_comfort import (
    classify_humidity_level,
    compute_comfort_target,
)
from .insights_environment import (
    calculate_comfort_recommendation,
    calculate_condensation_recommendation,
    calculate_heating_condensation_recommendation,
    calculate_mold_risk_recommendation,
)
from .sensor_helpers import get_effective_temperature as _get_effective_temp
from .sensor_helpers import get_outdoor_temperature as _get_outdoor_temp
from .sensor_zone import TadoZoneSensor

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .config_manager import ConfigurationManager
    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def _classify_comfort_deviation(deviation: float) -> str:
    """Classify comfort level based on temperature deviation from target."""
    if deviation < _COMFORT_FREEZING_THRESHOLD:
        return "Freezing"
    if deviation < _COMFORT_COLD_THRESHOLD:
        return "Cold"
    if deviation < _COMFORT_COOL_THRESHOLD:
        return "Cool"
    if deviation <= _COMFORT_WARM_THRESHOLD:
        return "Comfortable"
    if deviation <= _COMFORT_HOT_THRESHOLD:
        return "Warm"
    if deviation <= _COMFORT_SWELTERING_THRESHOLD:
        return "Hot"
    return "Sweltering"


def _extract_mold_risk_data(
    zone_data: dict[str, Any], hass: HomeAssistant, zone_id: str, coordinator: TadoDataUpdateCoordinator,
) -> tuple[float, float, float, float | None, float | None, str, float, float] | None:
    """Extract shared mold risk data (humidity, temps, dew point) from zone data."""
    sensor_data = zone_data.get("sensorDataPoints") or {}
    humidity = (sensor_data.get("humidity") or {}).get("percentage")
    if humidity is None:
        return None

    room_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
    if room_temp is None:
        return None

    (effective_temp, outdoor_temp, surface_temp, temperature_source, surface_temp_offset) = _get_effective_temp(
        hass,
        zone_id,
        room_temp,
        config_manager=coordinator.config_manager,
        zone_config_manager=coordinator.zone_config_manager,
    )

    dew_point = _calculate_dew_point(room_temp, humidity)

    return (
        humidity,
        room_temp,
        effective_temp,
        outdoor_temp,
        surface_temp,
        temperature_source,
        surface_temp_offset,
        dew_point,
    )


class TadoMoldRiskSensor(TadoZoneSensor):
    """Mold risk indicator sensor (state = Critical / High / Medium / Low).

    Risk Levels (based on condensation margin):
    - Critical: <3°C margin (high mold risk, condensation likely)
    - High: 3-5°C margin (elevated risk, monitor closely)
    - Medium: 5-7°C margin (moderate risk, improve ventilation)
    - Low: >7°C margin (safe, good conditions)
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Mold Risk Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_mold_risk"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

        # Attributes
        self._room_temp: float | None = None  # Room temp from Tado sensor
        self._effective_temp: float | None = None  # Effective temp used for calculation
        self._humidity: float | None = None
        self._dew_point: float | None = None
        self._margin: float | None = None
        self._temperature_source: str = "unknown"  # Track which tier is active
        self._outdoor_temp: float | None = None  # For surface temp calculation
        self._surface_temp: float | None = None  # Calculated surface temp
        self._surface_temp_offset: float = 0.0  # Calibration offset
        self._recommendation: str = ""

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "room_temperature": self._room_temp,
            "effective_temperature": round(self._effective_temp, 1) if self._effective_temp is not None else None,
            "humidity": self._humidity,
            "dew_point": round(self._dew_point, 1) if self._dew_point is not None else None,
            "margin": self._margin,
            "mold_risk_percentage": (
                _calculate_surface_rh(self._effective_temp, self._dew_point)
                if self._effective_temp is not None and self._dew_point is not None
                else None
            ),  # RH at surface (mold risk %)
            "temperature_source": self._temperature_source,
            "outdoor_temperature": self._outdoor_temp,
            "surface_temperature": round(self._surface_temp, 1) if self._surface_temp is not None else None,
            "surface_temp_offset": self._surface_temp_offset,  # Calibration offset
            **self._base_zone_attributes,
            "recommendation": _strip_zone_prefix(self._recommendation, self._zone_name),
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on risk level."""
        if self._attr_native_value == "Critical":
            return "mdi:mushroom-outline"
        if self._attr_native_value == "High":
            return "mdi:alert-circle"
        if self._attr_native_value == "Medium":
            return "mdi:alert"
        return "mdi:check-circle"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update mold risk based on temperature and humidity."""
        try:
            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            result = _extract_mold_risk_data(zone_data, self.hass, self._zone_id, self.coordinator)
            if result is None:
                self._data_present = False
                return

            (
                self._humidity,
                self._room_temp,
                self._effective_temp,
                self._outdoor_temp,
                self._surface_temp,
                self._temperature_source,
                self._surface_temp_offset,
                self._dew_point,
            ) = result

            # Calculate margin (difference between effective temperature and dew point)
            margin_exact = self._effective_temp - self._dew_point
            self._margin = round(margin_exact, 1)  # Rounded for display only

            # Determine risk level using exact margin to preserve monotonicity
            self._attr_native_value = classify_mold_risk_by_margin(margin_exact)

            # Get target temperature from zone data for specific recommendations
            target_temp = None
            if zone_data:
                setting = zone_data.get("setting") or {}
                target_temp = (setting.get("temperature") or {}).get("celsius")

            self._recommendation = calculate_mold_risk_recommendation(
                risk_level=self._attr_native_value,
                zone_name=self._zone_name,
                humidity=self._humidity,
                surface_temp=self._effective_temp,
                dew_point=self._dew_point,
                current_temp=self._room_temp,
                target_temp=target_temp,
            )

            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Environment Sensor: zone %s mold risk update failed "
                "(%s), marking unavailable until the next poll",
                self._zone_id, e,
            )
            self._data_present = False


class TadoMoldRiskPercentageSensor(TadoZoneSensor):
    """Mold risk percentage sensor: surface relative humidity (mold grows above ~70-80%)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Mold Risk Percentage Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_mold_risk_pct"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Attributes
        self._room_temp: float | None = None
        self._effective_temp: float | None = None
        self._humidity: float | None = None
        self._dew_point: float | None = None
        self._temperature_source: str = "unknown"
        self._outdoor_temp: float | None = None
        self._surface_temp: float | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "room_temperature": self._room_temp,
            "effective_temperature": round(self._effective_temp, 1) if self._effective_temp is not None else None,
            "humidity": self._humidity,
            "dew_point": round(self._dew_point, 1) if self._dew_point is not None else None,
            "temperature_source": self._temperature_source,
            **self._base_zone_attributes,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update mold risk percentage based on temperature and humidity."""
        try:
            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            result = _extract_mold_risk_data(zone_data, self.hass, self._zone_id, self.coordinator)
            if result is None:
                self._data_present = False
                return

            (
                self._humidity,
                self._room_temp,
                self._effective_temp,
                self._outdoor_temp,
                self._surface_temp,
                self._temperature_source,
                _,
                self._dew_point,
            ) = result

            # Calculate surface RH (mold risk percentage)
            surface_rh = (
                _calculate_surface_rh(self._effective_temp, self._dew_point)
                if self._effective_temp is not None and self._dew_point is not None
                else None
            )
            if surface_rh is None:
                self._data_present = False
                return

            self._attr_native_value = surface_rh
            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Environment Sensor: zone %s mold risk percentage "
                "update failed (%s), marking unavailable until the "
                "next poll",
                self._zone_id, e,
            )
            self._data_present = False


class TadoCondensationRiskSensor(TadoZoneSensor):
    """Condensation risk sensor for all climate zones.

    AC zones: condensation on window exterior when AC cools room.
    HEATING zones: condensation on window interior when indoor
            humidity is high and window inner surface drops below indoor dew point.

    Uses per-zone window_type configuration for U-value.

    Heating Risk Levels (aligned with Mold Risk, accounts for cold spots):
    - None: >7°C margin (safe)
    - Low: 5-7°C margin (monitor)
    - Medium: 3-5°C margin (condensation likely on coldest spots)
    - High: 1-3°C margin (condensation actively forming)
    - Critical: ≤1°C margin (heavy condensation)

    AC zones use the original thresholds (Critical <2, High 2-4, Medium 4-6, Low >6).

    State: Risk level text (Critical/High/Medium/Low/None)
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "AIR_CONDITIONING",
    ) -> None:
        """Initialize the Condensation Risk Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_condensation_risk"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

        # Common attributes
        self._room_temp: float | None = None
        self._outdoor_temp: float | None = None
        self._margin: float | None = None
        self._window_type: str = "double_pane"
        self._u_value: float | None = None
        self._recommendation: str = ""
        # AC-specific attributes
        self._outdoor_humidity: float | None = None
        self._outdoor_dew_point: float | None = None
        self._window_outer_surface_temp: float | None = None

        # Heating-specific attributes
        self._indoor_humidity: float | None = None
        self._indoor_dew_point: float | None = None
        self._surface_temperature: float | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self._zone_type == "HEATING":
            return {
                "room_temperature": self._room_temp,
                "humidity": self._indoor_humidity,
                "indoor_dew_point": round(self._indoor_dew_point, 1) if self._indoor_dew_point is not None else None,
                "surface_temperature": round(self._surface_temperature, 1)
                if self._surface_temperature is not None
                else None,
                "outdoor_temperature": self._outdoor_temp,
                "margin": self._margin,
                "window_type": _format_window_type(self._window_type),
                "u_value": self._u_value,
                **self._base_zone_attributes,
                "recommendation": _strip_zone_prefix(self._recommendation, self._zone_name),
            }
        return {
            "room_temperature": self._room_temp,
            "outdoor_temperature": self._outdoor_temp,
            "outdoor_humidity": self._outdoor_humidity,
            "outdoor_dew_point": round(self._outdoor_dew_point, 1) if self._outdoor_dew_point is not None else None,
            "window_outer_surface_temp": round(self._window_outer_surface_temp, 1)
            if self._window_outer_surface_temp is not None
            else None,
            "margin": self._margin,
            "window_type": _format_window_type(self._window_type),
            "u_value": self._u_value,
            **self._base_zone_attributes,
            "recommendation": _strip_zone_prefix(self._recommendation, self._zone_name),
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on risk level."""
        if self._attr_native_value == "Critical":
            return "mdi:water-alert"
        if self._attr_native_value == "High":
            return "mdi:alert-circle"
        if self._attr_native_value == "Medium":
            return "mdi:alert"
        return "mdi:check-circle"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update condensation risk based on zone type."""
        try:
            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            # Get room temperature (common to both zone types)
            sensor_data = zone_data.get("sensorDataPoints") or {}
            room_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
            if room_temp is None:
                self._data_present = False
                return

            self._room_temp = room_temp

            # Get config_manager and zone_config_manager from coordinator
            config_manager = self.coordinator.config_manager
            zone_config_manager = self.coordinator.zone_config_manager

            if not config_manager:
                self._data_present = False
                return

            # Get window type from per-zone config or global config
            self._window_type = zone_config_manager.get_effective_window_type(
                self._zone_id, config_manager,
            )
            self._u_value = zone_config_manager.get_window_u_value(self._zone_id, config_manager)

            if self._zone_type == "HEATING":
                self._update_heating(sensor_data, config_manager)
            else:
                self._update_ac(config_manager)

            self.coordinator.publish_entity_data(
                self._zone_id,
                ENTITY_DATA_CONDENSATION_RISK,
                {
                    "state": self._attr_native_value,
                    "recommendation": self._recommendation,
                },
            )

        except Exception as e:
            _LOGGER.debug(
                "Environment Sensor: zone %s condensation risk update "
                "failed (%s), marking unavailable until the next poll",
                self._zone_id, e,
            )
            self._data_present = False

    @callback
    def _update_heating(self, sensor_data: dict[str, Any], config_manager: ConfigurationManager) -> None:
        """Update condensation risk for HEATING zones (indoor dew point vs window inner surface)."""
        # Get indoor humidity from zone sensor data
        humidity = (sensor_data.get("humidity") or {}).get("percentage")
        if humidity is None:
            self._data_present = False
            return

        self._indoor_humidity = humidity

        # Calculate indoor dew point
        self._indoor_dew_point = _calculate_dew_point(self._room_temp, humidity)  # type: ignore[arg-type]

        # Get outdoor temperature for surface temp calculation
        # Fallback to room temp if outdoor not available (same as Mold Risk Tier 2)
        outdoor_entity = config_manager.get_outdoor_temp_entity()
        outdoor_temp = None
        if outdoor_entity:
            outdoor_temp = _get_outdoor_temp(self.hass, outdoor_entity)
        self._outdoor_temp = outdoor_temp

        effective_outdoor = outdoor_temp if outdoor_temp is not None else self._room_temp

        # Calculate window inner surface temperature (same formula as Mold Risk)
        self._surface_temperature = _calculate_surface_temperature(
            self._room_temp,  # type: ignore[arg-type]
            effective_outdoor,  # type: ignore[arg-type]
            self._u_value,  # type: ignore[arg-type]
        )

        # Apply surface_temp_offset if configured
        zone_config_manager = self.coordinator.zone_config_manager
        if zone_config_manager:
            offset = zone_config_manager.get_zone_value(
                self._zone_id,
                "surface_temp_offset",
                0.0,
            )
            if offset:
                self._surface_temperature = self._surface_temperature + float(offset)

        # Margin = surface_temp - indoor_dew_point
        # Positive = safe, Negative = condensation occurring
        margin_exact = self._surface_temperature - self._indoor_dew_point
        self._margin = round(margin_exact, 1)  # Rounded for display only

        # Heating zone risk levels (aligned with Mold Risk thresholds)
        # Real-world condensation occurs at higher margins than theoretical
        # because window edges/corners are 3-5°C colder than calculated average
        self._attr_native_value = classify_condensation_risk(margin_exact, "HEATING")

        self._recommendation = calculate_heating_condensation_recommendation(
            risk_level=self._attr_native_value,
            zone_name=self._zone_name,
            margin=self._margin,
            humidity=self._indoor_humidity,
            surface_temp=self._surface_temperature,
            dew_point=self._indoor_dew_point,
        )

        self._data_present = True

    @callback
    def _update_ac(self, config_manager: ConfigurationManager) -> None:
        """Update condensation risk for AC zones (outdoor dew point vs window outer surface)."""
        # Get outdoor temperature
        outdoor_entity = config_manager.get_outdoor_temp_entity()
        if not outdoor_entity:
            self._data_present = False
            return

        self._outdoor_temp = _get_outdoor_temp(self.hass, outdoor_entity)
        if self._outdoor_temp is None:
            self._data_present = False
            return

        # Get outdoor humidity (from weather entity)
        self._outdoor_humidity = self._get_outdoor_humidity(outdoor_entity)
        if self._outdoor_humidity is None:
            self._data_present = False
            return

        # Calculate outdoor dew point
        self._outdoor_dew_point = _calculate_dew_point(self._outdoor_temp, self._outdoor_humidity)

        # Calculate window outer surface temperature
        self._window_outer_surface_temp = _calculate_surface_temperature(
            self._outdoor_temp,
            self._room_temp,  # type: ignore[arg-type]
            self._u_value,  # type: ignore[arg-type]
        )

        # Calculate margin (difference between window outer surface temp and outdoor dew point)
        margin_exact = self._window_outer_surface_temp - self._outdoor_dew_point
        self._margin = round(margin_exact, 1)  # Rounded for display only

        # AC zone risk levels
        self._attr_native_value = classify_condensation_risk(margin_exact, "AIR_CONDITIONING")

        zone_data = self._get_zone_data()
        ac_setpoint = None
        if zone_data:
            setting = zone_data.get("setting") or {}
            ac_setpoint = (setting.get("temperature") or {}).get("celsius")

        self._recommendation = calculate_condensation_recommendation(
            risk_level=self._attr_native_value,
            zone_name=self._zone_name,
            margin=self._margin,
            ac_setpoint=ac_setpoint,
            current_temp=self._room_temp,
        )

        self._data_present = True

    def _get_outdoor_humidity(self, entity_id: str) -> float | None:
        """Read outdoor humidity from a weather entity or companion sensor."""
        if not self.hass or not entity_id:
            return None

        try:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                return None

            if entity_id.startswith("weather."):
                humidity = state.attributes.get("humidity")
                if humidity is not None:
                    return float(humidity)

            # For non-weather entities, try to find a companion humidity sensor
            # e.g., sensor.outdoor_temperature -> sensor.outdoor_humidity
            if entity_id.startswith("sensor.") and "temperature" in entity_id.lower():
                humidity_entity = entity_id.lower().replace("temperature", "humidity")
                humidity_state = self.hass.states.get(humidity_entity)
                if humidity_state and humidity_state.state not in ("unknown", "unavailable"):
                    try:
                        return float(humidity_state.state)
                    except (ValueError, TypeError):
                        pass

        except Exception as e:
            _LOGGER.debug(
                "Environment Sensor: could not read outdoor humidity "
                "from %s (%s), condensation risk falls back to "
                "indoor-only data",
                entity_id, e,
            )
            return None

        _LOGGER.debug(
            "Environment Sensor: no outdoor humidity available for "
            "%s, set a weather.* entity or pair the outdoor "
            "temperature sensor with a sensor.*_humidity sibling",
            entity_id,
        )
        return None


class TadoSurfaceTemperatureSensor(TadoZoneSensor):
    """Surface temperature sensor for calibration workflows (laser thermometer cross-check)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Surface Temperature Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_surface_temp"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

        # Attributes
        self._room_temp: float | None = None
        self._outdoor_temp: float | None = None
        self._window_type: str = "double_pane"
        self._u_value: float | None = None
        self._offset_applied: float = 0.0
        self._calculation_method: str = "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "room_temperature": self._room_temp,
            "outdoor_temperature": self._outdoor_temp,
            "window_type": _format_window_type(self._window_type),
            "u_value": self._u_value,
            "offset_applied": self._offset_applied,
            "calculation_method": self._calculation_method,
            **self._base_zone_attributes,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update surface temperature using 2-tier calculation strategy."""
        try:
            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            # Get room temperature
            sensor_data = zone_data.get("sensorDataPoints") or {}
            room_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
            if room_temp is None:
                self._data_present = False
                return

            self._room_temp = room_temp

            # Get config_manager and zone_config_manager from coordinator
            config_manager = self.coordinator.config_manager
            zone_config_manager = self.coordinator.zone_config_manager

            if not config_manager:
                # Fallback to room temperature
                self._attr_native_value = room_temp
                self._calculation_method = "room_average"
                self._outdoor_temp = None
                self._window_type = "unknown"
                self._u_value = None
                self._offset_applied = 0.0
                self._data_present = True
                return

            # Try Tier 1: Surface temperature estimation
            outdoor_entity = config_manager.get_outdoor_temp_entity()

            if outdoor_entity:
                self._outdoor_temp = _get_outdoor_temp(
                    self.hass,
                    outdoor_entity,
                    config_manager.get_use_feels_like(),
                )

                if self._outdoor_temp is not None:
                    self._window_type = zone_config_manager.get_effective_window_type(
                        self._zone_id, config_manager,
                    )
                    self._u_value = zone_config_manager.get_window_u_value(self._zone_id, config_manager)
                    self._offset_applied = zone_config_manager.get_surface_temp_offset(self._zone_id)

                    # Calculate surface temperature
                    surface_temp = _calculate_surface_temperature(
                        room_temp,
                        self._outdoor_temp,
                        self._u_value,
                    )

                    # Apply offset (for calibration)
                    if self._offset_applied != 0.0:
                        surface_temp = surface_temp + self._offset_applied
                        self._calculation_method = "calibrated_surface"
                    else:
                        self._calculation_method = "surface_estimation"

                    self._attr_native_value = round(surface_temp, 1)
                    self._data_present = True
                    return

            # Tier 2: Fallback to room temperature
            self._attr_native_value = room_temp
            self._calculation_method = "room_average"
            self._outdoor_temp = None
            self._window_type = "unknown"
            self._u_value = None
            self._offset_applied = 0.0
            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Environment Sensor: zone %s surface temperature "
                "update failed (%s), marking unavailable until the "
                "next poll",
                self._zone_id, e,
            )
            self._data_present = False


class TadoDewPointSensor(TadoZoneSensor):
    """Dew point temperature sensor (Magnus-Tetens, °C, for automation workflows)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Dew Point Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_dew_point"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

        # Attributes
        self._room_temp: float | None = None
        self._humidity: float | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "room_temperature": self._room_temp,
            "humidity": self._humidity,
            "calculation_method": "Magnus-Tetens",
            **self._base_zone_attributes,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update dew point based on room temperature and humidity."""
        try:
            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            # Get temperature and humidity from zone data
            sensor_data = zone_data.get("sensorDataPoints") or {}
            self._room_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
            self._humidity = (sensor_data.get("humidity") or {}).get("percentage")

            if self._room_temp is None or self._humidity is None:
                self._data_present = False
                return

            # Calculate dew point using Magnus-Tetens formula
            self._attr_native_value = round(_calculate_dew_point(self._room_temp, self._humidity), 1)
            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Environment Sensor: zone %s dew point update failed "
                "(%s), marking unavailable until the next poll",
                self._zone_id, e,
            )
            self._data_present = False


class TadoComfortLevelSensor(TadoZoneSensor):
    """Comfort level sensor using ASHRAE 55 adaptive comfort (with humidity suffix).

    Comfort target = 0.31 * outdoor_temp + 17.8°C, ±3°C acceptable band (90% acceptability).
    Falls back to latitude-based seasonal thresholds when outdoor temp unavailable.
    State: combined comfort text (e.g., "Comfortable", "Cool Dry").
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Comfort Level Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_comfort_level"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

        # Attributes
        self._temperature: float | None = None
        self._humidity: float | None = None
        self._outdoor_temp: float | None = None
        self._comfort_temp: float | None = None
        self._comfort_model: str = "unknown"
        self._dew_point: float | None = None
        self._recommendation: str = ""
        self._heat_index: float | None = None
        self._heat_risk_level: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "temperature": self._temperature,
            "humidity": self._humidity,
            "outdoor_temperature": self._outdoor_temp,
            "comfort_target": self._comfort_temp,
            "comfort_model": _format_comfort_model(self._comfort_model),
            "dew_point": round(self._dew_point, 1) if self._dew_point is not None else None,
            "heat_index": round(self._heat_index, 1) if self._heat_index is not None else None,
            "heat_risk_level": self._heat_risk_level,
            "humidity_level": classify_humidity_level(self._humidity),
            **self._base_zone_attributes,
            "recommendation": _strip_zone_prefix(self._recommendation, self._zone_name),
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on comfort level."""
        state = self._attr_native_value or ""
        if "Freezing" in state or "Cold" in state:  # type: ignore[operator]
            return "mdi:snowflake-alert"
        if "Cool" in state:  # type: ignore[operator]
            return "mdi:thermometer-low"
        if "Comfortable" in state:  # type: ignore[operator]
            return "mdi:emoticon-happy"
        if "Warm" in state:  # type: ignore[operator]
            return "mdi:thermometer-high"
        if "Hot" in state or "Sweltering" in state:  # type: ignore[operator]
            return "mdi:fire-alert"
        return "mdi:air-filter"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    def _get_hvac_mode_from_coordinator(self) -> str | None:
        """Get HVAC mode from coordinator data (no cross-entity hass.states.get)."""
        coord_data = self.coordinator.data or {}
        zone_states = get_zone_states(coord_data)
        zone_state = zone_states.get(self._zone_id) or zone_states.get(str(self._zone_id))
        if not zone_state:
            return None
        setting = zone_state.get("setting") or {}
        power = setting.get("power")
        overlay_type = zone_state.get("overlayType")
        if power == "ON":
            return "heat" if overlay_type == "MANUAL" else "auto"
        if overlay_type == "MANUAL":
            return "off"
        return "auto"

    @callback
    def update(self) -> None:
        """Update air comfort using adaptive comfort model."""
        try:
            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            sensor_data = zone_data.get("sensorDataPoints") or {}
            self._temperature = (sensor_data.get("insideTemperature") or {}).get("celsius")
            self._humidity = (sensor_data.get("humidity") or {}).get("percentage")

            if self._temperature is None:
                self._data_present = False
                return

            if self._humidity is not None:
                self._dew_point = _calculate_dew_point(self._temperature, self._humidity)

            # Compute Heat Index (warm side only)
            if (
                self._temperature is not None
                and self._humidity is not None
                and self._temperature >= HEAT_INDEX_ACTIVATION_TEMP
            ):
                self._heat_index = calculate_heat_index(self._temperature, self._humidity)
                self._heat_risk_level = classify_heat_risk_level(self._heat_index)
            else:
                self._heat_index = None
                self._heat_risk_level = None

            # Get outdoor temperature from config
            config_manager_ref = self.coordinator.config_manager
            if config_manager_ref:
                _ot_entity = config_manager_ref.get_outdoor_temp_entity()
                _ot_feels = config_manager_ref.get_use_feels_like()
                self._outdoor_temp = _get_outdoor_temp(self.hass, _ot_entity, _ot_feels)
            else:
                self._outdoor_temp = None

            # Calculate comfort level
            if self._outdoor_temp is not None:
                comfort_level = self._calculate_adaptive_comfort()
                self._comfort_model = "adaptive"
            else:
                comfort_level = self._calculate_seasonal_comfort()
                self._comfort_model = "seasonal"

            humidity_suffix = self._get_humidity_suffix()
            self._attr_native_value = comfort_level + humidity_suffix

            self._recommendation = calculate_comfort_recommendation(
                comfort_state=comfort_level,
                zone_name=self._zone_name,
                current_temp=self._temperature,
                target_temp=self._comfort_temp,
                humidity=self._humidity,
                hvac_mode=self._get_hvac_mode_from_coordinator(),
                heat_index=self._heat_index,
                heat_risk_level=self._heat_risk_level,
            )

            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Environment Sensor: zone %s comfort level update "
                "failed (%s), marking unavailable until the next poll",
                self._zone_id, e,
            )
            self._data_present = False

    def _calculate_adaptive_comfort(self) -> str:
        """Calculate comfort using ASHRAE 55 Adaptive Comfort model (0.31*outdoor + 17.8°C, ±3°C band)."""
        latitude = 51.5 if not self.hass else (self.hass.config.latitude or 51.5)
        day_of_year = dt_util.now().timetuple().tm_yday
        self._comfort_temp, deviation = compute_comfort_target(
            self._temperature,  # type: ignore[arg-type]
            self._humidity,
            self._outdoor_temp,
            latitude,
            day_of_year,
        )
        return _classify_comfort_deviation(deviation)

    def _calculate_seasonal_comfort(self) -> str:
        """Calculate comfort using latitude-based seasonal thresholds."""
        latitude = 51.5 if not self.hass else (self.hass.config.latitude or 51.5)
        day_of_year = dt_util.now().timetuple().tm_yday
        self._comfort_temp, deviation = compute_comfort_target(
            self._temperature,  # type: ignore[arg-type]
            self._humidity,
            None,  # outdoor_temp=None triggers seasonal path in helper
            latitude,
            day_of_year,
        )
        return _classify_comfort_deviation(deviation)

    def _get_humidity_suffix(self) -> str:
        """Get humidity suffix for comfort display (Dry <35%, Humid >70%)."""
        level = classify_humidity_level(self._humidity)
        if level == "dry":
            return " Dry"
        if level == "humid":
            return " Humid"
        return ""
