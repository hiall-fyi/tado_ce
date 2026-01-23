"""Tado CE Climate Platform - Supports Heating and AC zones."""
import json
import logging
from datetime import timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
    FAN_AUTO,
    FAN_HIGH,
    FAN_MEDIUM,
    FAN_LOW,
    SWING_ON,
    SWING_OFF,
    PRESET_HOME,
    PRESET_AWAY,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN, ZONES_FILE, ZONES_INFO_FILE, CONFIG_FILE, MOBILE_DEVICES_FILE, HOME_STATE_FILE,
    TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID, DEFAULT_ZONE_NAMES
)
from .device_manager import get_hub_device_info, get_zone_device_info
from .auth_manager import get_auth_manager

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


# Tado AC modes mapping
TADO_TO_HA_HVAC_MODE = {
    "COOL": HVACMode.COOL,
    "HEAT": HVACMode.HEAT,
    "DRY": HVACMode.DRY,
    "FAN": HVACMode.FAN_ONLY,
    "AUTO": HVACMode.HEAT_COOL,
}

HA_TO_TADO_HVAC_MODE = {v: k for k, v in TADO_TO_HA_HVAC_MODE.items()}

# Fan speed mapping
TADO_TO_HA_FAN = {
    "AUTO": FAN_AUTO,
    "HIGH": FAN_HIGH,
    "MIDDLE": FAN_MEDIUM,
    "LOW": FAN_LOW,
}

HA_TO_TADO_FAN = {v: k for k, v in TADO_TO_HA_FAN.items()}

# Swing mapping
TADO_TO_HA_SWING = {
    "ON": SWING_ON,
    "OFF": SWING_OFF,
}


def get_zone_names():
    """Load zone names from API data."""
    try:
        with open(ZONES_INFO_FILE) as f:
            zones_info = json.load(f)
            return {str(z.get('id')): z.get('name', f"Zone {z.get('id')}") for z in zones_info}
    except Exception as e:
        _LOGGER.warning(f"Failed to load zone names: {e}")
        return DEFAULT_ZONE_NAMES


def get_zone_types():
    """Load zone types from API data."""
    try:
        with open(ZONES_INFO_FILE) as f:
            zones_info = json.load(f)
            return {str(z.get('id')): z.get('type', 'HEATING') for z in zones_info}
    except Exception:
        return {}


def get_zone_capabilities():
    """Load zone capabilities (for AC zones)."""
    try:
        with open(ZONES_INFO_FILE) as f:
            zones_info = json.load(f)
            caps = {}
            for z in zones_info:
                zone_id = str(z.get('id'))
                # AC capabilities are in zone info
                caps[zone_id] = {
                    'type': z.get('type'),
                    'capabilities': z.get('capabilities', {}),
                }
            return caps
    except Exception:
        return {}


