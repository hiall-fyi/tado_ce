"""Tado CE per-zone sensors: temperature, humidity, target, overlay, heating / AC power."""

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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .climate_helpers import ac_power_percentage
from .const import OPEN_WINDOW_DEFAULT_TEMP, SIGNAL_HOMEKIT_UPDATE
from .device_manager import get_hub_device_info, get_zone_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .helpers import (
    PerEntityAvailabilityMixin,
    get_zone_state,
    get_zone_states,
    merge_homekit_into_zone_data,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class TadoZoneSensor(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], SensorEntity):
    """Represent a base Tado zone sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Zone Sensor."""
        super().__init__(coordinator)
        self._home_id = coordinator.home_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._data_present = False
        self._attr_native_value = None
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, coordinator.home_id)
        self._unsub_homekit_signal: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Register listeners when entity is added to hass."""
        await super().async_added_to_hass()
        self._unsub_homekit_signal = async_dispatcher_connect(
            self.hass,
            SIGNAL_HOMEKIT_UPDATE.format(home_id=self._home_id),
            self._handle_homekit_signal,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister listeners when entity is removed."""
        if self._unsub_homekit_signal:
            self._unsub_homekit_signal()
            self._unsub_homekit_signal = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_homekit_signal(self, zone_id: str) -> None:
        """Handle HomeKit data update for this zone."""
        if zone_id != self._zone_id:
            return
        self._handle_coordinator_update()

    def _get_zone_data(self) -> dict[str, Any] | None:
        """Read this zone's data, layering fresh HomeKit readings on top of cloud."""
        try:
            zone_data = get_zone_state(self.coordinator.data, self._zone_id)
        except Exception:
            _LOGGER.debug(
                "Zone Sensor: could not read zone %s data, returning "
                "None so the entity falls back to unavailable",
                self._zone_id,
            )
            return None
        if zone_data is None:
            return None
        return merge_homekit_into_zone_data(zone_data, self._zone_id, self.coordinator)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        zone_data = self._get_zone_data()
        if zone_data:
            self._update_from_zone_data(zone_data)
            self._data_present = True
        else:
            self._data_present = False
        self.async_write_ha_state()

    @callback
    def _update_from_zone_data(self, zone_data: dict[str, Any]) -> None:
        pass

    @property
    def _base_zone_attributes(self) -> dict[str, str]:
        """Base extra_state_attributes shared by all zone sensors."""
        from .format_helpers import format_zone_type

        return {"zone_type": format_zone_type(self._zone_type)}


class TadoTemperatureSensor(TadoZoneSensor):
    """Represent a Tado zone temperature sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Temperature Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_temperature"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._data_source: str = "cloud"
        self._last_homekit_update: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = self._base_zone_attributes
        attrs["data_source"] = self._data_source
        if self._last_homekit_update is not None:
            attrs["last_homekit_update"] = self._last_homekit_update
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """Mark unavailable if no temperature data (HOT_WATER combi boilers)."""
        zone_data = self._get_zone_data()
        if zone_data:
            self._update_from_zone_data(zone_data)
            self._update_homekit_attributes()
            self._data_present = self._attr_native_value is not None
        else:
            self._data_present = False
        self.async_write_ha_state()

    @callback
    def _update_from_zone_data(self, zone_data: dict[str, Any]) -> None:
        sensor_data = zone_data.get("sensorDataPoints") or {}
        new_value = (sensor_data.get("insideTemperature") or {}).get("celsius")
        if new_value != self._attr_native_value:
            _LOGGER.debug(
                "Zone Sensor: zone %s temperature %s → %s°C (source %s)",
                self._zone_id, self._attr_native_value, new_value, self._data_source,
            )
        self._attr_native_value = new_value

    def _update_homekit_attributes(self) -> None:
        """Surface `data_source` + `last_homekit_update` for the user."""
        try:
            provider = self.coordinator.homekit_provider
            reconciler = self.coordinator.state_reconciler
            if provider and reconciler and provider.is_connected:
                reconciler.local_provider = provider
                zcm = self.coordinator.zone_config_manager
                display_source = (
                    zcm.get_zone_value(self._zone_id, "display_temp_source", "auto")
                    if zcm else "auto"
                )
                result = reconciler.merge_zone_temperature(
                    self._zone_id, None,
                    display_source=display_source, purpose="display",
                )
                if isinstance(result, tuple) and len(result) == 2 and result[1] == "homekit":
                    self._data_source = "homekit"
                    _, _changed, observed = provider.get_temperature(self._zone_id)
                    self._last_homekit_update = observed.isoformat() if observed else None
                    return
        except AttributeError:
            # Coordinator / provider not fully initialised on first
            # poll; fall through to the cloud default.
            pass
        self._data_source = "cloud"
        self._last_homekit_update = None


class TadoHumiditySensor(TadoZoneSensor):
    """Represent a Tado zone humidity sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Humidity Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_humidity"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._data_source: str = "cloud"
        self._last_homekit_update: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = self._base_zone_attributes
        attrs["data_source"] = self._data_source
        if self._last_homekit_update is not None:
            attrs["last_homekit_update"] = self._last_homekit_update
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """Mark unavailable if no humidity data."""
        zone_data = self._get_zone_data()
        if zone_data:
            self._update_from_zone_data(zone_data)
            self._update_homekit_attributes()
            self._data_present = self._attr_native_value is not None
        else:
            self._data_present = False
        self.async_write_ha_state()

    @callback
    def _update_from_zone_data(self, zone_data: dict[str, Any]) -> None:
        sensor_data = zone_data.get("sensorDataPoints") or {}
        new_value = (sensor_data.get("humidity") or {}).get("percentage")
        if new_value != self._attr_native_value:
            _LOGGER.debug(
                "Zone Sensor: zone %s humidity %s → %s%% (source %s)",
                self._zone_id, self._attr_native_value, new_value, self._data_source,
            )
        self._attr_native_value = new_value

    def _update_homekit_attributes(self) -> None:
        """Pin humidity source to cloud (bridge humidity lags; reconciler only flips on cloud failure)."""
        self._data_source = "cloud"
        self._last_homekit_update = None


