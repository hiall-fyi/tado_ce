"""Tado CE Binary Sensors."""
import json
import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.core import HomeAssistant

from .const import ZONES_FILE, ZONES_INFO_FILE
from .device_manager import get_hub_device_info, get_zone_device_info

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


def _load_zones_file():
    """Load zones file (blocking)."""
    try:
        with open(ZONES_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _load_zones_info_file():
    """Load zones info file (blocking)."""
    try:
        with open(ZONES_INFO_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _get_zone_names():
    """Load zone names from API data."""
    try:
        with open(ZONES_INFO_FILE) as f:
            zones_info = json.load(f)
            return {str(z.get('id')): z.get('name', f"Zone {z.get('id')}") for z in zones_info}
    except Exception:
        return {}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE binary sensors from a config entry."""
    _LOGGER.debug("Tado CE binary_sensor: Setting up...")
    zone_names = await hass.async_add_executor_job(_get_zone_names)
    zones_info = await hass.async_add_executor_job(_load_zones_info_file)
    
    sensors = []
    
    # Home/Away sensor (global)
    sensors.append(TadoHomeSensor())
    
    # Open Window sensors (per zone that supports it)
    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone_id}")
            zone_type = zone.get('type')
            
            # Only add open window for heating zones that support it
            if zone_type == 'HEATING':
                owd = zone.get('openWindowDetection', {})
                if owd.get('supported', False):
                    sensors.append(TadoOpenWindowSensor(zone_id, zone_name, zone_type))
    
    async_add_entities(sensors, True)
    _LOGGER.info(f"Tado CE binary sensors loaded: {len(sensors)}")


class TadoHomeSensor(BinarySensorEntity):
    """Binary sensor for Tado Home/Away status."""
    
    def __init__(self):
        self._attr_name = "Tado CE Home"
        self._attr_unique_id = "tado_ce_home"
        self._attr_device_class = BinarySensorDeviceClass.PRESENCE
        self._attr_available = False
        self._attr_is_on = None
        # Use hub device info for global entities
        self._attr_device_info = get_hub_device_info()
        self._tado_mode = None
    
    @property
    def extra_state_attributes(self):
        return {
            "tado_mode": self._tado_mode,
        }
    
    def update(self):
        try:
            with open(ZONES_FILE) as f:
                data = json.load(f)
                # Get tado mode from first zone
                for zone_id, zone_data in data.get('zoneStates', {}).items():
                    self._tado_mode = zone_data.get('tadoMode')
                    if self._tado_mode:
                        self._attr_is_on = self._tado_mode == 'HOME'
                        self._attr_available = True
                        return
            self._attr_available = False
        except Exception:
            self._attr_available = False


class TadoOpenWindowSensor(BinarySensorEntity):
    """Binary sensor for Tado Open Window detection."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HEATING"):
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._attr_name = f"{zone_name} Open Window"
        # Use zone_name for unique_id to maintain entity_id stability
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_open_window"
        self._attr_device_class = BinarySensorDeviceClass.WINDOW
        self._attr_available = False
        self._attr_is_on = None
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type)
        self._detected_time = None
        self._expiry_time = None
    
    @property
    def extra_state_attributes(self):
        return {
            "detected_time": self._detected_time,
            "expiry_time": self._expiry_time,
        }
    
    def update(self):
        try:
            with open(ZONES_FILE) as f:
                data = json.load(f)
                zone_data = data.get('zoneStates', {}).get(self._zone_id)
                
                if not zone_data:
                    self._attr_available = False
                    return
                
                open_window = zone_data.get('openWindow')
                if open_window:
                    self._attr_is_on = True
                    self._detected_time = open_window.get('detectedTime')
                    self._expiry_time = open_window.get('expiryTime')
                else:
                    self._attr_is_on = False
                    self._detected_time = None
                    self._expiry_time = None
                
                self._attr_available = True
        except Exception:
            self._attr_available = False