def get_access_token():
    """Get access token using centralized AuthManager.
    
    DEPRECATED: This function is kept for backward compatibility.
    New code should use AuthManager directly.
    """
    auth_manager = get_auth_manager(CONFIG_FILE, CLIENT_ID, TADO_AUTH_URL)
    return auth_manager.get_access_token()


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE climate from a config entry."""
    zone_names = await hass.async_add_executor_job(get_zone_names)
    zone_types = await hass.async_add_executor_job(get_zone_types)
    zone_caps = await hass.async_add_executor_job(get_zone_capabilities)
    
    climates = []
    try:
        zones_data = await hass.async_add_executor_job(_load_zones_file)
        if zones_data:
            for zone_id, zone_data in zones_data.get('zoneStates', {}).items():
                zone_type = zone_types.get(zone_id, 'HEATING')
                zone_name = zone_names.get(zone_id, f"Zone {zone_id}")
                caps = zone_caps.get(zone_id, {})
                
                if zone_type == 'HEATING':
                    climates.append(TadoClimate(hass, zone_id, zone_name))
                elif zone_type == 'AIR_CONDITIONING':
                    climates.append(TadoACClimate(hass, zone_id, zone_name, caps))
    except Exception as e:
        _LOGGER.error(f"Failed to load zones for climate: {e}")
    
    async_add_entities(climates, True)
    _LOGGER.info(f"Tado CE climates loaded: {len(climates)}")


def _load_zones_file():
    """Load zones file (blocking)."""
    try:
        with open(ZONES_FILE) as f:
            return json.load(f)
    except Exception:
        return None


class TadoClimate(ClimateEntity):
    """Tado CE Climate Entity."""
    
    def __init__(self, hass: HomeAssistant, zone_id: str, zone_name: str):
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = None
        
        self._attr_name = zone_name
        # Use zone_id for unique_id to maintain entity_id stability across zone name changes
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_climate"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HEATING")
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE |
            ClimateEntityFeature.TURN_OFF |
            ClimateEntityFeature.TURN_ON |
            ClimateEntityFeature.PRESET_MODE
        )
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
        self._attr_preset_modes = [PRESET_HOME, PRESET_AWAY]
        self._attr_min_temp = 5
        self._attr_max_temp = 25
        self._attr_target_temperature_step = 0.5
        
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_hvac_mode = None
        self._attr_hvac_action = None
        self._attr_available = False
        self._attr_current_humidity = None
        
        # Extra attributes
        self._overlay_type = None
        self._heating_power = None
        self._attr_preset_mode = PRESET_HOME

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        return {
            "overlay_type": self._overlay_type,
            "heating_power": self._heating_power,
            "zone_id": self._zone_id,
        }

    def update(self):
        """Update climate state from JSON file."""
        try:
            # Load home_id from config
            with open(CONFIG_FILE) as f:
                config = json.load(f)
                self._home_id = config.get("home_id")
            
            with open(ZONES_FILE) as f:
                data = json.load(f)
                zone_data = data.get('zoneStates', {}).get(self._zone_id)
                
                if not zone_data:
                    self._attr_available = False
                    return
                
                # Current temperature
                self._attr_current_temperature = (
                    zone_data.get('sensorDataPoints', {})
                    .get('insideTemperature', {})
                    .get('celsius')
                )
                
                # Current humidity
                self._attr_current_humidity = (
                    zone_data.get('sensorDataPoints', {})
                    .get('humidity', {})
                    .get('percentage')
                )
                
                # Heating power
                self._heating_power = (
                    zone_data.get('activityDataPoints', {})
                    .get('heatingPower', {})
                    .get('percentage', 0)
                )
                
                # HVAC action based on heating power
                if self._heating_power and self._heating_power > 0:
                    self._attr_hvac_action = HVACAction.HEATING
                else:
                    self._attr_hvac_action = HVACAction.IDLE
                
                # Setting (target temp and mode)
                setting = zone_data.get('setting', {})
                power = setting.get('power')
                self._overlay_type = zone_data.get('overlayType')
                
                if power == 'ON':
                    temp = setting.get('temperature', {}).get('celsius')
                    self._attr_target_temperature = temp
                    
                    # Determine HVAC mode
                    if self._overlay_type == 'MANUAL':
                        self._attr_hvac_mode = HVACMode.HEAT
                    else:
                        self._attr_hvac_mode = HVACMode.AUTO
                else:
                    self._attr_hvac_mode = HVACMode.OFF
                    self._attr_target_temperature = None
                    self._attr_hvac_action = HVACAction.OFF
                
                self._attr_available = True
            
            # Update preset mode from mobile devices
            self._update_preset_mode()
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update {self.name}: {e}")
            self._attr_available = False
    
    def _update_preset_mode(self):
        """Update preset mode based on home state (not mobile devices).
        
        Uses home_state.json which reflects the actual Tado home/away state,
        regardless of whether mobile device tracking is enabled.
        """
        try:
            with open(HOME_STATE_FILE) as f:
                home_state = json.load(f)
                presence = home_state.get('presence', 'HOME')
                self._attr_preset_mode = PRESET_HOME if presence == 'HOME' else PRESET_AWAY
        except Exception:
            # Keep last known preset mode
            pass

    def set_preset_mode(self, preset_mode: str):
        """Set preset mode (Home/Away).
        
        Uses 1 API call to set presence lock.
        """
        if preset_mode == PRESET_AWAY:
            self._set_presence_lock("AWAY")
        else:
            self._set_presence_lock("HOME")
    
    def _set_presence_lock(self, state: str) -> bool:
        """Set presence lock via API."""
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        try:
            token = get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/presenceLock"
            payload = {"homePresence": state}
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Presence lock set to {state}")
                self._attr_preset_mode = PRESET_AWAY if state == "AWAY" else PRESET_HOME
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while setting presence lock: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while setting presence lock: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while setting presence lock: {e}")
            return False

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        
        if self._set_overlay(temperature):
            self._attr_target_temperature = temperature
            self._attr_hvac_mode = HVACMode.HEAT
            
            # Trigger immediate refresh
            self._trigger_immediate_refresh("temperature_change")

    def set_hvac_mode(self, hvac_mode: HVACMode):
        """Set new HVAC mode."""
        if hvac_mode == HVACMode.HEAT:
            temp = self._attr_target_temperature or 20
            if self._set_overlay(temp):
                self._attr_hvac_mode = HVACMode.HEAT
                self._trigger_immediate_refresh("hvac_mode_change")
        elif hvac_mode == HVACMode.OFF:
            if self._set_overlay_off():
                self._attr_hvac_mode = HVACMode.OFF
                self._trigger_immediate_refresh("hvac_mode_change")
        elif hvac_mode == HVACMode.AUTO:
            if self._delete_overlay():
                self._attr_hvac_mode = HVACMode.AUTO
                self._trigger_immediate_refresh("hvac_mode_change")
    
    def _trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            # Use call_soon_threadsafe to schedule the coroutine from sync context
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(
                    handler.trigger_refresh(self.entity_id, reason)
                )
            )
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")

    def _set_overlay(self, temperature: float) -> bool:
        """Set manual overlay with temperature."""
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        try:
            token = get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "HEATING",
                    "power": "ON",
                    "temperature": {"celsius": temperature}
                },
                "termination": {"type": "MANUAL"}
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Set {self._zone_name} to {temperature}°C")
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while setting temperature: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while setting temperature: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while setting temperature: {e}")
            return False

    def _set_overlay_off(self) -> bool:
        """Set overlay to OFF."""
        if not self._home_id:
            return False
        
        try:
            token = get_access_token()
            if not token:
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "HEATING",
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
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while turning off: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while turning off: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while turning off: {e}")
            return False

    def _delete_overlay(self) -> bool:
        """Delete overlay (return to schedule)."""
        if not self._home_id:
            return False
        
        try:
            token = get_access_token()
            if not token:
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            req = Request(url, method="DELETE")
            req.add_header("Authorization", f"Bearer {token}")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Deleted overlay for {self._zone_name}")
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while deleting overlay: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while deleting overlay: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while deleting overlay: {e}")
            return False

    def set_timer(self, temperature: float, duration_minutes: int = None, overlay: str = None) -> bool:
        """Set temperature with timer or overlay type.
        
        Args:
            temperature: Target temperature in Celsius
            duration_minutes: Duration in minutes (for TIMER termination)
            overlay: Overlay type - 'next_time_block' for TADO_MODE, None for MANUAL
        """
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        try:
            token = get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            
            # Determine termination type
            if duration_minutes:
                termination = {
                    "type": "TIMER",
                    "durationInSeconds": duration_minutes * 60
                }
                term_desc = f"for {duration_minutes} minutes"
            elif overlay == "next_time_block":
                termination = {"type": "TADO_MODE"}
                term_desc = "until next schedule block"
            else:
                termination = {"type": "MANUAL"}
                term_desc = "manually"
            
            payload = {
                "setting": {
                    "type": "HEATING",
                    "power": "ON",
                    "temperature": {"celsius": temperature}
                },
                "termination": termination
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Set {self._zone_name} to {temperature}°C {term_desc}")
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while setting timer: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while setting timer: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while setting timer: {e}")
            return False


class TadoACClimate(ClimateEntity):
    """Tado CE Air Conditioning Climate Entity."""
    
    def __init__(self, hass: HomeAssistant, zone_id: str, zone_name: str, capabilities: dict):
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = None
        self._capabilities = capabilities
        
        self._attr_name = zone_name
        # Use zone_id for unique_id to maintain entity_id stability across zone name changes
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_ac_climate"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "AIR_CONDITIONING")
        
        # Build supported features based on capabilities
        features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        
        # Check AC capabilities
        ac_caps = capabilities.get('capabilities', {})
        if ac_caps.get('FAN') or ac_caps.get('fanLevel'):
            features |= ClimateEntityFeature.FAN_MODE
        if ac_caps.get('SWING') or ac_caps.get('swing'):
            features |= ClimateEntityFeature.SWING_MODE
        
        self._attr_supported_features = features
        
        # Build HVAC modes based on capabilities
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
        for tado_mode, ha_mode in TADO_TO_HA_HVAC_MODE.items():
            if ac_caps.get(tado_mode) or tado_mode in ['COOL', 'HEAT']:
                if ha_mode not in self._attr_hvac_modes:
                    self._attr_hvac_modes.append(ha_mode)
        
        # Fan modes
        self._attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]
        
        # Swing modes
        self._attr_swing_modes = [SWING_ON, SWING_OFF]
        
        self._attr_min_temp = 16
        self._attr_max_temp = 30
        self._attr_target_temperature_step = 1
        
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_hvac_mode = None
        self._attr_hvac_action = None
        self._attr_fan_mode = None
        self._attr_swing_mode = None
        self._attr_available = False
        self._attr_current_humidity = None
        
        self._overlay_type = None
        self._ac_power_percentage = None

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        return {
            "overlay_type": self._overlay_type,
            "ac_power_percentage": self._ac_power_percentage,
            "zone_id": self._zone_id,
            "zone_type": "AIR_CONDITIONING",
        }

    def update(self):
        """Update AC climate state from JSON file."""
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
                
                # Current temperature
                self._attr_current_temperature = (
                    zone_data.get('sensorDataPoints', {})
                    .get('insideTemperature', {})
                    .get('celsius')
                )
                
                # Current humidity
                self._attr_current_humidity = (
                    zone_data.get('sensorDataPoints', {})
                    .get('humidity', {})
                    .get('percentage')
                )
                
                # AC power percentage
                self._ac_power_percentage = (
                    zone_data.get('activityDataPoints', {})
                    .get('acPower', {})
                    .get('percentage')
                )
                
                # Setting
                setting = zone_data.get('setting', {})
                power = setting.get('power')
                self._overlay_type = zone_data.get('overlayType')
                
                if power == 'ON':
                    # Temperature
                    temp = setting.get('temperature', {}).get('celsius')
                    self._attr_target_temperature = temp
                    
                    # Mode
                    tado_mode = setting.get('mode')
                    self._attr_hvac_mode = TADO_TO_HA_HVAC_MODE.get(tado_mode, HVACMode.AUTO)
                    
                    # Fan
                    fan_speed = setting.get('fanSpeed') or setting.get('fanLevel')
                    self._attr_fan_mode = TADO_TO_HA_FAN.get(fan_speed, FAN_AUTO)
                    
                    # Swing
                    swing = setting.get('swing')
                    self._attr_swing_mode = TADO_TO_HA_SWING.get(swing, SWING_OFF)
                    
                    # HVAC action
                    if self._ac_power_percentage and self._ac_power_percentage > 0:
                        if tado_mode == 'COOL':
                            self._attr_hvac_action = HVACAction.COOLING
                        elif tado_mode == 'HEAT':
                            self._attr_hvac_action = HVACAction.HEATING
                        elif tado_mode == 'DRY':
                            self._attr_hvac_action = HVACAction.DRYING
                        elif tado_mode == 'FAN':
                            self._attr_hvac_action = HVACAction.FAN
                        else:
                            self._attr_hvac_action = HVACAction.IDLE
                    else:
                        self._attr_hvac_action = HVACAction.IDLE
                else:
                    self._attr_hvac_mode = HVACMode.OFF
                    self._attr_target_temperature = None
                    self._attr_hvac_action = HVACAction.OFF
                
                self._attr_available = True
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update {self.name}: {e}")
            self._attr_available = False

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        
        if self._set_ac_overlay(temperature=temperature):
            self._attr_target_temperature = temperature
            
            # Trigger immediate refresh
            self._trigger_immediate_refresh("temperature_change")

    def set_hvac_mode(self, hvac_mode: HVACMode):
        """Set new HVAC mode."""
        if hvac_mode == HVACMode.OFF:
            if self._set_ac_overlay_off():
                self._attr_hvac_mode = HVACMode.OFF
                self._trigger_immediate_refresh("hvac_mode_change")
        elif hvac_mode == HVACMode.AUTO:
            if self._delete_overlay():
                self._attr_hvac_mode = HVACMode.AUTO
                self._trigger_immediate_refresh("hvac_mode_change")
        else:
            tado_mode = HA_TO_TADO_HVAC_MODE.get(hvac_mode, 'COOL')
            if self._set_ac_overlay(mode=tado_mode):
                self._attr_hvac_mode = hvac_mode
                self._trigger_immediate_refresh("hvac_mode_change")

    def set_fan_mode(self, fan_mode: str):
        """Set new fan mode."""
        tado_fan = HA_TO_TADO_FAN.get(fan_mode, 'AUTO')
        if self._set_ac_overlay(fan_speed=tado_fan):
            self._attr_fan_mode = fan_mode
            self._trigger_immediate_refresh("fan_mode_change")

    def set_swing_mode(self, swing_mode: str):
        """Set new swing mode."""
        tado_swing = "ON" if swing_mode == SWING_ON else "OFF"
        if self._set_ac_overlay(swing=tado_swing):
            self._attr_swing_mode = swing_mode
            self._trigger_immediate_refresh("swing_mode_change")
    
    def _trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            # Use call_soon_threadsafe to schedule the coroutine from sync context
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(
                    handler.trigger_refresh(self.entity_id, reason)
                )
            )
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")

    def _set_ac_overlay(self, temperature: float = None, mode: str = None, 
                        fan_speed: str = None, swing: str = None,
                        duration_minutes: int = None) -> bool:
        """Set AC overlay with optional parameters."""
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        try:
            token = get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            # Build setting from current state + changes
            setting = {
                "type": "AIR_CONDITIONING",
                "power": "ON",
            }
            
            # Mode
            if mode:
                setting["mode"] = mode
            elif self._attr_hvac_mode and self._attr_hvac_mode != HVACMode.OFF:
                setting["mode"] = HA_TO_TADO_HVAC_MODE.get(self._attr_hvac_mode, 'COOL')
            else:
                setting["mode"] = "COOL"
            
            # Temperature (not all modes need it)
            if setting["mode"] not in ["FAN", "DRY"]:
                if temperature:
                    setting["temperature"] = {"celsius": temperature}
                elif self._attr_target_temperature:
                    setting["temperature"] = {"celsius": self._attr_target_temperature}
                else:
                    setting["temperature"] = {"celsius": 24}
            
            # Fan speed
            if fan_speed:
                setting["fanSpeed"] = fan_speed
            elif self._attr_fan_mode:
                setting["fanSpeed"] = HA_TO_TADO_FAN.get(self._attr_fan_mode, 'AUTO')
            
            # Swing
            if swing:
                setting["swing"] = swing
            elif self._attr_swing_mode:
                setting["swing"] = "ON" if self._attr_swing_mode == SWING_ON else "OFF"
            
            # Termination
            if duration_minutes:
                termination = {"type": "TIMER", "durationInSeconds": duration_minutes * 60}
            else:
                termination = {"type": "MANUAL"}
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {"setting": setting, "termination": termination}
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Set AC {self._zone_name}: {setting}")
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while setting AC: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while setting AC: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while setting AC: {e}")
            return False

    def _set_ac_overlay_off(self) -> bool:
        """Turn off AC."""
        if not self._home_id:
            return False
        
        try:
            token = get_access_token()
            if not token:
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "AIR_CONDITIONING",
                    "power": "OFF"
                },
                "termination": {"type": "MANUAL"}
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Turned off AC {self._zone_name}")
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while turning off AC: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while turning off AC: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while turning off AC: {e}")
            return False

    def _delete_overlay(self) -> bool:
        """Delete overlay (return to schedule)."""
        if not self._home_id:
            return False
        
        try:
            token = get_access_token()
            if not token:
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            req = Request(url, method="DELETE")
            req.add_header("Authorization", f"Bearer {token}")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Deleted overlay for AC {self._zone_name}")
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while deleting AC overlay: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while deleting AC overlay: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while deleting AC overlay: {e}")
            return False

    def set_timer(self, temperature: float, duration_minutes: int, mode: str = None) -> bool:
        """Set AC with timer."""
        return self._set_ac_overlay(
            temperature=temperature,
            mode=mode,
            duration_minutes=duration_minutes
        )
