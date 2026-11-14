"""Tado CE binary sensors: home / away, open window, preheat now, connectivity."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ENTITY_DATA_PREHEAT_ADVISOR,
    ENTITY_DATA_PREHEAT_NOW,
)
from .device_manager import (
    get_bridge_device_info,
    get_device_name_suffix,
    get_hub_device_info,
    get_zone_device_info,
)
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .format_helpers import (
    format_confidence as _format_confidence,
)
from .format_helpers import (
    format_data_source as _format_data_source,
)
from .format_helpers import (
    format_tado_mode as _format_tado_mode,
)
from .format_helpers import (
    format_zone_type as _format_zone_type,
)
from .format_helpers import (
    strip_zone_prefix as _strip_zone_prefix,
)
from .helpers import (
    PerEntityAvailabilityMixin,
    connection_value_to_bool,
    get_zone_state,
    get_zone_states,
    mask_serial,
    parse_iso_datetime,
)
from .insights_device import calculate_connection_recommendation

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE binary sensors from a config entry."""
    _LOGGER.debug("Binary Sensor: setup starting")
    coordinator = entry.runtime_data
    data_loader = coordinator.data_loader
    home_id = coordinator.home_id
    config_manager = coordinator.config_manager
    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)

    # Check if Smart Comfort is enabled (required for Preheat Now sensor)
    smart_comfort_enabled = config_manager.get_smart_comfort_enabled()

    sensors: list[BinarySensorEntity] = []

    sensors.append(TadoHomeSensor(coordinator))

    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_name = zone.get("name", f"Zone {zone_id}")
            zone_type = zone.get("type")

            # Only add open window for heating zones that support it
            if zone_type == "HEATING":
                owd = zone.get("openWindowDetection") or {}
                if owd.get("supported", False):
                    sensors.append(TadoOpenWindowSensor(coordinator, zone_id, zone_name, zone_type, home_id))

                # Add Preheat Now sensor if Smart Comfort is enabled
                if smart_comfort_enabled:
                    sensors.append(TadoPreheatNowSensor(coordinator, zone_id, zone_name, zone_type, home_id))

    # Bridge connected sensor: local pairing when configured, cloud-only otherwise
    bridge_serial = entry.options.get("bridge_serial")
    bridge_auth_key = entry.options.get("bridge_auth_key")
    if bridge_serial and bridge_auth_key and coordinator.config_manager.get_bridge_enabled():
        sensors.append(TadoBridgeConnectedSensor(coordinator, bridge_serial))
        _LOGGER.debug("Binary Sensor: bridge connected sensor created (local)")
    else:
        cloud_bridge = _find_bridge_device(zones_info, coordinator.data.get("home_devices"))
        cloud_serial = (cloud_bridge or {}).get("shortSerialNo")
        if cloud_serial:
            sensors.append(TadoBridgeConnectedSensor(coordinator, cloud_serial))
            _LOGGER.debug("Binary Sensor: bridge connected sensor created (cloud-only)")

    # Device connection sensors (per device)
    if zones_info:
        _create_device_connection_sensors(coordinator, zones_info, sensors)

    # Hot water power sensors (per HOT_WATER zone)
    zones_data = await hass.async_add_executor_job(data_loader.load_zones_file)
    if zones_data:
        zone_states = zones_data.get("zoneStates") or {}
        zone_types = {str(z.get("id")): z.get("type") or "" for z in (zones_info or [])}
        zone_names = {str(z.get("id")): z.get("name", f"Zone {z.get('id')}") for z in (zones_info or [])}
        for zone_id in zone_states:
            if zone_types.get(zone_id) == "HOT_WATER":
                zone_name = zone_names.get(zone_id, f"Zone {zone_id}")
                sensors.append(TadoHotWaterPowerBinarySensor(coordinator, zone_id, zone_name))

    # HomeKit connected sensor (when homekit_enabled)
    if config_manager.get_homekit_enabled():
        sensors.append(TadoHomeKitConnectedSensor(coordinator))
        _LOGGER.debug("Binary Sensor: HomeKit connected sensor created")

    # `update_before_add=False`, `self.hass` isn't wired up yet, so
    # the entity's `update()` would fail on a coordinator-data read.
    async_add_entities(sensors, False)
    _LOGGER.info("Binary Sensor: created %d entity(ies)", len(sensors))


