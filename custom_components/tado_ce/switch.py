"""Tado CE Switch Platform (Child Lock + Early Start)."""
import json
import logging
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN, ZONES_INFO_FILE, CONFIG_FILE, MOBILE_DEVICES_FILE,
    TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID, API_ENDPOINT_DEVICES
)
from .device_manager import get_hub_device_info, get_zone_device_info
from .data_loader import load_zones_info_file, load_config_file

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE switches from a config entry."""
    _LOGGER.debug("Tado CE switch: Setting up...")
    zones_info = await hass.async_add_executor_job(load_zones_info_file)
    
    switches = []
    
    # Add Away Mode switch (global, 1 API call per toggle)
    switches.append(TadoAwayModeSwitch())
    
    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone.get('id')}")
            zone_type = zone.get('type')
            
            # Early Start switch (for heating zones that support it)
            if zone_type == 'HEATING':
                early_start = zone.get('earlyStart') or {}
                if early_start.get('supported', True):  # Default to supported
                    switches.append(TadoEarlyStartSwitch(
                        zone_id, zone_name, zone_type, early_start.get('enabled', False)
                    ))
            
            # Child Lock switches (per device)
            for device in zone.get('devices', []):
                if 'childLockEnabled' in device:
                    serial = device.get('shortSerialNo')
                    device_type = device.get('deviceType', 'unknown')
                    switches.append(TadoChildLockSwitch(
                        zone_id, serial, zone_name, zone_type, device_type, device.get('childLockEnabled', False), zones_info
                    ))
    
    if switches:
        async_add_entities(switches, True)
        _LOGGER.info(f"Tado CE switches loaded: {len(switches)}")
    else:
        _LOGGER.info("Tado CE: No switches found")


class TadoAwayModeSwitch(SwitchEntity):
    """Tado CE Away Mode Switch Entity.
    
    Allows manual control of Home/Away status.
    Uses 1 API call per toggle.
    """
    
    def __init__(self):
        self._attr_name = "Tado CE Away Mode"
        self._attr_unique_id = "tado_ce_away_mode"
        self._attr_icon = "mdi:home-export-outline"
        self._attr_is_on = False  # False = Home, True = Away
        self._attr_available = True
        self._attr_device_info = get_hub_device_info()
        self._presence_locked = False
    
    @property
    def icon(self):
        return "mdi:home-export-outline" if self._attr_is_on else "mdi:home"
    
    @property
    def extra_state_attributes(self):
        return {
            "description": "Toggle Home/Away mode manually",
            "presence_locked": self._presence_locked,
            "api_calls_per_toggle": 1,
        }
    
    def update(self):
        """Update away mode state from home state file."""
        try:
            # Try to read from home state file first (most reliable)
            try:
                import os
                state_file = os.path.join(os.path.dirname(MOBILE_DEVICES_FILE), 'home_state.json')
                with open(state_file) as f:
                    home_state = json.load(f)
                    presence = home_state.get('presence', 'HOME')
                    self._presence_locked = home_state.get('presenceLocked', False)
                    # Away mode is ON when presence is AWAY
                    self._attr_is_on = (presence == 'AWAY')
                    self._attr_available = True
                    return
            except Exception:
                pass
            
            # Fallback: check mobile devices location (if geo tracking enabled)
            with open(MOBILE_DEVICES_FILE) as f:
                mobile_devices = json.load(f)
                
            # Check if any device is at home
                any_at_home = False
                for device in mobile_devices:
                    location = device.get('location') or {}
                    if location.get('atHome', False):
                        any_at_home = True
                        break
                
                # Away mode is ON when no one is home
                self._attr_is_on = not any_at_home
                self._attr_available = True
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update away mode: {e}")
            # Keep last known state
    
    async def async_turn_on(self, **kwargs):
        """Set Away mode (everyone away) - async."""
        from .async_api import get_async_client
        
        client = get_async_client(self.hass)
        success = await client.set_presence_lock("AWAY")
        
        if success:
            self._attr_is_on = True
            self._presence_locked = True
            self.async_write_ha_state()
            await self._async_trigger_immediate_refresh("away_mode_on")
    
    async def async_turn_off(self, **kwargs):
        """Set Home mode (someone home) - async."""
        from .async_api import get_async_client
        
        client = get_async_client(self.hass)
        success = await client.set_presence_lock("HOME")
        
        if success:
            self._attr_is_on = False
            self._presence_locked = True
            self.async_write_ha_state()
            await self._async_trigger_immediate_refresh("away_mode_off")
    
    async def _async_trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, reason)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")


class TadoEarlyStartSwitch(SwitchEntity):
    """Tado CE Early Start Switch Entity."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str, initial_state: bool):
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        
        self._attr_name = f"{zone_name} Early Start"
        # Use zone_name for unique_id to maintain entity_id stability
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_early_start"
        self._attr_icon = "mdi:clock-fast"
        self._attr_is_on = initial_state
        self._attr_available = True
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type)
    
    @property
    def icon(self):
        return "mdi:clock-fast" if self._attr_is_on else "mdi:clock-outline"
    
    @property
    def extra_state_attributes(self):
        return {
            "zone_id": self._zone_id,
            "zone": self._zone_name,
            "description": "Pre-heats the room to reach target temperature on time",
        }
    
    def update(self):
        """Update early start state from API."""
        # Early start state is not in the cached files, so we keep the last known state
        # It will be updated when user toggles it
        pass
    
    async def async_turn_on(self, **kwargs):
        """Turn on early start - async."""
        success = await self._async_set_early_start(True)
        if success:
            await self._async_trigger_immediate_refresh("early_start_on")
    
    async def async_turn_off(self, **kwargs):
        """Turn off early start - async."""
        success = await self._async_set_early_start(False)
        if success:
            await self._async_trigger_immediate_refresh("early_start_off")
    
    async def _async_trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, reason)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")
    
    async def _async_set_early_start(self, enabled: bool) -> bool:
        """Set early start state via async API."""
        from .async_api import get_async_client
        
        client = get_async_client(self.hass)
        
        # Early start uses a different endpoint format
        endpoint = f"zones/{self._zone_id}/earlyStart"
        result = await client.api_call(endpoint, method="PUT", data={"enabled": enabled})
        
        if result is not None:
            state_str = "enabled" if enabled else "disabled"
            _LOGGER.info(f"Early Start {state_str} for {self._zone_name}")
            self._attr_is_on = enabled
            self.async_write_ha_state()
            return True
        
        _LOGGER.error(f"Failed to set early start for {self._zone_name}")
        return False


