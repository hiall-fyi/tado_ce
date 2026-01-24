"""Tado CE Climate Platform - Supports Heating and AC zones."""
import json
import logging
from datetime import timedelta

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
    DOMAIN, ZONES_FILE, ZONES_INFO_FILE, CONFIG_FILE, HOME_STATE_FILE,
    DEFAULT_ZONE_NAMES
)
from .device_manager import get_zone_device_info
from .async_api import get_async_client
from .data_loader import (
    load_zones_file, load_zones_info_file, load_config_file,
    load_home_state_file, load_offsets_file, load_ac_capabilities_file,
    get_zone_names as dl_get_zone_names, get_zone_types as dl_get_zone_types
)

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

# Fan level mapping - Tado uses SILENT, LEVEL1-5, AUTO
# Map to HA's limited fan modes (auto, low, medium, high)
TADO_TO_HA_FAN = {
    "AUTO": FAN_AUTO,
    "SILENT": FAN_LOW,
    "LEVEL1": FAN_LOW,
    "LEVEL2": FAN_LOW,
    "LEVEL3": FAN_MEDIUM,
    "LEVEL4": FAN_HIGH,
    "LEVEL5": FAN_HIGH,
    # Legacy mappings
    "HIGH": FAN_HIGH,
    "MIDDLE": FAN_MEDIUM,
    "LOW": FAN_LOW,
}

HA_TO_TADO_FAN = {
    FAN_AUTO: "AUTO",
    FAN_LOW: "LEVEL1",
    FAN_MEDIUM: "LEVEL3",
    FAN_HIGH: "LEVEL5",
}

# Swing mapping - Tado has multiple positions, HA only has ON/OFF
TADO_TO_HA_SWING = {
    "ON": SWING_ON,
    "OFF": SWING_OFF,
    # Any specific position counts as ON
    "UP": SWING_ON,
    "MID_UP": SWING_ON,
    "MID": SWING_ON,
    "MID_DOWN": SWING_ON,
    "DOWN": SWING_ON,
    "LEFT": SWING_ON,
    "MID_LEFT": SWING_ON,
    "MID_RIGHT": SWING_ON,
    "RIGHT": SWING_ON,
}


def get_zone_names():
    """Load zone names from API data."""
    return dl_get_zone_names()


def get_zone_types():
    """Load zone types from API data."""
    return dl_get_zone_types()