# Internet Bridge / Gateway device types as they appear in the home device
# list. A GW bridge has no Bridge API (that boundary is TADO_BRIDGE_MODELS),
# but it still lists here with its own connectionState, so reading its online
# flag is valid and independent of the Bridge-API boundary.
_BRIDGE_DEVICE_TYPES = ("IB01", "IB02", "GW01", "GW02", "GW03")


def _find_bridge_in_home_devices(home_devices: list[Any] | None) -> dict[str, Any] | None:
    """Return the bridge (Internet Bridge / Gateway) entry from a home device list."""
    for dev in home_devices or []:
        if isinstance(dev, dict) and dev.get("deviceType") in _BRIDGE_DEVICE_TYPES:
            return dev
    return None


def _find_bridge_device(
    zones_info: list[Any] | None, home_devices: list[Any] | None,
) -> dict[str, Any] | None:
    """Return the bridge/gateway device dict from either a zone's own devices or the home level."""
    for zone in zones_info or []:
        for dev in zone.get("devices") or []:
            if isinstance(dev, dict) and dev.get("deviceType") in _BRIDGE_DEVICE_TYPES:
                return dev
    return _find_bridge_in_home_devices(home_devices)


def _create_device_connection_sensors(
    coordinator: TadoDataUpdateCoordinator,
    zones_info: list[dict[str, Any]],
    sensors: list[BinarySensorEntity],
) -> None:
    """Create device connection binary sensors (one per device)."""
    device_zones: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {}
    for zone in zones_info:
        zone_id = str(zone.get("id"))
        zone_name = zone.get("name", f"Zone {zone_id}")
        zone_type = zone.get("type") or ""
        for device in zone.get("devices") or []:
            serial = device.get("shortSerialNo")
            if serial:
                device_zones.setdefault(serial, []).append((zone_id, zone_name, zone_type, device))

    _type_priority = {"HEATING": 0, "AIR_CONDITIONING": 1}
    for zone_list in device_zones.values():
        zone_list.sort(key=lambda item: _type_priority.get(item[2], 2))
        zone_id, zone_name, zone_type, device = zone_list[0]
        if "connectionState" in device:
            sensors.append(
                TadoDeviceConnectionBinarySensor(coordinator, zone_id, zone_name, zone_type, device, zones_info),
            )