class TadoChildLockSwitch(SwitchEntity):
    """Tado CE Child Lock Switch Entity."""
    
    def __init__(self, zone_id: str, serial: str, zone_name: str, zone_type: str, device_type: str, initial_state: bool, zones_info: list):
        self._zone_id = zone_id
        self._serial = serial
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._device_type = device_type
        
        # Import here to avoid circular dependency
        from .device_manager import get_device_name_suffix
        suffix = get_device_name_suffix(zone_id, serial, device_type, zones_info)
        
        self._attr_name = f"{zone_name}{suffix} Child Lock"
        self._attr_unique_id = f"tado_ce_{serial}_child_lock"
        self._attr_icon = "mdi:lock"
        self._attr_is_on = initial_state
        self._attr_available = True
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type)
    
    @property
    def icon(self):
        return "mdi:lock" if self._attr_is_on else "mdi:lock-open"
    
    @property
    def extra_state_attributes(self):
        return {
            "serial": self._serial,
            "device_type": self._device_type,
            "zone": self._zone_name,
        }
    
    def update(self):
        """Update child lock state from JSON file."""
        try:
            with open(ZONES_INFO_FILE) as f:
                zones_info = json.load(f)
                
                for zone in zones_info:
                    for device in zone.get('devices', []):
                        if device.get('shortSerialNo') == self._serial:
                            if 'childLockEnabled' in device:
                                self._attr_is_on = device.get('childLockEnabled', False)
                                self._attr_available = True
                                return
                
            self._attr_available = False
        except Exception:
            self._attr_available = False
    
    async def async_turn_on(self, **kwargs):
        """Turn on child lock - async."""
        success = await self._async_set_child_lock(True)
        if success:
            await self._async_trigger_immediate_refresh("child_lock_on")
    
    async def async_turn_off(self, **kwargs):
        """Turn off child lock - async."""
        success = await self._async_set_child_lock(False)
        if success:
            await self._async_trigger_immediate_refresh("child_lock_off")
    
    async def _async_trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, reason)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")
    
    async def _async_set_child_lock(self, enabled: bool) -> bool:
        """Set child lock state via async API."""
        from .async_api import get_async_client
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        import aiohttp
        
        client = get_async_client(self.hass)
        token = await client.get_access_token()
        
        if not token:
            _LOGGER.error("Failed to get access token")
            return False
        
        # Child lock uses device endpoint (not home endpoint)
        url = f"{API_ENDPOINT_DEVICES}/{self._serial}/childLock"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        session = async_get_clientsession(self.hass)
        
        try:
            async with session.put(
                url, headers=headers, json={"childLockEnabled": enabled}
            ) as resp:
                if resp.status in (200, 204):
                    state_str = "enabled" if enabled else "disabled"
                    _LOGGER.info(f"Child lock {state_str} for {self._zone_name} ({self._serial})")
                    self._attr_is_on = enabled
                    self.async_write_ha_state()
                    return True
                
                _LOGGER.error(f"Failed to set child lock: {resp.status}")
                return False
                
        except aiohttp.ClientError as e:
            _LOGGER.error(f"Network error while setting child lock: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while setting child lock: {e}")
            return False