def get_zone_capabilities():
    """Load zone capabilities (for AC zones).
    
    First tries to load from ac_capabilities.json (fetched from dedicated API endpoint).
    Falls back to zones_info.json for basic capabilities.
    """
    ac_caps = load_ac_capabilities_file() or {}
    zones_info = load_zones_info_file()
    
    if not zones_info:
        return {}
    
    caps = {}
    for z in zones_info:
        zone_id = str(z.get('id'))
        zone_type = z.get('type')
        
        if zone_type == 'AIR_CONDITIONING' and zone_id in ac_caps:
            # Use detailed AC capabilities from dedicated API
            caps[zone_id] = {
                'type': zone_type,
                'ac_capabilities': ac_caps[zone_id],
            }
        else:
            # Fallback to basic capabilities from zones_info
            # Use 'or {}' pattern for null safety
            caps[zone_id] = {
                'type': zone_type,
                'capabilities': z.get('capabilities') or {},
            }
    return caps


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE climate from a config entry."""
    zone_names = await hass.async_add_executor_job(get_zone_names)
    zone_types = await hass.async_add_executor_job(get_zone_types)
    zone_caps = await hass.async_add_executor_job(get_zone_capabilities)
    
    climates = []
    try:
        zones_data = await hass.async_add_executor_job(load_zones_file)
        if zones_data:
            # Use 'or {}' pattern for null safety
            zone_states = zones_data.get('zoneStates') or {}
            for zone_id, zone_data in zone_states.items():
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
        self._offset_celsius = None  # Temperature offset (optional, enabled in config)
        self._attr_preset_mode = PRESET_HOME

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {
            "overlay_type": self._overlay_type,
            "heating_power": self._heating_power,
            "zone_id": self._zone_id,
        }
        # Only include offset_celsius if enabled and available
        if self._offset_celsius is not None:
            attrs["offset_celsius"] = self._offset_celsius
        return attrs

    def update(self):
        """Update climate state from JSON file."""
        try:
            # Load home_id from config
            with open(CONFIG_FILE) as f:
                config = json.load(f)
                self._home_id = config.get("home_id")
            
            with open(ZONES_FILE) as f:
                data = json.load(f)
                # Use 'or {}' pattern for null safety
                zone_states = data.get('zoneStates') or {}
                zone_data = zone_states.get(self._zone_id)
                
                if not zone_data:
                    self._attr_available = False
                    return
                
                # Current temperature (use 'or {}' pattern for null safety)
                sensor_data = zone_data.get('sensorDataPoints') or {}
                self._attr_current_temperature = (
                    (sensor_data.get('insideTemperature') or {}).get('celsius')
                )
                
                # Current humidity
                self._attr_current_humidity = (
                    (sensor_data.get('humidity') or {}).get('percentage')
                )
                
                # Heating power
                activity_data = zone_data.get('activityDataPoints') or {}
                self._heating_power = (
                    (activity_data.get('heatingPower') or {}).get('percentage', 0)
                )
                
                # HVAC action based on heating power
                if self._heating_power and self._heating_power > 0:
                    self._attr_hvac_action = HVACAction.HEATING
                else:
                    self._attr_hvac_action = HVACAction.IDLE
                
                # Setting (target temp and mode)
                setting = zone_data.get('setting') or {}
                power = setting.get('power')
                self._overlay_type = zone_data.get('overlayType')
                
                if power == 'ON':
                    temp = (setting.get('temperature') or {}).get('celsius')
                    self._attr_target_temperature = temp
                    
                    # Determine HVAC mode - match official Tado integration behavior
                    # Show AUTO when following schedule (even if scheduled OFF)
                    # Show HEAT only when there's a MANUAL overlay
                    if self._overlay_type == 'MANUAL':
                        self._attr_hvac_mode = HVACMode.HEAT
                    else:
                        self._attr_hvac_mode = HVACMode.AUTO
                else:
                    # Power is OFF
                    # Match official Tado integration: show AUTO if following schedule
                    # This helps distinguish "OFF by schedule" vs "OFF by manual override"
                    if self._overlay_type == 'MANUAL':
                        self._attr_hvac_mode = HVACMode.OFF
                    else:
                        # Following schedule (even if scheduled to be OFF)
                        self._attr_hvac_mode = HVACMode.AUTO
                    self._attr_target_temperature = None
                    self._attr_hvac_action = HVACAction.OFF
                
                self._attr_available = True
            
            # Update preset mode from home state
            self._update_preset_mode()
            
            # Update offset if enabled
            self._update_offset()
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update {self.name}: {e}")
            self._attr_available = False
    
    def _update_offset(self):
        """Update temperature offset from cached offsets file.
        
        Offset is synced during full sync if offset_enabled is True in config.
        Only reads offset if offset_enabled is True in config.
        """
        try:
            # Check if offset is enabled in config
            config_manager = self.hass.data.get(DOMAIN, {}).get('config_manager')
            if not config_manager or not config_manager.get_offset_enabled():
                self._offset_celsius = None
                return
            
            from .const import OFFSETS_FILE
            if OFFSETS_FILE.exists():
                with open(OFFSETS_FILE) as f:
                    offsets = json.load(f)
                    self._offset_celsius = offsets.get(self._zone_id)
            else:
                self._offset_celsius = None
        except Exception:
            # Keep existing offset value on error
            pass
    
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

    async def async_set_preset_mode(self, preset_mode: str):
        """Set preset mode (Home/Away).
        
        Uses 1 API call to set presence lock.
        """
        client = get_async_client(self.hass)
        state = "AWAY" if preset_mode == PRESET_AWAY else "HOME"
        
        if await client.set_presence_lock(state):
            self._attr_preset_mode = preset_mode
            await self._async_trigger_immediate_refresh("preset_mode_change")

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        
        client = get_async_client(self.hass)
        setting = {
            "type": "HEATING",
            "power": "ON",
            "temperature": {"celsius": temperature}
        }
        termination = {"type": "MANUAL"}
        
        if await client.set_zone_overlay(self._zone_id, setting, termination):
            self._attr_target_temperature = temperature
            self._attr_hvac_mode = HVACMode.HEAT
            _LOGGER.info(f"Set {self._zone_name} to {temperature}°C")
            await self._async_trigger_immediate_refresh("temperature_change")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode):
        """Set new HVAC mode."""
        client = get_async_client(self.hass)
        
        if hvac_mode == HVACMode.HEAT:
            temp = self._attr_target_temperature or 20
            setting = {
                "type": "HEATING",
                "power": "ON",
                "temperature": {"celsius": temp}
            }
            termination = {"type": "MANUAL"}
            
            if await client.set_zone_overlay(self._zone_id, setting, termination):
                self._attr_hvac_mode = HVACMode.HEAT
                await self._async_trigger_immediate_refresh("hvac_mode_change")
                
        elif hvac_mode == HVACMode.OFF:
            setting = {
                "type": "HEATING",
                "power": "OFF"
            }
            termination = {"type": "MANUAL"}
            
            if await client.set_zone_overlay(self._zone_id, setting, termination):
                self._attr_hvac_mode = HVACMode.OFF
                await self._async_trigger_immediate_refresh("hvac_mode_change")
                
        elif hvac_mode == HVACMode.AUTO:
            if await client.delete_zone_overlay(self._zone_id):
                self._attr_hvac_mode = HVACMode.AUTO
                await self._async_trigger_immediate_refresh("hvac_mode_change")
    
    async def _async_trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, reason)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")

    async def async_set_timer(self, temperature: float, duration_minutes: int = None, overlay: str = None) -> bool:
        """Set temperature with timer or overlay type.
        
        Args:
            temperature: Target temperature in Celsius
            duration_minutes: Duration in minutes (for TIMER termination)
            overlay: Overlay type - 'next_time_block' for TADO_MODE, None for MANUAL
        """
        client = get_async_client(self.hass)
        
        setting = {
            "type": "HEATING",
            "power": "ON",
            "temperature": {"celsius": temperature}
        }
        
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
        
        if await client.set_zone_overlay(self._zone_id, setting, termination):
            _LOGGER.info(f"Set {self._zone_name} to {temperature}°C {term_desc}")
            return True
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
        
        # Get AC capabilities from dedicated API endpoint
        # Format: {"COOL": {...}, "HEAT": {...}, "DRY": {...}, "FAN": {...}, "AUTO": {...}}
        # Use 'or {}' pattern for null safety
        ac_caps = capabilities.get('ac_capabilities') or {}
        
        # Build supported features based on capabilities
        features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        
        # Check if any mode has fan levels
        has_fan = any((ac_caps.get(mode) or {}).get('fanLevel') for mode in ['COOL', 'HEAT', 'DRY', 'FAN', 'AUTO'])
        if has_fan:
            features |= ClimateEntityFeature.FAN_MODE
        
        # Check if any mode has swing options
        has_swing = any(
            (ac_caps.get(mode) or {}).get('verticalSwing') or (ac_caps.get(mode) or {}).get('horizontalSwing')
            for mode in ['COOL', 'HEAT', 'DRY', 'FAN', 'AUTO']
        )
        if has_swing:
            features |= ClimateEntityFeature.SWING_MODE
        
        self._attr_supported_features = features
        
        # Build HVAC modes based on capabilities
        # Always include OFF and AUTO (schedule)
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
        
        # Add modes that exist in capabilities
        for tado_mode in ['COOL', 'HEAT', 'DRY', 'FAN']:
            if tado_mode in ac_caps:
                ha_mode = TADO_TO_HA_HVAC_MODE.get(tado_mode)
                if ha_mode and ha_mode not in self._attr_hvac_modes:
                    self._attr_hvac_modes.append(ha_mode)
        
        # If AUTO mode exists in capabilities, it maps to HEAT_COOL
        if 'AUTO' in ac_caps:
            # AUTO is already added above, but ensure HEAT_COOL is there
            if HVACMode.HEAT_COOL not in self._attr_hvac_modes:
                self._attr_hvac_modes.append(HVACMode.HEAT_COOL)
        
        _LOGGER.debug(f"AC zone {zone_id} HVAC modes: {self._attr_hvac_modes}")
        
        # Fan modes - collect from all modes that have fanLevel
        fan_levels = set()
        for mode_caps in ac_caps.values():
            if isinstance(mode_caps, dict) and 'fanLevel' in mode_caps:
                fan_levels.update(mode_caps['fanLevel'])
        
        if fan_levels:
            # Map Tado fan levels to HA fan modes
            self._attr_fan_modes = []
            if 'AUTO' in fan_levels:
                self._attr_fan_modes.append(FAN_AUTO)
            if any(f in fan_levels for f in ['SILENT', 'LEVEL1', 'LEVEL2', 'LOW']):
                self._attr_fan_modes.append(FAN_LOW)
            if any(f in fan_levels for f in ['LEVEL3', 'MIDDLE']):
                self._attr_fan_modes.append(FAN_MEDIUM)
            if any(f in fan_levels for f in ['LEVEL4', 'LEVEL5', 'HIGH']):
                self._attr_fan_modes.append(FAN_HIGH)
            _LOGGER.debug(f"AC zone {zone_id} fan modes: {self._attr_fan_modes} (from {fan_levels})")
        else:
            self._attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]
        
        # Swing modes - unified dropdown like official Tado integration
        # Options: off, vertical, horizontal, both
        if has_swing:
            self._attr_swing_modes = ["off", "vertical", "horizontal", "both"]
        else:
            self._attr_swing_modes = None
        
        # Temperature range from capabilities
        # Get from any mode that has temperatures (COOL is most common)
        temp_caps = None
        for mode in ['COOL', 'HEAT', 'AUTO', 'DRY']:
            if mode in ac_caps and 'temperatures' in ac_caps[mode]:
                # Use 'or {}' pattern for null safety
                temp_caps = (ac_caps[mode]['temperatures'].get('celsius') or {})
                break
        
        if temp_caps:
            self._attr_min_temp = temp_caps.get('min', 16)
            self._attr_max_temp = temp_caps.get('max', 30)
            self._attr_target_temperature_step = temp_caps.get('step', 1)
        else:
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
                # Use 'or {}' pattern for null safety
                zone_states = data.get('zoneStates') or {}
                zone_data = zone_states.get(self._zone_id)
                
                if not zone_data:
                    self._attr_available = False
                    return
                
                # Current temperature (use 'or {}' pattern for null safety)
                sensor_data = zone_data.get('sensorDataPoints') or {}
                self._attr_current_temperature = (
                    (sensor_data.get('insideTemperature') or {}).get('celsius')
                )
                
                # Current humidity
                self._attr_current_humidity = (
                    (sensor_data.get('humidity') or {}).get('percentage')
                )
                
                # AC power state - API returns {'value': 'ON'/'OFF'} not percentage
                activity_data = zone_data.get('activityDataPoints') or {}
                ac_power = activity_data.get('acPower') or {}
                ac_power_value = ac_power.get('value')  # 'ON' or 'OFF'
                # Keep percentage for backwards compatibility attribute
                self._ac_power_percentage = ac_power.get('percentage')
                
                # Setting
                setting = zone_data.get('setting') or {}
                power = setting.get('power')
                self._overlay_type = zone_data.get('overlayType')
                
                if power == 'ON':
                    # Temperature
                    temp = (setting.get('temperature') or {}).get('celsius')
                    self._attr_target_temperature = temp
                    
                    # Mode
                    tado_mode = setting.get('mode')
                    self._attr_hvac_mode = TADO_TO_HA_HVAC_MODE.get(tado_mode, HVACMode.AUTO)
                    
                    # Fan - API returns fanLevel (newer firmware) or fanSpeed (older firmware)
                    fan_level = setting.get('fanLevel') or setting.get('fanSpeed')
                    self._attr_fan_mode = TADO_TO_HA_FAN.get(fan_level, FAN_AUTO)
                    
                    # Swing - API returns verticalSwing/horizontalSwing (not swing)
                    # Map to unified swing mode: off/vertical/horizontal/both
                    vertical_swing = setting.get('verticalSwing', 'OFF')
                    horizontal_swing = setting.get('horizontalSwing', 'OFF')
                    v_on = vertical_swing != 'OFF'
                    h_on = horizontal_swing != 'OFF'
                    if v_on and h_on:
                        self._attr_swing_mode = "both"
                    elif v_on:
                        self._attr_swing_mode = "vertical"
                    elif h_on:
                        self._attr_swing_mode = "horizontal"
                    else:
                        self._attr_swing_mode = "off"
                    
                    # HVAC action - based on acPower.value ('ON'/'OFF')
                    if ac_power_value == 'ON':
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
                    # Power is OFF - keep last temperature for reference
                    # Don't set self._attr_target_temperature = None
                    self._attr_hvac_mode = HVACMode.OFF
                    self._attr_hvac_action = HVACAction.OFF
                
                self._attr_available = True
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update {self.name}: {e}")
            self._attr_available = False

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        
        if await self._async_set_ac_overlay(temperature=temperature):
            self._attr_target_temperature = temperature
            await self._async_trigger_immediate_refresh("temperature_change")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode):
        """Set new HVAC mode."""
        client = get_async_client(self.hass)
        
        if hvac_mode == HVACMode.OFF:
            setting = {
                "type": "AIR_CONDITIONING",
                "power": "OFF"
            }
            termination = {"type": "MANUAL"}
            
            if await client.set_zone_overlay(self._zone_id, setting, termination):
                self._attr_hvac_mode = HVACMode.OFF
                await self._async_trigger_immediate_refresh("hvac_mode_change")
                
        elif hvac_mode == HVACMode.AUTO:
            if await client.delete_zone_overlay(self._zone_id):
                self._attr_hvac_mode = HVACMode.AUTO
                await self._async_trigger_immediate_refresh("hvac_mode_change")
        else:
            tado_mode = HA_TO_TADO_HVAC_MODE.get(hvac_mode, 'COOL')
            if await self._async_set_ac_overlay(mode=tado_mode):
                self._attr_hvac_mode = hvac_mode
                await self._async_trigger_immediate_refresh("hvac_mode_change")

    async def async_set_fan_mode(self, fan_mode: str):
        """Set new fan mode."""
        tado_fan = HA_TO_TADO_FAN.get(fan_mode, 'AUTO')
        if await self._async_set_ac_overlay(fan_level=tado_fan):
            self._attr_fan_mode = fan_mode
            await self._async_trigger_immediate_refresh("fan_mode_change")

    async def async_set_swing_mode(self, swing_mode: str):
        """Set new swing mode.
        
        Unified swing dropdown like official Tado integration:
        - off: verticalSwing=OFF, horizontalSwing=OFF
        - vertical: verticalSwing=ON, horizontalSwing=OFF
        - horizontal: verticalSwing=OFF, horizontalSwing=ON
        - both: verticalSwing=ON, horizontalSwing=ON
        """
        if swing_mode == "off":
            v_swing, h_swing = "OFF", "OFF"
        elif swing_mode == "vertical":
            v_swing, h_swing = "ON", "OFF"
        elif swing_mode == "horizontal":
            v_swing, h_swing = "OFF", "ON"
        elif swing_mode == "both":
            v_swing, h_swing = "ON", "ON"
        else:
            # Fallback for legacy SWING_ON/SWING_OFF
            v_swing = "ON" if swing_mode == SWING_ON else "OFF"
            h_swing = "OFF"
        
        if await self._async_set_ac_overlay(vertical_swing=v_swing, horizontal_swing=h_swing):
            self._attr_swing_mode = swing_mode
            await self._async_trigger_immediate_refresh("swing_mode_change")
    
    async def _async_trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, reason)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")

    async def _async_set_ac_overlay(self, temperature: float = None, mode: str = None, 
                                    fan_level: str = None, vertical_swing: str = None,
                                    horizontal_swing: str = None,
                                    duration_minutes: int = None) -> bool:
        """Set AC overlay with optional parameters.
        
        Uses Tado API v2 format with fanLevel, verticalSwing, horizontalSwing.
        Only sends fields that are supported by the current mode (per capabilities).
        """
        client = get_async_client(self.hass)
        
        # Build setting from current state + changes
        setting = {
            "type": "AIR_CONDITIONING",
            "power": "ON",
        }
        
        # Mode
        if mode:
            setting["mode"] = mode
        elif self._attr_hvac_mode and self._attr_hvac_mode not in (HVACMode.OFF, HVACMode.AUTO):
            setting["mode"] = HA_TO_TADO_HVAC_MODE.get(self._attr_hvac_mode, 'COOL')
        else:
            setting["mode"] = "COOL"
        
        current_mode = setting["mode"]
        
        # Get capabilities for current mode to check what fields are supported
        ac_caps = self._capabilities.get('ac_capabilities') or {}
        mode_caps = ac_caps.get(current_mode) or {}
        
        # Temperature (FAN mode doesn't need it)
        if current_mode != "FAN":
            if temperature:
                setting["temperature"] = {"celsius": temperature}
            elif self._attr_target_temperature:
                setting["temperature"] = {"celsius": self._attr_target_temperature}
            else:
                setting["temperature"] = {"celsius": 24}
        
        # Fan level - only send if mode supports it (DRY mode doesn't have fanLevel)
        if 'fanLevel' in mode_caps:
            if fan_level:
                setting["fanLevel"] = fan_level
            elif self._attr_fan_mode:
                setting["fanLevel"] = HA_TO_TADO_FAN.get(self._attr_fan_mode, 'AUTO')
            else:
                setting["fanLevel"] = "AUTO"
        
        # Swing - only send if mode supports it
        # Use unified swing mode: off/vertical/horizontal/both
        if 'verticalSwing' in mode_caps:
            if vertical_swing is not None:
                setting["verticalSwing"] = vertical_swing
            elif self._attr_swing_mode in ("vertical", "both"):
                setting["verticalSwing"] = "ON"
            else:
                setting["verticalSwing"] = "OFF"
        
        if 'horizontalSwing' in mode_caps:
            if horizontal_swing is not None:
                setting["horizontalSwing"] = horizontal_swing
            elif self._attr_swing_mode in ("horizontal", "both"):
                setting["horizontalSwing"] = "ON"
            else:
                setting["horizontalSwing"] = "OFF"
        
        # Termination
        if duration_minutes:
            termination = {"type": "TIMER", "durationInSeconds": duration_minutes * 60}
        else:
            termination = {"type": "MANUAL"}
        
        _LOGGER.debug(f"AC overlay payload: setting={setting}, termination={termination}")
        
        if await client.set_zone_overlay(self._zone_id, setting, termination):
            _LOGGER.info(f"Set AC {self._zone_name}: {setting}")
            return True
        return False

    async def async_set_timer(self, temperature: float, duration_minutes: int, mode: str = None) -> bool:
        """Set AC with timer."""
        return await self._async_set_ac_overlay(
            temperature=temperature,
            mode=mode,
            duration_minutes=duration_minutes
        )
