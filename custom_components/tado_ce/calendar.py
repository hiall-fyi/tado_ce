"""Tado CE calendar platform: read-only heating schedule view per zone.

One calendar entity per HEATING zone, showing the cloud-side
schedule blocks as `CalendarEvent`s. Schedules rarely change, so
the platform serves cached values aggressively to avoid burning
API quota on every entry-setup retry: a stale schedule is
strictly better than no calendar.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo  # type: ignore[attr-defined]
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ASSUMED_DAILY_LIMIT_UNKNOWN_TIER,
    DAY_TYPES,
    DOMAIN,
    MANUFACTURER,
    MAX_SCHEDULE_FETCH_CALLS,
    SCHEDULE_FILL_MAX_QUOTA_FRACTION,
)
from .device_manager import hub_parent_kwargs
from .entity_registry import ENTITY_REGISTRY
from .helpers import hard_quota_reserve

if TYPE_CHECKING:
    from homeassistant.core import Event
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


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


def schedule_fetch_budget(ratelimit_data: dict[str, Any]) -> int:
    """Return how many API calls this setup run may spend filling the schedule cache.

    Two terms: spend down to the reserve that pauses polling, and never more
    than a fraction of a day's allowance in one run, since a large SEVEN_DAY
    home would otherwise fetch 72 calls at once.
    """
    # Defaulted separately: an unknown tier budgets as the smallest, an
    # unknown `remaining` assumes a full day. Defaulting both together would
    # discard a known-low `remaining` when only the limit is missing.
    limit = ratelimit_data.get("limit") or ASSUMED_DAILY_LIMIT_UNKNOWN_TIER
    remaining = ratelimit_data.get("remaining")
    if remaining is None:
        remaining = limit

    spendable = remaining - hard_quota_reserve(limit)
    per_run_cap = int(limit * SCHEDULE_FILL_MAX_QUOTA_FRACTION)
    return max(0, min(spendable, per_run_cap))


def get_schedule_device_info(home_id: str) -> DeviceInfo:
    """Build the HA `DeviceInfo` for the per-home Heating Schedule device."""
    schedule_identifier = f"tado_ce_{home_id}_heating_schedule" if home_id != "unknown" else "tado_ce_heating_schedule"

    parent = hub_parent_kwargs(home_id)

    return DeviceInfo(
        configuration_url="https://app.tado.com",
        identifiers={(DOMAIN, schedule_identifier)},
        name="Heating Schedule",
        manufacturer=MANUFACTURER,
        model="Zone Schedules",
        **parent,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE calendar entities."""
    coordinator = entry.runtime_data
    data_loader = coordinator.data_loader
    home_id = coordinator.home_id
    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)

    if not zones_info:
        _LOGGER.warning(
            "Calendar: no zones available, calendar entities will "
            "be created after the next successful zone fetch",
        )
        return

    # Cached schedules are reused aggressively, see module
    # docstring for the quota / staleness trade-off.
    client = coordinator.api_client
    cached_schedules = data_loader.load_schedules_file() or {}

    ratelimit_data = data_loader.load_ratelimit_file() or {}
    call_budget = schedule_fetch_budget(ratelimit_data)
    starting_budget = call_budget

    schedules: dict[str, Any] = {}
    cached_hits = 0
    fetched = 0
    skipped_low_quota = 0

    for zone_data in zones_info:
        zone_id = str(zone_data.get("id", ""))
        zone_name = zone_data.get("name", f"Zone {zone_id}")
        zone_type = zone_data.get("type")

        if zone_type != "HEATING":
            continue

        cached = cached_schedules.get(zone_id)
        if cached:
            schedules[zone_id] = cached
            cached_hits += 1
            continue

        # Worst case up front: the real cost is only known once the call
        # returns the timetable type.
        if call_budget < MAX_SCHEDULE_FETCH_CALLS:
            skipped_low_quota += 1
            continue

        call_budget -= 1  # the activeTimetable call
        try:
            schedule_data = await client.get_zone_schedule(zone_id)
            if schedule_data:
                schedule_type = schedule_data.get("type", "ONE_DAY")
                schedules[zone_id] = {
                    "name": zone_name,
                    "type": schedule_type,
                    "blocks": schedule_data.get("blocks") or {},
                    "failed_days": schedule_data.get("failed_days") or [],
                }
                fetched += 1
                call_budget -= len(DAY_TYPES.get(schedule_type, DAY_TYPES["ONE_DAY"]))
        except Exception:
            _LOGGER.warning(
                "Calendar: schedule fetch for %s failed, zone "
                "will be retried on the next setup",
                zone_name,
                exc_info=True,
            )

    # Merge this run's results onto the full on-disk cache, so a zone this
    # run didn't touch (a different zone type, or one this run skipped)
    # keeps whatever schedule it already had rather than being dropped.
    if fetched > 0:
        merged = dict(cached_schedules)
        merged.update(schedules)
        await _async_save_schedules(hass, merged, home_id, data_loader=data_loader)

    _LOGGER.info(
        "Calendar: %d zone(s) loaded: %d from cache, %d fetched, "
        "%d skipped (quota low)",
        len(schedules), cached_hits, fetched, skipped_low_quota,
    )

    # One line per run, not per zone: the recovery is the same for all of them.
    if skipped_low_quota:
        _LOGGER.warning(
            "Calendar: %d zone(s) have no schedule yet, this setup run's "
            "budget of %d API call(s) is spent. They fill on a later "
            "restart, or press a zone's Refresh Schedule button to fetch "
            "it now.",
            skipped_low_quota, starting_budget,
        )

    # Create calendar entity for each zone
    calendars = []
    for zone_id, schedule in schedules.items():
        calendars.append(
            TadoZoneScheduleCalendar(
                coordinator,
                zone_id,
                schedule["name"],
                schedule,
                home_id,
            ),
        )

    async_add_entities(calendars)


