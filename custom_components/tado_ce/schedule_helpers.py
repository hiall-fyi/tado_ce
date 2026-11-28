"""Tado CE schedule helpers: schedule block lookup and target temperature extraction."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import ACTIVE_TIMETABLE_IDS, DAY_TYPES, DOMAIN
from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError

if TYPE_CHECKING:
    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)


# Day type mapping for schedule parsing
DAY_TYPE_MAP = {
    0: "MONDAY",
    1: "TUESDAY",
    2: "WEDNESDAY",
    3: "THURSDAY",
    4: "FRIDAY",
    5: "SATURDAY",
    6: "SUNDAY",
}


def _get_day_blocks(blocks: dict[str, Any], schedule_type: str, weekday: int) -> list[Any]:
    """Get schedule blocks for a specific weekday (handles ONE_DAY / THREE_DAY / SEVEN_DAY layouts)."""
    # Tado API may return null for existing keys; 'or []' handles None correctly
    if schedule_type == "THREE_DAY":
        if weekday < 5:
            day_blocks = blocks.get("MONDAY_TO_FRIDAY") or []
        elif weekday == 5:
            day_blocks = blocks.get("SATURDAY") or []
        else:
            day_blocks = blocks.get("SUNDAY") or []
    elif schedule_type == "SEVEN_DAY":
        day_name = DAY_TYPE_MAP.get(weekday, "MONDAY")
        day_blocks = blocks.get(day_name) or []
    else:
        # ONE_DAY, or an unrecognised schedule_type: matches api_client.py and
        # calendar.py's own fallback for an unexpected timetable type.
        day_blocks = blocks.get("MONDAY_TO_SUNDAY") or []
    # Both current- and next-block scans assume ascending start-time order;
    # the Tado API response isn't guaranteed to arrive that way.
    return sorted(day_blocks, key=lambda b: b.get("start", "00:00"))


def _resolve_current_time(current_time: datetime | None) -> datetime:
    """Return the given time, else Home Assistant's local now."""
    if current_time is not None:
        return current_time
    return dt_util.now()


def _find_current_block(
    day_blocks: list[Any],
    current_time_str: str,
) -> dict[str, Any] | None:
    """Find the schedule block covering the given time (assumes blocks are sorted by start)."""
    current_block: dict[str, Any] | None = None
    for block in day_blocks:
        block_start: str = block.get("start", "00:00")
        if block_start <= current_time_str:
            current_block = block
        else:
            break
    return current_block


def next_schedule_change(zone_id: str, data_loader: DataLoader | None, after: datetime) -> datetime | None:
    """The first schedule block start after `after` within the next week, or None when the zone has no usable schedule."""
    if data_loader is None:
        return None
    schedule = data_loader.get_zone_schedule(zone_id)
    if not schedule:
        return None
    # A naive time is read as UTC rather than raising.
    moment = dt_util.as_local(after if after.tzinfo is not None else after.replace(tzinfo=UTC))
    blocks_dict: dict[str, Any] = schedule.get("blocks") or {}
    schedule_type: str = schedule.get("type", "ONE_DAY")
    for offset in range(8):
        day = moment.date() + timedelta(days=offset)
        for block in _get_day_blocks(blocks_dict, schedule_type, day.weekday()):
            try:
                hour, minute = (int(part) for part in str(block.get("start", "")).split(":")[:2])
                start = datetime.combine(day, time(hour, minute), tzinfo=moment.tzinfo)
            except ValueError:
                # A block with an unreadable start cannot say when the schedule changes, so it is skipped.
                continue
            if start > moment:
                return start
    return None


def _extract_block_celsius(block: dict[str, Any]) -> float | None:
    """Extract the target celsius from a schedule block, or None if OFF/missing."""
    setting: dict[str, Any] = block.get("setting") or {}
    if setting.get("power", "OFF") != "ON":
        return None
    temp_data: dict[str, Any] | None = setting.get("temperature")
    if not temp_data:
        return None
    return temp_data.get("celsius")


def get_current_schedule_target(
    zone_id: str,
    data_loader: DataLoader | None = None,
    current_time: datetime | None = None,
) -> float | None:
    """Get scheduled target temperature for the current time block (None when no schedule or block is OFF)."""
    if data_loader is None:
        return None

    schedule = data_loader.get_zone_schedule(zone_id)
    if not schedule:
        return None

    now = _resolve_current_time(current_time)
    blocks_dict: dict[str, Any] = schedule.get("blocks") or {}
    schedule_type: str = schedule.get("type", "ONE_DAY")
    day_blocks = _get_day_blocks(blocks_dict, schedule_type, now.weekday())

    if not day_blocks:
        return None

    current_block = _find_current_block(day_blocks, now.strftime("%H:%M"))
    if current_block is None:
        return None

    return _extract_block_celsius(current_block)


def get_active_timetable(zone_id: str, data_loader: DataLoader | None) -> str | None:
    """The zone's cached timetable type in lower case, or None when nothing usable is cached."""
    if data_loader is None:
        return None
    schedule = data_loader.get_zone_schedule(zone_id)
    if not isinstance(schedule, dict):
        return None
    timetable = schedule.get("type")
    if isinstance(timetable, str) and timetable in ACTIVE_TIMETABLE_IDS:
        return timetable.lower()
    return None


async def async_refresh_zone_schedule(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    *,
    fallback_type: str | None = None,
) -> bool:
    """Fetch the zone's schedule into the cache and tell the calendar; with a fallback type a failed fetch still stores that type."""
    schedule_data: dict[str, Any] | None = None
    failure: Exception | None = None
    try:
        schedule_data = await coordinator.api_client.get_zone_schedule(zone_id)
    except Exception as err:
        # With no fallback the caller decides; with one, the entry below must be written before any error leaves.
        if fallback_type is None:
            raise
        failure = err

    if schedule_data:
        entry = {
            "name": zone_name,
            "type": schedule_data.get("type", "ONE_DAY"),
            "blocks": schedule_data.get("blocks") or {},
            "failed_days": schedule_data.get("failed_days") or [],
        }
        await coordinator.data_loader.async_merge_store("schedules", {zone_id: entry})
        _LOGGER.debug("Schedule: refreshed %s, cache updated", zone_name)
        coordinator.hass.bus.async_fire(f"{DOMAIN}_schedule_updated", {"zone_id": zone_id, "zone_name": zone_name})
        # Only the calendar hears the event; the climate attributes read the cache on their next state write.
        coordinator.async_update_listeners()
        return True

    if fallback_type is None:
        _LOGGER.warning(
            "Schedule: fetch for %s returned no data, calendar will keep the previous schedule",
            zone_name,
        )
        return False

    _LOGGER.warning(
        "Schedule: switched %s to %s but could not read its blocks (%s), press Refresh Schedule to load them",
        zone_name, fallback_type, failure if failure is not None else "no data",
    )
    fallback = {"name": zone_name, "type": fallback_type, "blocks": {}, "failed_days": list(DAY_TYPES[fallback_type])}
    await coordinator.data_loader.async_merge_store("schedules", {zone_id: fallback})
    coordinator.hass.bus.async_fire(f"{DOMAIN}_schedule_updated", {"zone_id": zone_id, "zone_name": zone_name})
    coordinator.async_update_listeners()
    if isinstance(failure, TadoAuthError | TadoRateLimitError | TadoBlockedError):
        raise failure
    return False
