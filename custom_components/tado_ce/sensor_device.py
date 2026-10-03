"""Tado CE per-device battery sensor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .device_manager import get_device_name_suffix, get_zone_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .format_helpers import (
    format_battery_state as _format_battery_state,
)
from .format_helpers import (
    format_connection_state_attr as _format_connection_state_attr,
)
from .format_helpers import (
    strip_zone_prefix as _strip_zone_prefix,
)
from .helpers import PerEntityAvailabilityMixin, mask_serial
from .insights_device import (
    calculate_battery_recommendation,
)

if TYPE_CHECKING:
    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class TadoBatterySensor(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], SensorEntity):
    """Represent a Tado device battery state sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str,
        device: dict[str, Any],
        zones_info: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._device_serial = device.get("shortSerialNo", "unknown")
        self._device_type = device.get("deviceType", "unknown")

        _meta = ENTITY_REGISTRY["sensor_battery"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = (
            f"tado_ce_{coordinator.home_id}"
            f"_{_meta.unique_id_suffix.format(serial=self._device_serial)}"
        )
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._data_present = True
        self._attr_native_value = _format_battery_state(device.get("batteryState", "unknown"))
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, coordinator.home_id)

        suffix = get_device_name_suffix(zone_id, self._device_serial, self._device_type, zones_info or [])
        if suffix:
            _meta_suffixed = ENTITY_REGISTRY["sensor_battery_suffixed"]
            self._attr_translation_key = _meta_suffixed.translation_key
            self._attr_translation_placeholders = {"device_suffix": suffix}

        self._firmware = device.get("currentFwVersion")
        self._connection_state = (device.get("connectionState") or {}).get("value")
        self._connection_timestamp = (device.get("connectionState") or {}).get("timestamp")
        self._recommendation: str = ""

    @property
    def icon(self) -> str | None:
        """Return the icon for the entity."""
        if self._attr_native_value == "Low":
            return "mdi:battery-low"
        if self._attr_native_value == "Critical":
            return "mdi:battery-alert"
        return "mdi:battery"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "device_serial": mask_serial(self._device_serial),
            "device_type": self._device_type,
            "firmware_version": self._firmware,
            "connection_state": _format_connection_state_attr(self._connection_state),
            "connection_timestamp": self._connection_timestamp,
            "recommendation": _strip_zone_prefix(self._recommendation, self._zone_name),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Refresh battery / firmware / connection state from coordinator data."""
        try:
            zones_info = (self.coordinator.data or {}).get("zones_info")
            if zones_info:
                for zone in zones_info:
                    # `devices` can come back as null on a fresh zone, so
                    # `or []` covers both null and missing-key cases.
                    for device in zone.get("devices") or []:
                        if device.get("shortSerialNo") == self._device_serial:
                            raw_battery = device.get("batteryState", "unknown")
                            self._attr_native_value = _format_battery_state(raw_battery)
                            self._firmware = device.get("currentFwVersion")
                            conn = device.get("connectionState") or {}
                            self._connection_state = conn.get("value")
                            self._connection_timestamp = conn.get("timestamp")

                            self._recommendation = calculate_battery_recommendation(
                                battery_state=raw_battery,
                                zone_name=self._zone_name,
                                device_type=self._device_type,
                            )

                            self._data_present = True
                            return
            # Device serial no longer present in the account: unavailable, and
            # drop the last-known battery level so no stale value lingers.
            self._attr_native_value = None
            self._data_present = False
        except (KeyError, TypeError, AttributeError) as err:
            _LOGGER.debug(
                "Battery Sensor: %s update failed (%s), entity marked "
                "unavailable until the next poll",
                mask_serial(self._device_serial), err,
            )
            self._attr_native_value = None
            self._data_present = False
