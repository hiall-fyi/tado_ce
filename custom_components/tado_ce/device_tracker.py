"""Tado CE Device Tracker (Presence Detection)."""
import json
import logging
from datetime import timedelta

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.core import HomeAssistant

from .const import MOBILE_DEVICES_FILE
from .device_manager import get_hub_device_info
from .data_loader import load_mobile_devices_file

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE device trackers from a config entry."""
    _LOGGER.debug("Tado CE device_tracker: Setting up...")
    mobile_devices = await hass.async_add_executor_job(load_mobile_devices_file)
    
    trackers = []
    
    if mobile_devices:
        for device in mobile_devices:
            device_id = device.get('id')
            device_name = device.get('name', f"Device {device_id}")
            settings = device.get('settings') or {}
            
            # Only create tracker if geo tracking is enabled
            if settings.get('geoTrackingEnabled', False):
                trackers.append(TadoDeviceTracker(device_id, device_name, device))
            else:
                _LOGGER.debug(f"Skipping {device_name} - geoTrackingEnabled is False")
    
    if trackers:
        async_add_entities(trackers, True)
        _LOGGER.info(f"Tado CE device trackers loaded: {len(trackers)}")
    else:
        _LOGGER.info("Tado CE: No devices with geo tracking enabled")


class TadoDeviceTracker(TrackerEntity):
    """Tado CE Device Tracker Entity."""
    
    def __init__(self, device_id: int, device_name: str, device_data: dict):
        self._device_id = device_id
        self._device_name = device_name
        self._device_data = device_data
        
        self._attr_name = f"Tado CE {device_name}"
        self._attr_unique_id = f"tado_ce_device_{device_id}"
        self._attr_available = False
        # Use hub device info for global entities
        self._attr_device_info = get_hub_device_info()
        
        self._is_home = None
        self._location = None
        self._bearing = None
        self._relative_distance = None
    
    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS
    
    @property
    def is_connected(self) -> bool:
        return self._is_home is not None
    
    @property
    def location_name(self) -> str | None:
        if self._is_home is True:
            return "home"
        elif self._is_home is False:
            return "not_home"
        return None
    
    @property
    def extra_state_attributes(self):
        metadata = self._device_data.get('deviceMetadata') or {}
        return {
            "device_id": self._device_id,
            "platform": metadata.get('platform'),
            "os_version": metadata.get('osVersion'),
            "model": metadata.get('model'),
            "bearing": self._bearing,
            "relative_distance": self._relative_distance,
        }
    
    def update(self):
        """Update device tracker state from JSON file."""
        try:
            with open(MOBILE_DEVICES_FILE) as f:
                devices = json.load(f)
                
                for device in devices:
                    if device.get('id') == self._device_id:
                        self._device_data = device
                        location = device.get('location')
                        
                        if location:
                            self._is_home = location.get('atHome')
                            self._bearing = (location.get('bearingFromHome') or {}).get('degrees')
                            self._relative_distance = location.get('relativeDistanceFromHomeFence')
                        else:
                            # No location data - device might not have geo tracking
                            self._is_home = None
                        
                        self._attr_available = True
                        return
                
            self._attr_available = False
        except Exception:
            self._attr_available = False