class TadoHeatingPowerSensor(TadoZoneSensor):
    """Represent a Tado heating power percentage sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Heating Power Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_heating_power"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_from_zone_data(self, zone_data: dict[str, Any]) -> None:
        activity_data = zone_data.get("activityDataPoints") or {}
        power = (activity_data.get("heatingPower") or {}).get("percentage")
        self._attr_native_value = power if power is not None else 0


class TadoACPowerSensor(TadoZoneSensor):
    """Represent a Tado AC power state sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "AIR_CONDITIONING",
    ) -> None:
        """Initialize the ACPower Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_ac_power"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_from_zone_data(self, zone_data: dict[str, Any]) -> None:
        activity_data = zone_data.get("activityDataPoints") or {}
        self._attr_native_value = ac_power_percentage(activity_data)


class TadoBoilerFlowTemperatureSensor(
    PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], SensorEntity,
):
    """Hub-level boiler flow temperature, sourced from whichever zone reports it."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        """Initialize the Boiler Flow Temperature Sensor."""
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["sensor_boiler_flow_temp"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_info = get_hub_device_info(coordinator.home_id)
        self._data_present = False
        self._attr_native_value = None
        self._source_zone: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "source_zone": self._source_zone,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        try:
            data = self.coordinator.data
            if not data:
                self._data_present = False
                self.async_write_ha_state()
                return

            zone_states = get_zone_states(data)
            for zone_id, zone_data in zone_states.items():
                activity_data = zone_data.get("activityDataPoints") or {}
                flow_temp = (activity_data.get("boilerFlowTemperature") or {}).get("celsius")
                if flow_temp is not None:
                    self._attr_native_value = flow_temp
                    self._source_zone = zone_id
                    self._data_present = True
                    self.async_write_ha_state()
                    return

            self._attr_native_value = None
            self._source_zone = None
            self._data_present = False
        except Exception:
            _LOGGER.debug(
                "Zone Sensor: boiler flow temperature update failed, "
                "marking unavailable until the next poll",
                exc_info=True,
            )
            self._data_present = False
        self.async_write_ha_state()


class TadoTargetTempSensor(TadoZoneSensor):
    """Represent a Tado target temperature sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Target Temp Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_target"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_suggested_display_precision = 1
        self._attr_icon = _meta.icon

    @callback
    def _update_from_zone_data(self, zone_data: dict[str, Any]) -> None:
        setting = zone_data.get("setting") or {}
        if setting.get("power") == "ON":
            self._attr_native_value = (setting.get("temperature") or {}).get("celsius")
        elif zone_data.get("overlayType") == "MANUAL":
            # A manual OFF overlay is frost protection, same as the climate card.
            self._attr_native_value = OPEN_WINDOW_DEFAULT_TEMP
        else:
            # Schedule/Away OFF: no target to show. The climate entity's own
            # target here can go stale when the schedule lookup fails, so
            # mirroring it is a separate, undecided question.
            self._attr_native_value = None


class TadoOverlaySensor(TadoZoneSensor):
    """Represent a Tado overlay mode sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Overlay Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_overlay"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._next_change = None
        self._next_temp = None
        self._overlay_expiry: str | None = None
        self._overlay_remaining: int | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        attrs: dict[str, Any] = {
            "next_change": self._next_change,
            "next_temperature": self._next_temp,
        }
        if self._overlay_expiry is not None:
            attrs["overlay_expiry"] = self._overlay_expiry
        if self._overlay_remaining is not None:
            attrs["overlay_remaining_seconds"] = self._overlay_remaining
        return attrs

    @callback
    def _update_from_zone_data(self, zone_data: dict[str, Any]) -> None:
        overlay_type = zone_data.get("overlayType")
        setting = zone_data.get("setting") or {}
        power = setting.get("power")

        if power == "OFF":
            self._attr_native_value = "Off"
        elif overlay_type == "MANUAL":
            self._attr_native_value = "Manual"
        else:
            self._attr_native_value = "Schedule"

        overlay = zone_data.get("overlay") or {}
        termination = overlay.get("termination") or {}
        if termination.get("type") == "TIMER":
            self._overlay_expiry = termination.get("expiry") or termination.get("projectedExpiry")
            self._overlay_remaining = termination.get("remainingTimeInSeconds")
        else:
            self._overlay_expiry = None
            self._overlay_remaining = None

        if self._overlay_expiry:
            # Timer overlays revert to the schedule, but the schedule
            # target at that moment isn't deterministic from the zone
            # snapshot alone, so leave next_temp blank.
            self._next_change = self._overlay_expiry
            self._next_temp = None
        else:
            next_change = zone_data.get("nextScheduleChange")
            if next_change:
                self._next_change = next_change.get("start")
                next_setting = next_change.get("setting")
                if next_setting:
                    temp = next_setting.get("temperature")
                    self._next_temp = temp.get("celsius") if temp else None
                else:
                    self._next_temp = None
            else:
                self._next_change = None
                self._next_temp = None

