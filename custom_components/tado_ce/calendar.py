"""Tado CE Calendar Platform - Zone Heating Schedules."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, DATA_DIR, MANUFACTURER
from .data_loader import load_zones_info_file
from .async_api import get_async_client

_LOGGER = logging.getLogger(__name__)

SCHEDULES_FILE = DATA_DIR / "schedules.json"

# Day type mappings
DAY_TYPES = {
    "ONE_DAY": ["MONDAY_TO_SUNDAY"],
    "THREE_DAY": ["MONDAY_TO_FRIDAY", "SATURDAY", "SUNDAY"],
    "SEVEN_DAY": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"],
}

DAY_TYPE_TO_WEEKDAYS = {
    "MONDAY_TO_SUNDAY": [0, 1, 2, 3, 4, 5, 6],
    "MONDAY_TO_FRIDAY": [0, 1, 2, 3, 4],
    "SATURDAY": [5],
    "SUNDAY": [6],
    "MONDAY": [0],
    "TUESDAY": [1],
    "WEDNESDAY": [2],
    "THURSDAY": [3],
    "FRIDAY": [4],
}


def get_schedule_device_info() -> DeviceInfo:
    """Get device info for Heating Schedule device."""
    return DeviceInfo(
        identifiers={(DOMAIN, "tado_ce_heating_schedule")},
        name="Heating Schedule",
        manufacturer=MANUFACTURER,
        model="Zone Schedules",
        via_device=(DOMAIN, "tado_ce_hub"),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tado CE calendar entities."""
    zones_info = await hass.async_add_executor_job(load_zones_info_file)
    
    if not zones_info:
        _LOGGER.warning("No zones found for calendar setup")
        return
    
    # Fetch all schedules first
    client = get_async_client(hass)
    schedules = {}
    
    for zone_data in zones_info:
        zone_id = str(zone_data.get("id", ""))
        zone_name = zone_data.get("name", f"Zone {zone_id}")
        zone_type = zone_data.get("type", "HEATING")
        
        # Only fetch HEATING zones
        if zone_type != "HEATING":
            continue
        
        try:
            schedule_data = await client.get_zone_schedule(zone_id)
            if schedule_data:
                schedules[zone_id] = {
                    "name": zone_name,
                    "type": schedule_data.get("type", "ONE_DAY"),
                    "blocks": schedule_data.get("blocks", {}),
                }
        except Exception as e:
            _LOGGER.error(f"Failed to fetch schedule for {zone_name}: {e}")
    
    # Save schedules to file
    await _async_save_schedules(hass, schedules)
    
    # Create calendar entity for each zone
    calendars = []
    for zone_id, schedule in schedules.items():
        calendars.append(
            TadoZoneScheduleCalendar(
                hass,
                zone_id,
                schedule["name"],
                schedule,
            )
        )
    
    async_add_entities(calendars)
    _LOGGER.info(f"Added {len(calendars)} Tado schedule calendars")


async def _async_save_schedules(hass: HomeAssistant, schedules: dict) -> None:
    """Save schedules to file."""
    def _save():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(SCHEDULES_FILE, 'w') as f:
            json.dump(schedules, f, indent=2)
    
    await hass.async_add_executor_job(_save)


class TadoZoneScheduleCalendar(CalendarEntity):
    """Calendar entity for a single zone's heating schedule."""
    
    _attr_has_entity_name = False  # Use full name, not device + entity name
    _attr_supported_features = 0  # Read-only
    
    def __init__(
        self,
        hass: HomeAssistant,
        zone_id: str,
        zone_name: str,
        schedule: dict,
    ) -> None:
        """Initialize the calendar."""
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._schedule = schedule
        
        # Short name for calendar sidebar (just zone name)
        self._attr_name = zone_name
        self._attr_unique_id = f"tado_ce_schedule_{zone_id}"
        self._attr_device_info = get_schedule_device_info()
        self._attr_icon = "mdi:calendar-clock"
        
        self._event: CalendarEvent | None = None
    
    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming event."""
        return self._event
    
    async def async_update(self) -> None:
        """Update the current event."""
        now = dt_util.now()
        today = now.date()
        
        events = await self.async_get_events(self.hass, today, today + timedelta(days=1))
        
        self._event = None
        for event in sorted(events, key=lambda e: e.start):
            if event.start <= now < event.end:
                self._event = event
                break
            elif event.start > now:
                self._event = event
                break
    
    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: date,
        end_date: date,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        events = []
        timetable_type = self._schedule.get("type", "ONE_DAY")
        blocks_by_day = self._schedule.get("blocks", {})
        
        current = start_date
        while current < end_date:
            weekday = current.weekday()
            day_blocks = self._get_blocks_for_weekday(weekday, timetable_type, blocks_by_day)
            
            for block in day_blocks:
                event = self._block_to_event(block, current)
                if event:
                    events.append(event)
            
            current += timedelta(days=1)
        
        return events
    
    def _get_blocks_for_weekday(
        self,
        weekday: int,
        timetable_type: str,
        blocks_by_day: dict,
    ) -> list[dict]:
        """Get schedule blocks for a specific weekday."""
        day_types = DAY_TYPES.get(timetable_type, ["MONDAY_TO_SUNDAY"])
        
        for day_type in day_types:
            weekdays = DAY_TYPE_TO_WEEKDAYS.get(day_type, [])
            if weekday in weekdays:
                return blocks_by_day.get(day_type, [])
        
        return []
    
    def _block_to_event(self, block: dict, event_date: date) -> CalendarEvent | None:
        """Convert a schedule block to a calendar event."""
        start_time = block.get("start", "00:00")
        end_time = block.get("end", "00:00")
        setting = block.get("setting", {})
        
        # Skip OFF blocks
        power = setting.get("power", "OFF")
        if power != "ON":
            return None
        
        temp = setting.get("temperature", {})
        if not temp:
            return None
        
        start_h, start_m = map(int, start_time.split(":"))
        end_h, end_m = map(int, end_time.split(":"))
        
        tz = dt_util.get_default_time_zone()
        start_dt = datetime(event_date.year, event_date.month, event_date.day, start_h, start_m, tzinfo=tz)
        
        if end_time == "00:00" and start_time != "00:00":
            end_dt = datetime(event_date.year, event_date.month, event_date.day, 23, 59, 59, tzinfo=tz)
        else:
            end_dt = datetime(event_date.year, event_date.month, event_date.day, end_h, end_m, tzinfo=tz)
        
        if start_dt >= end_dt:
            return None
        
        temp_c = temp.get("celsius", 0)
        
        # Include zone name in summary for calendar view
        return CalendarEvent(
            start=start_dt,
            end=end_dt,
            summary=f"{self._zone_name} {temp_c}°C",
        )
