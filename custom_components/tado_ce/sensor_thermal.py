"""Tado CE Thermal Analysis Sensors: inertia, heating rate, preheat time, etc."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .device_manager import get_zone_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .insights_heating import calculate_confidence_recommendation

if TYPE_CHECKING:
    from .heating_coordinator import HeatingCycleCoordinator

_LOGGER = logging.getLogger(__name__)


class TadoThermalInertiaSensor(CoordinatorEntity["HeatingCycleCoordinator"], SensorEntity):
    """Sensor for thermal inertia time (delay before temperature rises)."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, home_id: str, coordinator: HeatingCycleCoordinator, zone_id: str, zone_name: str, zone_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._home_id = home_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        _meta = ENTITY_REGISTRY["sensor_thermal_inertia"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._attr_native_unit_of_measurement = "min"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

    @property
    def native_value(self) -> float | None:
        """Return sensor value from coordinator data."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return None
        return zone_data.get("inertia_time")

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        return super().available and zone_data is not None and zone_data.get("inertia_time") is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return {}
        return {
            "cycle_count": zone_data.get("cycle_count", 0),
            "completed_count": zone_data.get("completed_count", 0),
            "confidence_score": zone_data.get("confidence_score", 0.0),
        }


class TadoHeatingRateSensor(CoordinatorEntity["HeatingCycleCoordinator"], SensorEntity):
    """Sensor for heating rate (°C per hour)."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, home_id: str, coordinator: HeatingCycleCoordinator, zone_id: str, zone_name: str, zone_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._home_id = home_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        _meta = ENTITY_REGISTRY["sensor_heating_rate"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._attr_native_unit_of_measurement = "°C/h"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

    @property
    def native_value(self) -> float | None:
        """Return sensor value from coordinator data."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return None
        return zone_data.get("heating_rate")

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        return super().available and zone_data is not None and zone_data.get("heating_rate") is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return {}
        return {
            "cycle_count": zone_data.get("cycle_count", 0),
            "completed_count": zone_data.get("completed_count", 0),
            "confidence_score": zone_data.get("confidence_score", 0.0),
        }


class TadoPreheatTimeSensor(CoordinatorEntity["HeatingCycleCoordinator"], SensorEntity):
    """Sensor for estimated preheat time to reach target temperature."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, home_id: str, coordinator: HeatingCycleCoordinator, zone_id: str, zone_name: str, zone_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._home_id = home_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        _meta = ENTITY_REGISTRY["sensor_preheat_time"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._attr_native_unit_of_measurement = "min"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._current_temp: float | None = None
        self._target_temp: float | None = None
        self._estimated_preheat: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update cached values from coordinator data."""
        zone_state = self.coordinator.get_zone_state(self._zone_id)
        if not zone_state:
            self._current_temp = None
            self._target_temp = None
            self._estimated_preheat = None
            self.async_write_ha_state()
            return

        current_temp = zone_state.get("current_temp")
        target_temp = zone_state.get("target_temp")

        self._current_temp = current_temp
        self._target_temp = target_temp

        if current_temp is not None and target_temp is not None:
            self._estimated_preheat = self.coordinator.estimate_preheat_time(
                self._zone_id, current_temp, target_temp,
            )
        else:
            self._estimated_preheat = None

        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return cached preheat time estimate."""
        return self._estimated_preheat

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        zone_state = self.coordinator.get_zone_state(self._zone_id)
        # Need both: analysis data (heating_rate) AND current zone state (temps)
        return (
            super().available
            and zone_data is not None
            and zone_data.get("heating_rate") is not None
            and zone_state is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return {}
        return {
            "current_temp": self._current_temp,
            "target_temp": self._target_temp,
            "cycle_count": zone_data.get("cycle_count", 0),
            "completed_count": zone_data.get("completed_count", 0),
            "confidence_score": zone_data.get("confidence_score", 0.0),
        }


class TadoConfidenceSensor(CoordinatorEntity["HeatingCycleCoordinator"], SensorEntity):
    """Sensor for confidence score of preheat estimates (0-100%)."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, home_id: str, coordinator: HeatingCycleCoordinator, zone_id: str, zone_name: str, zone_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._home_id = home_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        _meta = ENTITY_REGISTRY["sensor_confidence"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

    @property
    def native_value(self) -> float | None:
        """Return sensor value from coordinator data."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return None
        # Convert 0.0-1.0 to 0-100%
        confidence = zone_data.get("confidence_score")
        if confidence is not None:
            return round(float(confidence) * 100, 1)
        return None

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        return super().available and zone_data is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return {}
        cycle_count = zone_data.get("cycle_count", 0)
        completed_count = zone_data.get("completed_count", 0)
        confidence = zone_data.get("confidence_score")
        confidence_pct = round(confidence * 100, 1) if confidence is not None else None
        recommendation = calculate_confidence_recommendation(
            confidence_percent=confidence_pct,
            zone_name=self._zone_name,
            cycle_count=cycle_count,
            completed_count=completed_count,
        )
        return {"cycle_count": cycle_count, "completed_count": completed_count, "recommendation": recommendation}


class TadoHeatingAccelerationSensor(CoordinatorEntity["HeatingCycleCoordinator"], SensorEntity):
    """Sensor for heating acceleration (°C/h²: higher = faster-response system)."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, home_id: str, coordinator: HeatingCycleCoordinator, zone_id: str, zone_name: str, zone_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._home_id = home_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        _meta = ENTITY_REGISTRY["sensor_heat_accel"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._attr_native_unit_of_measurement = "°C/h²"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

    @property
    def native_value(self) -> float | None:
        """Return sensor value from coordinator data."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return None
        return zone_data.get("acceleration")

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        return super().available and zone_data is not None and zone_data.get("acceleration") is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return {}
        return {
            "cycle_count": zone_data.get("cycle_count", 0),
            "completed_count": zone_data.get("completed_count", 0),
        }


class TadoApproachFactorSensor(CoordinatorEntity["HeatingCycleCoordinator"], SensorEntity):
    """Sensor for approach deceleration factor (second-order analysis).

    Measures how much the heating rate decreases as temperature
    approaches the setpoint. Used to predict overshoot.

    Factor interpretation:
    - 100%: No deceleration, will likely overshoot
    - 50%: 50% deceleration, controlled approach
    - 0%: Complete stop before setpoint (rare)
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, home_id: str, coordinator: HeatingCycleCoordinator, zone_id: str, zone_name: str, zone_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._home_id = home_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        _meta = ENTITY_REGISTRY["sensor_approach_factor"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

    @property
    def native_value(self) -> float | None:
        """Return sensor value from coordinator data."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return None
        return zone_data.get("approach_factor")

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        return super().available and zone_data is not None and zone_data.get("approach_factor") is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        zone_data = self.coordinator.get_zone_data(self._zone_id)
        if not zone_data:
            return {}
        return {
            "cycle_count": zone_data.get("cycle_count", 0),
            "completed_count": zone_data.get("completed_count", 0),
            "overshoot_estimate": zone_data.get("overshoot_estimate"),
        }
