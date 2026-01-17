"""Tado CE Switch Platform (Child Lock + Early Start)."""
import json
import logging
from datetime import timedelta
from urllib.request import Request, urlopen

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant

from .const import (
    ZONES_INFO_FILE, CONFIG_FILE,
    TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


def _load_zones_info_file():
    """Load zones info file (blocking)."""
    try:
        with open(ZONES_INFO_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _get_access_token():
    """Get access token by refreshing from config."""
    from urllib.parse import urlencode
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        
        refresh_token = config.get("refresh_token")
        if not refresh_token:
            return None
        
        data = urlencode({
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }).encode()
        
        req = Request(f"{TADO_AUTH_URL}/token", data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        
        with urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode())
            
            # Save new refresh token
            new_refresh = token_data.get("refresh_token")
            if new_refresh and new_refresh != refresh_token:
                config["refresh_token"] = new_refresh
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(config, f, indent=2)
            
            return token_data.get("access_token")
    except Exception as e:
        _LOGGER.error(f"Failed to get access token: {e}")
        return None


def _get_home_id():
    """Get home ID from config."""
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
            return config.get("home_id")
    except Exception:
        return None


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE switches from a config entry."""
    _LOGGER.warning("Tado CE switch: Setting up...")
    zones_info = await hass.async_add_executor_job(_load_zones_info_file)
    
    switches = []
    
    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone.get('id')}")
            zone_type = zone.get('type')
            
            # Early Start switch (for heating zones that support it)
            if zone_type == 'HEATING':
                early_start = zone.get('earlyStart', {})
                if early_start.get('supported', True):  # Default to supported
                    switches.append(TadoEarlyStartSwitch(
                        zone_id, zone_name, early_start.get('enabled', False)
                    ))
            
            # Child Lock switches (per device)
            for device in zone.get('devices', []):
                if 'childLockEnabled' in device:
                    serial = device.get('shortSerialNo')
                    device_type = device.get('deviceType', 'unknown')
                    switches.append(TadoChildLockSwitch(
                        serial, zone_name, device_type, device.get('childLockEnabled', False)
                    ))
    
    if switches:
        async_add_entities(switches, True)
        _LOGGER.warning(f"Tado CE switches loaded: {len(switches)}")
    else:
        _LOGGER.warning("Tado CE: No switches found")


class TadoEarlyStartSwitch(SwitchEntity):
    """Tado CE Early Start Switch Entity."""
    
    def __init__(self, zone_id: str, zone_name: str, initial_state: bool):
        self._zone_id = zone_id
        self._zone_name = zone_name
        
        self._attr_name = f"Tado CE {zone_name} Early Start"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_early_start"
        self._attr_icon = "mdi:clock-fast"
        self._attr_is_on = initial_state
        self._attr_available = True
    
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
    
    def turn_on(self, **kwargs):
        """Turn on early start."""
        self._set_early_start(True)
    
    def turn_off(self, **kwargs):
        """Turn off early start."""
        self._set_early_start(False)
    
    def _set_early_start(self, enabled: bool) -> bool:
        """Set early start state via API."""
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            home_id = _get_home_id()
            if not home_id:
                _LOGGER.error("No home_id configured")
                return False
            
            url = f"{TADO_API_BASE}/homes/{home_id}/zones/{self._zone_id}/earlyStart"
            payload = {"enabled": enabled}
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                state_str = "enabled" if enabled else "disabled"
                _LOGGER.info(f"Early Start {state_str} for {self._zone_name}")
                self._attr_is_on = enabled
                return True
                
        except Exception as e:
            _LOGGER.error(f"Failed to set early start: {e}")
            return False


class TadoChildLockSwitch(SwitchEntity):
    """Tado CE Child Lock Switch Entity."""
    
    def __init__(self, serial: str, zone_name: str, device_type: str, initial_state: bool):
        self._serial = serial
        self._zone_name = zone_name
        self._device_type = device_type
        
        self._attr_name = f"Tado CE {zone_name} Child Lock"
        self._attr_unique_id = f"tado_ce_{serial}_child_lock"
        self._attr_icon = "mdi:lock"
        self._attr_is_on = initial_state
        self._attr_available = True
    
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
    
    def turn_on(self, **kwargs):
        """Turn on child lock."""
        self._set_child_lock(True)
    
    def turn_off(self, **kwargs):
        """Turn off child lock."""
        self._set_child_lock(False)
    
    def _set_child_lock(self, enabled: bool) -> bool:
        """Set child lock state via API."""
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            # API endpoint: PUT /devices/{serialNo}/childLock
            url = f"https://my.tado.com/api/v2/devices/{self._serial}/childLock"
            payload = {"childLockEnabled": enabled}
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                state_str = "enabled" if enabled else "disabled"
                _LOGGER.info(f"Child lock {state_str} for {self._zone_name} ({self._serial})")
                self._attr_is_on = enabled
                return True
                
        except Exception as e:
            _LOGGER.error(f"Failed to set child lock: {e}")
            return False
