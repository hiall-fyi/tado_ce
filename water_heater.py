"""Tado CE Water Heater Platform."""
import json
import logging
from datetime import timedelta
from urllib.request import Request, urlopen

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import STATE_OFF, STATE_ON, UnitOfTemperature
from homeassistant.core import HomeAssistant

from .const import (
    ZONES_FILE, ZONES_INFO_FILE, CONFIG_FILE,
    TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)

OPERATION_MODES = [STATE_ON, STATE_OFF]


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


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE water heater from a config entry."""
    _LOGGER.warning("Tado CE water_heater: Setting up...")
    zones_info = await hass.async_add_executor_job(_load_zones_info_file)
    
    water_heaters = []
    
    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone_id}")
            zone_type = zone.get('type')
            
            if zone_type == 'HOT_WATER':
                water_heaters.append(TadoWaterHeater(hass, zone_id, zone_name))
    
    if water_heaters:
        async_add_entities(water_heaters, True)
        _LOGGER.warning(f"Tado CE water heaters loaded: {len(water_heaters)}")
    else:
        _LOGGER.warning("Tado CE: No hot water zones found")


class TadoWaterHeater(WaterHeaterEntity):
    """Tado CE Water Heater Entity."""
    
    def __init__(self, hass: HomeAssistant, zone_id: str, zone_name: str):
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = None
        
        self._attr_name = f"Tado CE {zone_name}"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_water_heater"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = WaterHeaterEntityFeature.OPERATION_MODE
        self._attr_operation_list = OPERATION_MODES
        self._attr_min_temp = 30
        self._attr_max_temp = 65
        
        self._attr_current_operation = None
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_available = False
        
        self._overlay_type = None

    @property
    def extra_state_attributes(self):
        return {
            "overlay_type": self._overlay_type,
            "zone_id": self._zone_id,
        }

    def update(self):
        """Update water heater state from JSON file."""
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
                self._home_id = config.get("home_id")
            
            with open(ZONES_FILE) as f:
                data = json.load(f)
                zone_data = data.get('zoneStates', {}).get(self._zone_id)
                
                if not zone_data:
                    self._attr_available = False
                    return
                
                setting = zone_data.get('setting', {})
                power = setting.get('power')
                self._overlay_type = zone_data.get('overlayType')
                
                if power == 'ON':
                    self._attr_current_operation = STATE_ON
                else:
                    self._attr_current_operation = STATE_OFF
                
                self._attr_available = True
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update {self.name}: {e}")
            self._attr_available = False

    def set_operation_mode(self, operation_mode: str):
        """Set new operation mode."""
        if operation_mode == STATE_ON:
            self._turn_on()
        else:
            self._turn_off()

    def _turn_on(self) -> bool:
        """Turn on hot water."""
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "HOT_WATER",
                    "power": "ON"
                },
                "termination": {"type": "MANUAL"}
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Turned on {self._zone_name}")
                self._attr_current_operation = STATE_ON
                return True
                
        except Exception as e:
            _LOGGER.error(f"Failed to turn on hot water: {e}")
            return False

    def _turn_off(self) -> bool:
        """Turn off hot water."""
        if not self._home_id:
            return False
        
        try:
            token = _get_access_token()
            if not token:
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "HOT_WATER",
                    "power": "OFF"
                },
                "termination": {"type": "MANUAL"}
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Turned off {self._zone_name}")
                self._attr_current_operation = STATE_OFF
                return True
                
        except Exception as e:
            _LOGGER.error(f"Failed to turn off hot water: {e}")
            return False

    def set_timer(self, duration_minutes: int) -> bool:
        """Turn on hot water with timer (duration in minutes)."""
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "HOT_WATER",
                    "power": "ON"
                },
                "termination": {
                    "type": "TIMER",
                    "durationInSeconds": duration_minutes * 60
                }
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Turned on {self._zone_name} for {duration_minutes} minutes")
                self._attr_current_operation = STATE_ON
                return True
                
        except Exception as e:
            _LOGGER.error(f"Failed to set hot water timer: {e}")
            return False

    def resume_schedule(self) -> bool:
        """Resume hot water schedule (delete overlay)."""
        if not self._home_id:
            return False
        
        try:
            token = _get_access_token()
            if not token:
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            req = Request(url, method="DELETE")
            req.add_header("Authorization", f"Bearer {token}")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Resumed schedule for {self._zone_name}")
                return True
                
        except Exception as e:
            _LOGGER.error(f"Failed to resume schedule: {e}")
            return False
