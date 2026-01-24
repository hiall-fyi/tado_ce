"""Tado CE Button Platform."""
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .device_manager import get_zone_device_info
from .config_manager import ConfigurationManager
from .data_loader import load_zones_info_file

_LOGGER = logging.getLogger(__name__)

# Default timer preset durations (in minutes)
DEFAULT_TIMER_PRESETS = [30, 60, 90]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE buttons from a config entry."""
    _LOGGER.debug("Tado CE button: Setting up...")
    zones_info = await hass.async_add_executor_job(load_zones_info_file)
    
    buttons = []
    
    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone_id}")
            zone_type = zone.get('type')
            
            # Create timer preset buttons for hot water zones
            if zone_type == 'HOT_WATER':
                for duration in DEFAULT_TIMER_PRESETS:
                    buttons.append(
                        TadoWaterHeaterTimerButton(hass, zone_id, zone_name, duration)
                    )
    
    if buttons:
        async_add_entities(buttons, True)
        _LOGGER.info(f"Tado CE buttons loaded: {len(buttons)}")
    else:
        _LOGGER.info("Tado CE: No buttons to create")


class TadoWaterHeaterTimerButton(ButtonEntity):
    """Button to set water heater timer with preset duration."""
    
    def __init__(self, hass: HomeAssistant, zone_id: str, zone_name: str, duration: int):
        """Initialize the button.
        
        Args:
            hass: Home Assistant instance
            zone_id: Zone ID
            zone_name: Zone name
            duration: Timer duration in minutes
        """
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._duration = duration
        
        self._attr_name = f"{zone_name} {duration}min Timer"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_timer_{duration}min"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HOT_WATER")
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_icon = "mdi:timer"
    
    async def async_press(self) -> None:
        """Handle button press - set water heater timer with preset duration."""
        from homeassistant.exceptions import HomeAssistantError
        
        _LOGGER.info(f"Timer button pressed - {self._zone_name} for {self._duration} minutes")
        
        # Construct entity ID
        water_heater_entity_id = f"water_heater.{self._zone_name.lower().replace(' ', '_')}"
        
        # CRITICAL FIX: Verify entity exists before calling service
        if not self.hass.states.get(water_heater_entity_id):
            error_msg = f"Water heater entity not found: {water_heater_entity_id}"
            _LOGGER.error(f"Timer button failed - {error_msg}")
            # Raise error to show in UI
            raise HomeAssistantError(error_msg)
        
        # Convert duration (minutes) to HH:MM:SS format
        hours = self._duration // 60
        minutes = self._duration % 60
        time_period = f"{hours:02d}:{minutes:02d}:00"
        
        _LOGGER.info(f"Calling set_water_heater_timer for {water_heater_entity_id} with {time_period}")
        
        try:
            # Call the set_water_heater_timer service
            await self.hass.services.async_call(
                "tado_ce",
                "set_water_heater_timer",
                {
                    "entity_id": water_heater_entity_id,
                    "time_period": time_period,
                },
                blocking=True,
            )
            
            _LOGGER.info(f"Timer set successfully - {self._zone_name} for {self._duration} minutes")
            
        except HomeAssistantError:
            # Re-raise HomeAssistantError as-is (already has good error message)
            raise
        except Exception as e:
            # Catch any other unexpected errors and provide detailed message
            error_type = type(e).__name__
            error_msg = f"Failed to set {self._duration}min timer for {self._zone_name}: {error_type}: {str(e)}"
            _LOGGER.error(f"Timer button failed - {error_msg}")
            raise HomeAssistantError(error_msg) from e
