"""Tado CE schedule helpers: schedule block lookup and target temperature extraction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .data_loader import DataLoader


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
    """Resolve current time, falling back to UTC if HA is unavailable."""
    if current_time is not None:
        return current_time
    try:
        return dt_util.now()
    except (ValueError, TypeError):
        return datetime.now(UTC)


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
