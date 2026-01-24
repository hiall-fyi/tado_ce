"""Tado CE Water Heater Platform."""
import asyncio
import json
import logging
from datetime import timedelta

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN, ZONES_FILE, ZONES_INFO_FILE, CONFIG_FILE,
    TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID
)
from .device_manager import get_zone_device_info
from .data_loader import load_zones_file, load_zones_info_file, load_config_file

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)

# Operation modes for hot water
STATE_AUTO = "auto"  # Follow schedule (no overlay)
STATE_HEAT = "heat"  # Timer or manual heating
OPERATION_MODES = [STATE_AUTO, STATE_HEAT, STATE_OFF]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE water heater from a config entry."""
    _LOGGER.debug("Tado CE water_heater: Setting up...")
    zones_info = await hass.async_add_executor_job(load_zones_info_file)
    
    water_heaters = []
    
    if zones_info:
        _LOGGER.debug(f"Tado CE water_heater: Found {len(zones_info)} zones")
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone_id}")
            zone_type = zone.get('type')
            
            if zone_type == 'HOT_WATER':
                _LOGGER.debug(f"Tado CE water_heater: Creating entity for zone {zone_id} ({zone_name})")
                water_heaters.append(TadoWaterHeater(hass, zone_id, zone_name))
    
    if water_heaters:
        async_add_entities(water_heaters, True)
        _LOGGER.info(f"Tado CE water heaters loaded: {len(water_heaters)}")
    else:
        _LOGGER.debug("Tado CE: No hot water zones found")


class TadoWaterHeater(WaterHeaterEntity):
    """Tado CE Water Heater Entity."""
    
    def __init__(self, hass: HomeAssistant, zone_id: str, zone_name: str):
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = None
        
        self._attr_name = zone_name
        # Use zone_id for unique_id to maintain entity_id stability across zone name changes
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_water_heater"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_min_temp = 30
        self._attr_max_temp = 65
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HOT_WATER")
        
        self._attr_current_operation = None
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_available = False
        
        # Supported features - will be updated based on zone capabilities
        self._supports_temperature = False
        self._attr_supported_features = WaterHeaterEntityFeature.OPERATION_MODE
        self._attr_operation_list = OPERATION_MODES
        
        self._overlay_type = None

    @property
    def extra_state_attributes(self):
        return {
            "overlay_type": self._overlay_type,
            "zone_id": self._zone_id,
        }

    def update(self):
        """Update water heater state from JSON file."""
        _LOGGER.debug(f"TadoWaterHeater.update() called for {self._zone_name} (zone {self._zone_id})")
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
                    _LOGGER.debug(f"No zone data for {self._zone_name} (zone {self._zone_id})")
                    self._attr_available = False
                    return
                
                # Check link state - if offline, mark unavailable
                link = zone_data.get('link') or {}
                link_state = link.get('state')
                if link_state != 'ONLINE':
                    _LOGGER.debug(f"Zone {self._zone_name} link state: {link_state}")
                    self._attr_available = False
                    return
                
                _LOGGER.debug(f"Zone {self._zone_name} link state OK, setting available=True")
                setting = zone_data.get('setting') or {}
                power = setting.get('power')
                overlay = zone_data.get('overlay')
                self._overlay_type = zone_data.get('overlayType')
                
                # Read target temperature from setting (for systems that support it)
                temp_data = setting.get('temperature') or {}
                self._attr_target_temperature = temp_data.get('celsius')
                
                # Enable temperature feature if zone supports it
                if self._attr_target_temperature is not None and not self._supports_temperature:
                    self._supports_temperature = True
                    self._attr_supported_features = (
                        WaterHeaterEntityFeature.OPERATION_MODE |
                        WaterHeaterEntityFeature.TARGET_TEMPERATURE
                    )
                    _LOGGER.debug(f"Hot water zone {self._zone_name} supports temperature control")
                
                # Detect current operation mode based on overlay state
                if not overlay or self._overlay_type is None:
                    # No overlay = following schedule
                    self._attr_current_operation = STATE_AUTO
                elif self._overlay_type == 'TIMER':
                    # Timer overlay = HEAT mode
                    self._attr_current_operation = STATE_HEAT
                elif self._overlay_type == 'MANUAL':
                    if power == 'OFF':
                        # Manual OFF = OFF mode
                        self._attr_current_operation = STATE_OFF
                    else:
                        # Manual ON = HEAT mode
                        self._attr_current_operation = STATE_HEAT
                else:
                    # Unknown overlay type, default to AUTO
                    _LOGGER.debug(f"Unknown overlay type: {self._overlay_type}, defaulting to AUTO")
                    self._attr_current_operation = STATE_AUTO
                
                self._attr_available = True
                
        except FileNotFoundError as e:
            _LOGGER.warning(f"Data file not found for {self.name}: {e}")
            self._attr_available = False
        except json.JSONDecodeError as e:
            _LOGGER.warning(f"Invalid JSON for {self.name}: {e}")
            self._attr_available = False
        except Exception as e:
            import traceback
            _LOGGER.error(f"Failed to update {self.name}: {e}\n{traceback.format_exc()}")
            self._attr_available = False

    async def async_set_operation_mode(self, operation_mode: str):
        """Set new operation mode with retry logic (async).
        
        Uses TadoAsyncClient for non-blocking API calls.
        """
        from .async_api import get_async_client
        
        # Store previous operation mode for rollback on failure
        previous_mode = self._attr_current_operation
        success = False
        max_retries = 2  # Initial attempt + 1 retry
        
        client = get_async_client(self.hass)
        
        for attempt in range(max_retries):
            if operation_mode == STATE_AUTO:
                # AUTO mode: Delete overlay to follow schedule
                success = await client.delete_zone_overlay(self._zone_id)
                if success:
                    self._attr_current_operation = STATE_AUTO
                    _LOGGER.info(f"Resumed schedule for {self._zone_name}")
                    await self._async_trigger_immediate_refresh("hot_water_auto")
                    break
            elif operation_mode == STATE_HEAT:
                # HEAT mode: Turn on with timer
                duration = self._get_timer_duration()
                success = await self._async_set_timer(duration, None)
                if success:
                    self._attr_current_operation = STATE_HEAT
                    await self._async_trigger_immediate_refresh("hot_water_heat")
                    break
            elif operation_mode == STATE_OFF:
                # OFF mode: Turn off with manual overlay
                success = await self._async_turn_off()
                if success:
                    self._attr_current_operation = STATE_OFF
                    await self._async_trigger_immediate_refresh("hot_water_off")
                    break
            
            # If failed and not last attempt, wait and retry
            if not success and attempt < max_retries - 1:
                _LOGGER.warning(
                    f"Failed to set operation mode to {operation_mode} (attempt {attempt + 1}/{max_retries}), "
                    f"retrying in 5 seconds..."
                )
                await asyncio.sleep(5)
        
        if not success:
            _LOGGER.error(
                f"Failed to set operation mode to {operation_mode} after {max_retries} attempts. "
                f"Keeping previous mode: {previous_mode}"
            )
            # Restore previous operation mode
            self._attr_current_operation = previous_mode
    
    def set_operation_mode(self, operation_mode: str):
        """Set new operation mode (sync wrapper for backward compatibility).
        
        Home Assistant will call async_set_operation_mode() directly.
        This is kept for backward compatibility only.
        """
        # Home Assistant handles async methods automatically
        pass

    
    def _get_timer_duration(self) -> int:
        """Get configured timer duration in minutes (default 60)."""
        try:
            # Try to get from hass.data
            from .const import DOMAIN
            if DOMAIN in self.hass.data and 'config_manager' in self.hass.data[DOMAIN]:
                config_manager = self.hass.data[DOMAIN]['config_manager']
                return config_manager.get_hot_water_timer_duration()
        except Exception as e:
            _LOGGER.debug(f"Failed to get timer duration from config: {e}")
        
        # Default to 60 minutes
        return 60
    
    async def _async_trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change (async)."""
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, reason)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")

    async def _async_turn_on(self) -> bool:
        """Turn on hot water (async)."""
        from .async_api import get_async_client
        
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        client = get_async_client(self.hass)
        
        setting = {"type": "HOT_WATER", "power": "ON"}
        termination = {"type": "MANUAL"}
        
        success = await client.set_zone_overlay(self._zone_id, setting, termination)
        if success:
            _LOGGER.info(f"Turned on {self._zone_name}")
            self._attr_current_operation = STATE_HEAT
        return success

    async def _async_turn_off(self) -> bool:
        """Turn off hot water (async)."""
        from .async_api import get_async_client
        
        if not self._home_id:
            _LOGGER.error("No home_id configured for hot water zone")
            return False
        
        client = get_async_client(self.hass)
        
        setting = {"type": "HOT_WATER", "power": "OFF"}
        termination = {"type": "MANUAL"}
        
        success = await client.set_zone_overlay(self._zone_id, setting, termination)
        if success:
            _LOGGER.info(f"Turned off {self._zone_name}")
            self._attr_current_operation = STATE_OFF
        return success

    async def _async_set_timer(self, duration_minutes: int, temperature: float = None) -> bool:
        """Turn on hot water with timer (async)."""
        from .async_api import get_async_client
        
        if not self._home_id:
            _LOGGER.error("No home_id configured for hot water zone")
            return False
        
        client = get_async_client(self.hass)
        
        # Build setting payload
        setting = {"type": "HOT_WATER", "power": "ON"}
        
        # Add temperature if provided (for solar water heater systems)
        if temperature is not None:
            setting["temperature"] = {"celsius": temperature}
        
        termination = {"type": "TIMER", "durationInSeconds": duration_minutes * 60}
        
        success = await client.set_zone_overlay(self._zone_id, setting, termination)
        if success:
            temp_str = f" at {temperature}°C" if temperature is not None else ""
            _LOGGER.info(f"Turned on {self._zone_name} for {duration_minutes} minutes{temp_str}")
            self._attr_current_operation = STATE_HEAT
        return success

    async def async_set_timer(self, duration_minutes: int, temperature: float = None) -> bool:
        """Public async method to set timer (for service calls)."""
        success = await self._async_set_timer(duration_minutes, temperature)
        if success:
            await self._async_trigger_immediate_refresh("hot_water_timer")
        return success

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature (async).
        
        For hot water systems that support temperature control (e.g., hot water tanks).
        """
        from .async_api import get_async_client
        
        temperature = kwargs.get("temperature")
        if temperature is None:
            _LOGGER.warning("No temperature provided")
            return
        
        if not self._supports_temperature:
            _LOGGER.warning(f"Hot water zone {self._zone_name} does not support temperature control")
            return
        
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return
        
        client = get_async_client(self.hass)
        
        # Set temperature with manual overlay
        setting = {
            "type": "HOT_WATER",
            "power": "ON",
            "temperature": {"celsius": temperature}
        }
        termination = {"type": "MANUAL"}
        
        success = await client.set_zone_overlay(self._zone_id, setting, termination)
        if success:
            self._attr_target_temperature = temperature
            self._attr_current_operation = STATE_HEAT
            _LOGGER.info(f"Set {self._zone_name} temperature to {temperature}°C")
            await self._async_trigger_immediate_refresh("hot_water_temperature")