async def _async_save_schedules(
    hass: HomeAssistant,
    schedules: dict[str, Any],
    home_id: str | None = None,
    data_loader: DataLoader | None = None,
) -> None:
    """Persist the latest schedules dict via DataLoader's auxiliary store."""
    if data_loader is not None:
        await data_loader.async_update_store("schedules", schedules)


class TadoZoneScheduleCalendar(CoordinatorEntity["TadoDataUpdateCoordinator"], CalendarEntity):
    """Read-only calendar surfacing one heating zone's schedule blocks as events."""

    _attr_has_entity_name = True
    _attr_supported_features = 0  # Read-only

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        schedule: dict[str, Any],
        home_id: str = "",
    ) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._schedule = schedule

        # Short name for calendar sidebar (just zone name)
        _meta = ENTITY_REGISTRY["calendar_schedule"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_schedule_device_info(home_id)
        if _meta.icon:
            self._attr_icon = _meta.icon

        self._event: CalendarEvent | None = None
        self._unsub_schedule_update = None

    async def async_added_to_hass(self) -> None:
        """Listen for the Refresh Schedule button's bus event.

        Bus events fire outside the coordinator's poll cycle.
        The dedicated listener is how the Refresh Schedule
        button asks the calendar to reload without forcing a
        full integration refresh.
        """
        await super().async_added_to_hass()

        @callback
        def _handle_schedule_update(event: Event) -> None:
            """Reload this zone when the Refresh Schedule button fires."""
            event_zone_id = event.data.get("zone_id")
            if event_zone_id == self._zone_id:
                _LOGGER.debug(
                    "Calendar: zone %s schedule refresh requested, "
                    "reloading from cache",
                    self._zone_name,
                )
                self.hass.async_create_task(self._async_reload_schedule())

        self._unsub_schedule_update = self.hass.bus.async_listen(  # type: ignore[assignment]
            f"{DOMAIN}_schedule_updated",
            _handle_schedule_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister event listener when entity is removed."""
        if self._unsub_schedule_update:
            self._unsub_schedule_update()
            self._unsub_schedule_update = None
        await super().async_will_remove_from_hass()

    async def _async_reload_schedule(self) -> None:
        """Pull the latest schedule for this zone from the DataLoader cache."""
        try:
            # Cache is populated write-through by the Refresh
            # Schedule button, so this read sees the freshest data
            # without an extra API call.
            cached = self.coordinator.data_loader.get_cached("schedules")
            if cached and isinstance(cached, dict) and self._zone_id in cached:
                self._schedule = cached[self._zone_id]
                _LOGGER.debug(
                    "Calendar: zone %s schedule reloaded from cache",
                    self._zone_name,
                )
                self.async_write_ha_state()
                return
            else:
                self._schedule = {}
                self.async_write_ha_state()

        except Exception:
            _LOGGER.warning(
                "Calendar: zone %s schedule reload failed, keeping "
                "the previous schedule until the next refresh",
                self._zone_name,
                exc_info=True,
            )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming event."""
        return self._event

    async def async_update(self) -> None:
        """Refresh `event` to whichever block is current or next upcoming today."""
        now = dt_util.now()
        today = now.date()

        events = await self.async_get_events(self.hass, today, today + timedelta(days=1))

        self._event = None
        for event in sorted(events, key=lambda e: e.start):
            if event.start <= now < event.end or event.start > now:
                self._event = event
                break

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: date,
        end_date: date,
    ) -> list[CalendarEvent]:
        """Return all schedule events between `start_date` and `end_date`."""
        events = []
        timetable_type = self._schedule.get("type", "ONE_DAY")
        blocks_by_day = self._schedule.get("blocks") or {}

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
        blocks_by_day: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Pick the schedule blocks defined for this weekday under the timetable."""
        day_types = DAY_TYPES.get(timetable_type, ["MONDAY_TO_SUNDAY"])

        for day_type in day_types:
            weekdays = DAY_TYPE_TO_WEEKDAYS.get(day_type, [])
            if weekday in weekdays:
                return blocks_by_day.get(day_type) or []

        return []

    def _block_to_event(self, block: dict[str, Any], event_date: date) -> CalendarEvent | None:
        """Render a single schedule block as a `CalendarEvent`, or None to skip it."""
        start_time = block.get("start", "00:00")
        end_time = block.get("end", "00:00")
        setting = block.get("setting") or {}

        # OFF blocks are explicit "no heating" gaps in the schedule,
        # so they shouldn't appear as events on the calendar.
        power = setting.get("power", "OFF")
        if power != "ON":
            return None

        temp = setting.get("temperature") or {}
        if not temp:
            return None

        start_h, start_m = map(int, start_time.split(":"))
        end_h, end_m = map(int, end_time.split(":"))

        tz = dt_util.get_default_time_zone()
        start_dt = datetime(event_date.year, event_date.month, event_date.day, start_h, start_m, tzinfo=tz)

        if end_time == "00:00" and start_time != "00:00":
            # Tado encodes "ends at midnight" as 00:00 on the same
            # day, which would compute as an empty interval, so clamp
            # to 23:59:59 instead.
            end_dt = datetime(event_date.year, event_date.month, event_date.day, 23, 59, 59, tzinfo=tz)
        else:
            end_dt = datetime(event_date.year, event_date.month, event_date.day, end_h, end_m, tzinfo=tz)

        if start_dt >= end_dt:
            return None

        temp_c = temp.get("celsius", 0)

        return CalendarEvent(
            start=start_dt,
            end=end_dt,
            summary=f"{self._zone_name} {temp_c}°C",
        )