class TadoHomeSensor(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], BinarySensorEntity):
    """Binary sensor for Tado Home/Away status (reads home_state.json with zones.json fallback)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["binary_sensor_home"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        self._attr_device_class = BinarySensorDeviceClass.PRESENCE
        self._attr_entity_category = get_entity_category(_meta)
        self._data_present = False
        self._attr_is_on = None
        # Use hub device info for global entities
        self._attr_device_info = get_hub_device_info(coordinator.home_id)
        self._tado_mode: str | None = None
        self._presence_locked: bool | None = None  # Track if presence is locked (manual override)
        self._data_source: str | None = None  # Track which data source is being used

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "tado_mode": _format_tado_mode(self._tado_mode),  # type: ignore[arg-type]
            "presence_locked": self._presence_locked,
            "data_source": _format_data_source(self._data_source),  # type: ignore[arg-type]
        }

    @callback
    def update(self) -> None:
        """Update from home_state.json (primary) or zones.json (fallback)."""
        try:
            coord_data = self.coordinator.data or {}

            # Primary: Read from home_state (source of truth for presence)
            home_state = coord_data.get("home_state")
            if home_state:
                presence = home_state.get("presence", "HOME")
                self._presence_locked = home_state.get("presenceLocked", False)
                self._attr_is_on = presence == "HOME"
                self._tado_mode = presence  # Keep tado_mode attribute for compatibility
                self._data_source = "home_state"
                self._data_present = True
                return

            # Fallback: Read from zones tadoMode
            # This is used when home_state_sync_enabled=false
            zone_states = get_zone_states(coord_data)
            if zone_states:
                for zone_data in zone_states.values():
                    self._tado_mode = zone_data.get("tadoMode")
                    if self._tado_mode:
                        self._attr_is_on = self._tado_mode == "HOME"
                        self._presence_locked = zone_data.get("geolocationOverride", False)
                        self._data_source = "zones"
                        self._data_present = True
                        return

            self._data_present = False
        except Exception as e:
            _LOGGER.warning(
                "Binary Sensor: home / away update failed (%s), "
                "marking unavailable until the next poll",
                e,
            )
            self._data_present = False




class TadoOpenWindowSensor(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], BinarySensorEntity):
    """Binary sensor for Tado Open Window detection."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str = "HEATING",
        home_id: str = "",
    ) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["binary_sensor_window"]
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_class = BinarySensorDeviceClass.WINDOW
        self._data_present = False
        self._attr_is_on = None
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._detected_time = None
        self._expiry_time = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "detected_time": self._detected_time,
            "expiry_time": self._expiry_time,
        }

    @callback
    def update(self) -> None:
        """Update entity state from coordinator data."""
        try:
            coord_data = self.coordinator.data or {}
            zone_data = get_zone_state(coord_data, self._zone_id)

            if not zone_data:
                self._data_present = False
                return

            open_window = zone_data.get("openWindow")
            open_window_detected = zone_data.get("openWindowDetected", False)

            if open_window:
                self._attr_is_on = True
                self._detected_time = open_window.get("detectedTime")
                self._expiry_time = open_window.get("expiryTime")
            elif open_window_detected:
                self._attr_is_on = True
                self._detected_time = None
                self._expiry_time = None
            else:
                self._attr_is_on = False
                self._detected_time = None
                self._expiry_time = None

            self._data_present = True
        except Exception:
            _LOGGER.debug(
                "Binary Sensor: zone %s open-window update failed, "
                "marking unavailable until the next poll",
                self._zone_id,
                exc_info=True,
            )
            self._data_present = False


def _parse_preheat_start(time_str: str, now: datetime, days_ahead: int = 0) -> datetime:
    """Return the tz-aware datetime for an advisor "HH:MM" on now's date plus days_ahead (ValueError if unparseable)."""
    parsed = datetime.strptime(time_str, "%H:%M")
    day = now + timedelta(days=days_ahead)
    return day.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)


