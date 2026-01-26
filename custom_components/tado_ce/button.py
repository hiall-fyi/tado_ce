"""Tado CE Button Platform."""
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .device_manager import get_zone_device_info, get_hub_device_info
from .config_manager import ConfigurationManager
from .data_loader import load_zones_info_file

_LOGGER = logging.getLogger(__name__)

# Default timer preset durations (in minutes)
DEFAULT_TIMER_PRESETS = [30, 60, 90]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE buttons from a config entry."""
    _LOGGER.debug("Tado CE button: Setting up...")
    zones_info = await hass.async_add_executor_job(load_zones_info_file)
    
    # Get config manager to check if schedule calendar is enabled
    config_manager = hass.data.get(DOMAIN, {}).get('config_manager')
    schedule_calendar_enabled = config_manager.get_schedule_calendar_enabled() if config_manager else False
    
    buttons = []
    
    # Add Resume All Schedules button (hub-level)
    buttons.append(TadoResumeAllSchedulesButton(hass))
    
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
            
            # Create refresh schedule button for heating zones (only if calendar enabled)
            if zone_type == 'HEATING' and schedule_calendar_enabled:
                buttons.append(
                    TadoRefreshScheduleButton(hass, zone_id, zone_name)
                )
    
    if buttons:
        async_add_entities(buttons, True)
        _LOGGER.info(f"Tado CE buttons loaded: {len(buttons)}")
    else:
        _LOGGER.info("Tado CE: No buttons to create")


class TadoResumeAllSchedulesButton(ButtonEntity):
    """Button to resume schedules for all zones (delete all overlays)."""
    
    def __init__(self, hass: HomeAssistant):
        """Initialize the button."""
        self.hass = hass
        
        self._attr_name = "Tado CE Resume All Schedules"
        self._attr_unique_id = "tado_ce_resume_all_schedules"
        self._attr_device_info = get_hub_device_info()
        self._attr_icon = "mdi:calendar-refresh"
    
    async def async_press(self) -> None:
        """Handle button press - resume schedules for all zones."""
        from .async_api import get_async_client
        from .data_loader import load_zones_info_file
        from .immediate_refresh_handler import get_handler
        
        _LOGGER.info("Resume All Schedules button pressed")
        
        client = get_async_client(self.hass)
        zones_info = await self.hass.async_add_executor_job(load_zones_info_file)
        
        if not zones_info:
            _LOGGER.warning("No zones found to resume schedules")
            return
        
        success_count = 0
        fail_count = 0
        
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone_id}")
            
            try:
                if await client.delete_zone_overlay(zone_id):
                    _LOGGER.debug(f"Resumed schedule for {zone_name} (zone {zone_id})")
                    success_count += 1
                else:
                    # API returned False - might mean no overlay existed
                    _LOGGER.debug(f"No overlay to delete for {zone_name} (zone {zone_id})")
                    success_count += 1  # Still count as success
            except Exception as e:
                _LOGGER.error(f"Failed to resume schedule for {zone_name}: {e}")
                fail_count += 1
        
        if fail_count == 0:
            _LOGGER.info(f"Resume All Schedules complete: {success_count} zones processed")
        else:
            _LOGGER.warning(f"Resume All Schedules: {success_count} succeeded, {fail_count} failed")
        
        # Trigger immediate refresh to update all entities
        try:
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, "resume_all_schedules", force=True, skip_debounce=True)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")


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


class TadoRefreshScheduleButton(ButtonEntity):
    """Button to refresh schedule for a specific zone."""
    
    def __init__(self, hass: HomeAssistant, zone_id: str, zone_name: str):
        """Initialize the button.
        
        Args:
            hass: Home Assistant instance
            zone_id: Zone ID
            zone_name: Zone name
        """
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        
        self._attr_name = f"{zone_name} Refresh Schedule"
        self._attr_unique_id = f"tado_ce_{zone_id}_refresh_schedule"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HEATING")
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_icon = "mdi:calendar-refresh"
    
    async def async_press(self) -> None:
        """Handle button press - refresh schedule for this zone."""
        from .async_api import get_async_client
        from .calendar import SCHEDULES_FILE
        import json
        
        _LOGGER.info(f"Refresh Schedule button pressed for {self._zone_name} (zone {self._zone_id})")
        
        client = get_async_client(self.hass)
        
        try:
            # Fetch fresh schedule from API
            schedule_data = await client.get_zone_schedule(self._zone_id)
            
            if not schedule_data:
                _LOGGER.warning(f"No schedule data returned for {self._zone_name}")
                return
            
            # Load existing schedules
            def _load_schedules():
                if SCHEDULES_FILE.exists():
                    with open(SCHEDULES_FILE) as f:
                        return json.load(f)
                return {}
            
            schedules = await self.hass.async_add_executor_job(_load_schedules)
            
            # Update this zone's schedule
            schedules[self._zone_id] = {
                "name": self._zone_name,
                "type": schedule_data.get("type", "ONE_DAY"),
                "blocks": schedule_data.get("blocks", {}),
            }
            
            # Save back to file
            def _save_schedules():
                SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(SCHEDULES_FILE, 'w') as f:
                    json.dump(schedules, f, indent=2)
            
            await self.hass.async_add_executor_job(_save_schedules)
            
            _LOGGER.info(f"Schedule refreshed for {self._zone_name}")
            
            # Fire event to notify calendar entity to update
            self.hass.bus.async_fire(
                f"{DOMAIN}_schedule_updated",
                {"zone_id": self._zone_id, "zone_name": self._zone_name}
            )
            
        except Exception as e:
            _LOGGER.error(f"Failed to refresh schedule for {self._zone_name}: {e}")
