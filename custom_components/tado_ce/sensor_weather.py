"""Tado CE home-level weather sensors."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .format_helpers import WEATHER_STATE_MAP
from .helpers import PerEntityAvailabilityMixin

if TYPE_CHECKING:
    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class TadoOutsideTemperatureSensor(
    PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], SensorEntity,
):
    """Outside temperature from Tado weather data."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["sensor_outside_temp"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_entity_category = get_entity_category(_meta)
        self._data_present = False
        self._attr_native_value = None
        self._timestamp = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {"timestamp": self._timestamp}

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Refresh outside temperature from coordinator weather data."""
        try:
            data = (self.coordinator.data or {}).get("weather")
            if data:
                temp_data = data.get("outsideTemperature") or {}
                self._attr_native_value = temp_data.get("celsius")
                self._timestamp = temp_data.get("timestamp")
                self._data_present = self._attr_native_value is not None
            else:
                # No weather block on this poll: unavailable, and drop the last
                # value so a stale reading does not linger.
                self._attr_native_value = None
                self._data_present = False
        except Exception:
            _LOGGER.debug(
                "Weather Sensor: outside temperature update failed, "
                "marking unavailable until the next poll",
                exc_info=True,
            )
            self._attr_native_value = None
            self._data_present = False


class TadoSolarIntensitySensor(
    PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], SensorEntity,
):
    """Solar intensity from Tado weather data."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["sensor_solar_intensity"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        self._attr_icon = _meta.icon
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = get_entity_category(_meta)
        self._data_present = False
        self._attr_native_value = None
        self._timestamp = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {"timestamp": self._timestamp}

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Refresh solar intensity percentage from coordinator weather data."""
        try:
            data = (self.coordinator.data or {}).get("weather")
            if data:
                solar_data = data.get("solarIntensity") or {}
                self._attr_native_value = solar_data.get("percentage")
                self._timestamp = solar_data.get("timestamp")
                self._data_present = self._attr_native_value is not None
            else:
                # No weather block on this poll: unavailable, and drop the last
                # value so a stale reading does not linger.
                self._attr_native_value = None
                self._data_present = False
        except Exception:
            _LOGGER.debug(
                "Weather Sensor: solar intensity update failed, "
                "marking unavailable until the next poll",
                exc_info=True,
            )
            self._attr_native_value = None
            self._data_present = False


class TadoWeatherStateSensor(
    PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], SensorEntity,
):
    """Weather state from Tado weather data."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["sensor_weather"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._data_present = False
        self._attr_native_value = None
        self._raw_state = None
        self._timestamp = None

    @property
    def icon(self) -> str | None:
        """Return the icon for the entity."""
        icons = {
            "SUN": "mdi:weather-sunny",
            "CLOUDY": "mdi:weather-cloudy",
            "CLOUDY_MOSTLY": "mdi:weather-cloudy",
            "CLOUDY_PARTLY": "mdi:weather-partly-cloudy",
            "RAIN": "mdi:weather-rainy",
            "SCATTERED_RAIN": "mdi:weather-partly-rainy",
            "DRIZZLE": "mdi:weather-rainy",
            "SNOW": "mdi:weather-snowy",
            "FOGGY": "mdi:weather-fog",
            "NIGHT_CLEAR": "mdi:weather-night",
            "NIGHT_CLOUDY": "mdi:weather-night-partly-cloudy",
            "THUNDERSTORMS": "mdi:weather-lightning",
            "WINDY": "mdi:weather-windy",
        }
        return icons.get(self._raw_state, "mdi:weather-partly-cloudy")  # type: ignore[call-overload, no-any-return]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "raw_state": self._raw_state,
            "timestamp": self._timestamp,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Refresh weather-state condition (sunny / cloudy / rain / etc.) from coordinator data."""
        try:
            data = (self.coordinator.data or {}).get("weather")
            if data:
                weather_data = data.get("weatherState") or {}
                self._raw_state = weather_data.get("value")
                self._timestamp = weather_data.get("timestamp")
                self._attr_native_value = WEATHER_STATE_MAP.get(self._raw_state, self._raw_state)  # type: ignore[arg-type]
                self._data_present = self._attr_native_value is not None
            else:
                # No weather block on this poll: unavailable, and drop the last
                # value so a stale reading does not linger.
                self._attr_native_value = None
                self._raw_state = None
                self._data_present = False
        except Exception:
            _LOGGER.debug(
                "Weather Sensor: weather state update failed, marking "
                "unavailable until the next poll",
                exc_info=True,
            )
            self._attr_native_value = None
            self._raw_state = None
            self._data_present = False