class TadoPreheatNowSensor(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], BinarySensorEntity):
    """Binary sensor indicating when to start preheating (turns ON at recommended start time)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str = "HEATING",
        home_id: str = "",
    ) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["binary_sensor_preheat_now"]
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_class = BinarySensorDeviceClass.HEAT
        self._attr_entity_category = get_entity_category(_meta)
        self._data_present = False
        self._attr_is_on = None
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)

        # Attributes for debugging/display
        self._recommended_start = None
        self._target_time = None
        self._target_temp = None
        self._current_temp = None
        self._duration_minutes = None
        self._confidence = "unknown"
        self._is_tomorrow: bool = False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "recommended_start": self._recommended_start,
            "target_time": self._target_time,
            "target_temperature": self._target_temp,
            "current_temperature": self._current_temp,
            "duration_minutes": self._duration_minutes,
            "confidence": _format_confidence(self._confidence),
            "is_tomorrow": self._is_tomorrow,
            "zone_type": _format_zone_type(self._zone_type),
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on state."""
        if self._attr_is_on:
            return "mdi:radiator"
        return "mdi:radiator-off"

    @callback
    def update(self) -> None:
        """Update preheat now status."""
        start_at: datetime | None = None
        try:
            if not self.hass:
                self._data_present = False
                return

            # Read preheat advisor data from coordinator (published by TadoPreheatAdvisorSensor)
            preheat_data = self.coordinator.get_entity_data(self._zone_id, ENTITY_DATA_PREHEAT_ADVISOR)

            # Copy attributes from preheat advisor data
            if preheat_data:
                self._target_time = preheat_data.get("target_time")
                self._target_temp = preheat_data.get("target_temperature")
                self._current_temp = preheat_data.get("current_temperature")
                self._duration_minutes = preheat_data.get("duration_minutes")
                self._confidence = preheat_data.get("confidence", "unknown")
                self._is_tomorrow = preheat_data.get("is_tomorrow", False)

            # Check for non-actionable states
            non_actionable_states = (
                "unavailable",
                "unknown",
                "No schedule",
                "Heating OFF",
                "Ready",
                "Insufficient data",
                "Away",
                None,
            )
            preheat_state_val = preheat_data.get("state") if preheat_data else None
            if not preheat_data or preheat_state_val in non_actionable_states:
                self._attr_is_on = False
                self._data_present = True
                self._recommended_start = None
                return

            # If preheat is for a future day, never trigger
            if self._is_tomorrow:
                self._attr_is_on = False
                self._data_present = True
                self._recommended_start = preheat_state_val
                try:
                    start_at = _parse_preheat_start(
                        preheat_state_val.removeprefix("Tomorrow "), dt_util.now(), days_ahead=1,
                    )
                except ValueError:
                    start_at = None
                return

            # Parse recommended start time (format: "HH:MM")
            # Note: UFH buffer is already applied in TadoPreheatAdvisorSensor
            try:
                recommended_str = preheat_state_val
                now = dt_util.now()
                recommended_time = _parse_preheat_start(recommended_str, now)

                self._recommended_start = recommended_str

                # Check if it's time to preheat
                self._attr_is_on = now >= recommended_time
                self._data_present = True
                start_at = recommended_time

            except ValueError:
                _LOGGER.debug(
                    "Binary Sensor: preheat schedule had a "
                    "non-parseable time string, preheat-now defaults "
                    "to off until the next coordinator poll provides a "
                    "valid value",
                    exc_info=True,
                )
                self._attr_is_on = False
                self._data_present = True
                self._recommended_start = None

        except Exception as e:
            _LOGGER.debug(
                "Binary Sensor: zone %s preheat-now update failed "
                "(%s), marking unavailable until the next poll",
                self._zone_id, e,
            )
            self._data_present = False
        finally:
            manager = self.coordinator.adaptive_preheat_manager
            if manager is not None:
                manager.schedule_start(self._zone_id, start_at)
            # Publish computed state to coordinator for cross-component access
            # (used by AdaptivePreheatManager initial state check)
            if self._data_present:
                self.coordinator.publish_entity_data(
                    self._zone_id,
                    ENTITY_DATA_PREHEAT_NOW,
                    {
                        "state": "on" if self._attr_is_on else "off",
                        "recommended_start": self._recommended_start,
                    },
                )


class TadoDeviceConnectionBinarySensor(
    PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], BinarySensorEntity,
):
    """Represent a Tado device connection state binary sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

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
        self._device_serial = device.get("shortSerialNo", "unknown")
        self._device_type = device.get("deviceType", "unknown")
        self._zone_name = zone_name
        self._zone_type = zone_type

        self._attr_translation_key = "connection"
        _conn_meta = ENTITY_REGISTRY["binary_sensor_connection"]
        self._attr_unique_id = (
            f"tado_ce_{coordinator.home_id}"
            f"_{_conn_meta.unique_id_suffix.format(serial=self._device_serial)}"
        )
        self._attr_entity_category = get_entity_category(_conn_meta)
        self._data_present = True
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, coordinator.home_id)

        # Add device suffix to distinguish multiple devices in the same zone
        suffix = get_device_name_suffix(zone_id, self._device_serial, self._device_type, zones_info or [])
        if suffix:
            _meta_suffixed = ENTITY_REGISTRY["binary_sensor_connection_suffixed"]
            self._attr_translation_key = _meta_suffixed.translation_key
            self._attr_translation_placeholders = {"device_suffix": suffix}

        conn = device.get("connectionState") or {}
        self._attr_is_on = connection_value_to_bool(conn)
        self._connection_timestamp = conn.get("timestamp")
        self._firmware = device.get("currentFwVersion")
        self._offline_minutes: int | None = None
        self._recommendation: str = ""

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "device_serial": mask_serial(self._device_serial),
            "device_type": self._device_type,
            "firmware_version": self._firmware,
            "last_seen": self._connection_timestamp,
            "offline_minutes": self._offline_minutes,
            "recommendation": _strip_zone_prefix(self._recommendation, self._zone_name),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update entity state from coordinator data."""
        try:
            zones_info = (self.coordinator.data or {}).get("zones_info")
            if zones_info:
                for zone in zones_info:
                    for device in zone.get("devices") or []:
                        if device.get("shortSerialNo") == self._device_serial:
                            conn = device.get("connectionState") or {}
                            self._attr_is_on = connection_value_to_bool(conn)
                            self._connection_timestamp = conn.get("timestamp")
                            self._firmware = device.get("currentFwVersion")

                            self._offline_minutes = None
                            if self._connection_timestamp and self._attr_is_on is False:
                                try:
                                    last_seen_dt = parse_iso_datetime(
                                        self._connection_timestamp,
                                    )
                                    now_utc = dt_util.utcnow()
                                    self._offline_minutes = int((now_utc - last_seen_dt).total_seconds() / 60)
                                except (ValueError, TypeError) as err:
                                    _LOGGER.debug(
                                        "Binary Sensor: device %s "
                                        "connection timestamp not "
                                        "parseable (%s), offline "
                                        "duration left blank",
                                        mask_serial(self._device_serial),
                                        err,
                                    )

                            if self._attr_is_on is True:
                                connection_state = "Online"
                            elif self._attr_is_on is False:
                                connection_state = "Offline"
                            else:
                                connection_state = "Unknown"
                            self._recommendation = calculate_connection_recommendation(
                                connection_state=connection_state,
                                zone_name=self._zone_name,
                                last_seen=self._connection_timestamp,
                                offline_minutes=self._offline_minutes,
                            )

                            self._data_present = True
                            return
            # Device serial no longer present in the account: unavailable, and
            # drop the last-known state so no stale value lingers.
            self._attr_is_on = None
            self._data_present = False
        except (KeyError, TypeError, AttributeError) as err:
            _LOGGER.debug(
                "Binary Sensor: device %s connection update failed "
                "(%s), marking unavailable until the next poll",
                mask_serial(self._device_serial), err,
            )
            self._attr_is_on = None
            self._data_present = False


class TadoHotWaterPowerBinarySensor(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], BinarySensorEntity):
    """Represent a Tado hot water power state binary sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str = "HOT_WATER",
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type

        _meta = ENTITY_REGISTRY["binary_sensor_power"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_entity_category = get_entity_category(_meta)
        self._data_present = False
        self._attr_is_on = None
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, coordinator.home_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_zone_data()
        self.async_write_ha_state()

    @callback
    def _update_from_zone_data(self) -> None:
        """Update entity state from coordinator data."""
        try:
            data = self.coordinator.data
            if data:
                zone_data = get_zone_state(data, self._zone_id)
                if zone_data:
                    setting = zone_data.get("setting") or {}
                    power = setting.get("power")
                    self._attr_is_on = power == "ON" if power else None
                    self._data_present = True
                    return
            self._data_present = False
        except Exception:
            _LOGGER.debug(
                "Binary Sensor: zone %s hot water power update "
                "failed, marking unavailable until the next poll",
                self._zone_id,
                exc_info=True,
            )
            self._data_present = False


class TadoHomeKitConnectedSensor(CoordinatorEntity["TadoDataUpdateCoordinator"], BinarySensorEntity):
    """Represent a Tado HomeKit connection status binary sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["binary_sensor_homekit_connected"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_info = get_hub_device_info(coordinator.home_id)
        self._attr_is_on = False
        self._status = "not_configured"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        client = self.coordinator.homekit_client
        stats = client.connection_stats if client else {}
        uptime: str | None = None
        if client and client.is_connected and stats.get("last_connected"):
            try:
                connected_dt = parse_iso_datetime(stats["last_connected"])
                delta = dt_util.utcnow() - connected_dt
                uptime = str(delta).split(".")[0]  # Remove microseconds
            except (ValueError, TypeError):
                _LOGGER.debug("binary_sensor: corrupt last_connected %r, omitting uptime", stats.get("last_connected"))

        # Mapped/unmapped zone counts
        mapped_zones = 0
        unmapped_zones = 0
        if client and hasattr(client, "zone_aid_map"):
            mapped_zones = len(client.zone_aid_map)
            data = self.coordinator.data or {}
            zones_info = data.get("zones_info") or []
            if zones_info:
                from .const import get_climate_zone_ids

                all_climate_ids = get_climate_zone_ids(zones_info)
                unmapped_zones = len(all_climate_ids) - mapped_zones

        return {
            "last_connected": stats.get("last_connected"),
            "last_disconnected": stats.get("last_disconnected") or "Never",
            "reconnect_count": stats.get("reconnect_count", 0),
            "uptime": uptime,
            "status": self._status,
            "mapped_zones": mapped_zones,
            "unmapped_zones": max(0, unmapped_zones),
            **self.coordinator.homekit_write_metrics,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        client = self.coordinator.homekit_client
        if client is None:
            self._attr_is_on = False
            self._status = "not_configured"
        elif client.is_connected:
            self._attr_is_on = True
            self._status = "connected"
        else:
            self._attr_is_on = False
            self._status = "disconnected"
        self.async_write_ha_state()


class TadoBridgeConnectedSensor(
    CoordinatorEntity["TadoDataUpdateCoordinator"],
    BinarySensorEntity,
):
    """Binary sensor for Bridge API connectivity."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator, bridge_serial: str) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["binary_sensor_bridge_connected"]
        self._attr_device_info = get_bridge_device_info(bridge_serial, coordinator.home_id)
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        self._attr_translation_key = _meta.translation_key
        self._attr_entity_registry_enabled_default = _meta.enabled_default
        self._attr_entity_category = get_entity_category(_meta)
        if _meta.icon:
            self._attr_icon = _meta.icon

    @callback
    def _handle_coordinator_update(self) -> None:
        """State = the bridge's real online flag.

        Primary: `bridgeConnected` from the bridge-auth poll (zero cloud quota),
        present whenever a boiler-side device is wired and local pairing is
        configured. Fallback: the bridge/gateway device's own `connectionState`
        from zones_info or the home device list, used whenever the primary
        isn't a plain bool: no local pairing configured, the first local poll
        hasn't landed yet, or a boiler-less body collapsed without
        `bridgeConnected`.
        """
        data = self.coordinator.data or {}
        bridge = data.get("bridge")
        flag = bridge.get("bridgeConnected") if isinstance(bridge, dict) else None
        if isinstance(flag, bool):
            self._attr_is_on = flag
        else:
            dev = _find_bridge_device(data.get("zones_info"), data.get("home_devices"))
            self._attr_is_on = connection_value_to_bool((dev or {}).get("connectionState"))
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available when we can actually observe the bridge.

        Locally paired: the health tracker records whether recent bridge API
        calls succeeded, only flipping to disconnected after several
        consecutive failures so a single transient timeout doesn't flap this.
        Cloud-only (no health tracker, since nothing is polling it locally):
        falls back to whether the coordinator's last cloud sync succeeded.
        """
        health = self.coordinator.bridge_health_tracker
        if health is not None:
            return health.state.is_connected
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return health metrics as extra state attributes."""
        health = self.coordinator.bridge_health_tracker
        if health is None:
            return {}
        s = health.state
        return {
            "last_successful_poll": (
                s.last_successful_poll.isoformat() if s.last_successful_poll else None
            ),
            "consecutive_failures": s.consecutive_failures,
            "last_error": s.last_error,
            "response_time_ms": s.last_response_time_ms,
        }
