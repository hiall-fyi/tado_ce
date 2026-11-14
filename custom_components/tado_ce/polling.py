"""Tado CE adaptive polling: day/night windows, quota-aware backoff, low-quota guard.

Computes the next coordinator polling interval from current API quota,
the configured day / night hours, and feature flags (HomeKit connected
state changes the calls-per-sync count). Custom user intervals win
over the adaptive value unless quota would be exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_DAY_INTERVAL,
    DEFAULT_NIGHT_INTERVAL,
    LATE_RESET_FLOOR_MINUTES,
    LATE_RESET_WINDOW_HOURS,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    POLLING_SAFETY_BUFFER,
    RESET_CLAMP_SLACK_MINUTES,
)
from .fetch_plan import PlanConfig, has_custom_interval, steady_state_cost
from .helpers import predicted_next_reset
from .quota_levels import QuotaLevels

if TYPE_CHECKING:
    from .config_manager import ConfigurationManager

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PollingContext:
    """Derived polling calculation context: all values needed by mode helpers."""

    remaining: int
    effective_remaining: float
    usable_quota: float
    reset_hours: float
    is_day: bool
    is_uniform_mode: bool
    day_start: int
    night_start: int
    current_hour: int
    calls_per_sync: int
    night_duration: int
    day_duration: int
    daily_limit: int | None
    full_syncs_reserved: float
    levels: QuotaLevels
    night_interval_minutes: int


def _build_polling_context(
    ratelimit_data: dict[str, Any],
    config_manager: ConfigurationManager,
    homekit_connected: bool = False,
    *,
    full_sync_extra_calls: int,
    night_floor_minutes: int | None = None,
) -> _PollingContext:
    """Bundle every derived value the mode-specific helpers need.

    Centralises reset-time recalculation, calls-per-sync adjustment,
    day/night detection, and quota budgeting so each mode helper just
    reads from the immutable context instead of recomputing.
    """
    _remaining = ratelimit_data.get("remaining")
    remaining: int = int(_remaining) if _remaining is not None else 100
    _limit = ratelimit_data.get("limit")
    daily_limit: int | None = int(_limit) if _limit is not None else None
    _reset_sec = ratelimit_data.get("reset_seconds")
    reset_seconds: int = int(_reset_sec) if _reset_sec is not None else 86400
    last_reset_utc = ratelimit_data.get("last_reset_utc")

    # Prefer the live-calculated value over the stored snapshot when
    # we know when the quota last reset.
    if last_reset_utc:
        from .ratelimit import calculate_seconds_until_reset

        calculated = calculate_seconds_until_reset(last_reset_utc)
        if calculated is not None:
            reset_seconds = calculated

    reset_hours = reset_seconds / 3600

    per_poll = steady_state_cost(
        PlanConfig.from_config_manager(config_manager),
        homekit_connected=homekit_connected,
        user_has_custom_interval=has_custom_interval(config_manager),
    ).per_poll
    calls_per_sync = max(1, per_poll)
    effective_remaining = remaining / calls_per_sync
    # WHY: effective_remaining counts syncs, so the reserve (in calls) is divided by the sync cost.
    levels = QuotaLevels(QuotaLevels.normalise_limit(daily_limit))
    usable_quota = effective_remaining * POLLING_SAFETY_BUFFER - levels.interval_reserve / calls_per_sync
    # WHY: full syncs run on their own clock, so the extra calls expected before the reset come off the top (in syncs).
    full_syncs_reserved = (reset_seconds / levels.full_sync_period) * full_sync_extra_calls / calls_per_sync
    usable_quota -= full_syncs_reserved

    now = dt_util.now()
    day_start = config_manager.get_day_start_hour()
    night_start = config_manager.get_night_start_hour()
    is_day = is_daytime(config_manager)

    if night_start > day_start:
        night_duration = 24 - night_start + day_start
    else:
        night_duration = day_start - night_start
    day_duration = 24 - night_duration

    return _PollingContext(
        remaining=remaining,
        effective_remaining=effective_remaining,
        usable_quota=usable_quota,
        reset_hours=reset_hours,
        is_day=is_day,
        is_uniform_mode=(day_start == night_start),
        day_start=day_start,
        night_start=night_start,
        current_hour=now.hour,
        calls_per_sync=calls_per_sync,
        night_duration=night_duration,
        day_duration=day_duration,
        daily_limit=daily_limit,
        full_syncs_reserved=full_syncs_reserved,
        levels=levels,
        # WHY: the night floor is a HomeKit-path rule, so a cloud-only entry still prices its night at the maximum.
        night_interval_minutes=night_floor_minutes if night_floor_minutes and homekit_connected else MAX_POLLING_INTERVAL,
    )


def _calculate_uniform_interval(ctx: _PollingContext) -> int:
    """Return the polling interval when day_start == night_start.

    Uniform mode treats the whole 24 h as a single quota window, with no
    day / night distinction, and spreads the usable quota evenly.
    """
    effective_hours = ctx.reset_hours
    night_calls_needed = 0

    day_quota = max(0, ctx.usable_quota - night_calls_needed)

    if day_quota <= 0 or effective_hours <= 0:
        return MAX_POLLING_INTERVAL

    effective_minutes = effective_hours * 60
    interval_minutes = effective_minutes / day_quota
    interval_minutes = int(max(MIN_POLLING_INTERVAL, min(MAX_POLLING_INTERVAL, interval_minutes)))

    _LOGGER.debug(
        "Polling: uniform mode → %d min "
        "(remaining=%s, usable=%.0f, reset=%.1fh, full_syncs_reserved=%.1f)",
        interval_minutes, ctx.remaining, ctx.usable_quota, ctx.reset_hours, ctx.full_syncs_reserved,
    )

    return interval_minutes


def _night_reserved_calls(ctx: _PollingContext, config_manager: ConfigurationManager) -> float:
    """Return the syncs the coming night will spend, at the custom night interval when one is set."""
    custom_night = config_manager.get_custom_night_interval()
    return (ctx.night_duration * 60) / (custom_night if custom_night is not None else ctx.night_interval_minutes)


def _calculate_low_quota_interval(ctx: _PollingContext, config_manager: ConfigurationManager) -> int | None:
    """Return the polling interval for low-quota users.

    Night holds the maximum interval to conserve quota; daytime
    distributes whatever is left after reserving night calls. Returns
    None at night so the caller falls back to the default / custom
    night interval.
    """
    night_calls = _night_reserved_calls(ctx, config_manager)
    day_calls = ctx.usable_quota - night_calls

    if day_calls <= 0:
        if not ctx.is_day:
            return None
        return MAX_POLLING_INTERVAL

    if not ctx.is_day:
        return None

    day_interval = (ctx.day_duration * 60) / day_calls
    day_interval = int(max(MIN_POLLING_INTERVAL, min(MAX_POLLING_INTERVAL, day_interval)))

    _LOGGER.debug(
        "Polling: low-quota daytime → %d min "
        "(remaining=%s, day_calls=%.0f, night_reserved=%.0f, full_syncs_reserved=%.1f)",
        day_interval, ctx.remaining, day_calls, night_calls, ctx.full_syncs_reserved,
    )

    return day_interval


def _calculate_day_night_interval(
    ctx: _PollingContext,
    config_manager: ConfigurationManager,
) -> int | None:
    """Return the daytime adaptive interval, taking quota reset-time into account.

    Returns None at night so the caller uses the default / custom
    night interval. During the day the helper picks the closer of
    "until quota reset" and "until night start" as the budgeting
    horizon, and reserves quota for the upcoming night when night
    comes first.
    """
    if not ctx.is_day:
        return None

    if ctx.current_hour < ctx.night_start:
        hours_until_night = ctx.night_start - ctx.current_hour
    else:
        hours_until_night = 24 - ctx.current_hour + ctx.night_start

    if ctx.reset_hours < hours_until_night:
        effective_hours = ctx.reset_hours
        night_calls_needed = 0.0
        time_boundary = f"reset ({ctx.reset_hours:.1f}h)"
    else:
        effective_hours = float(hours_until_night)
        night_calls_needed = _night_reserved_calls(ctx, config_manager)
        time_boundary = f"night ({hours_until_night}h)"

    day_quota = max(0, ctx.usable_quota - night_calls_needed)

    if day_quota <= 0 or effective_hours <= 0:
        return MAX_POLLING_INTERVAL

    effective_minutes = effective_hours * 60
    interval_minutes = effective_minutes / day_quota
    interval_minutes = int(max(MIN_POLLING_INTERVAL, min(MAX_POLLING_INTERVAL, interval_minutes)))

    _LOGGER.debug(
        "Polling: daytime adaptive → %d min until %s "
        "(remaining=%s, day_quota=%.0f, night_reserved=%.0f, full_syncs_reserved=%.1f)",
        interval_minutes, time_boundary, ctx.remaining,
        day_quota, night_calls_needed, ctx.full_syncs_reserved,
    )

    return interval_minutes


def calculate_adaptive_interval(
    ratelimit_data: dict[str, Any],
    config_manager: ConfigurationManager,
    homekit_connected: bool = False,
    *,
    full_sync_extra_calls: int,
    night_floor_minutes: int | None = None,
) -> int | None:
    """Pick the right mode-specific helper and return the calculated interval."""
    ctx = _build_polling_context(
        ratelimit_data, config_manager, homekit_connected=homekit_connected,
        full_sync_extra_calls=full_sync_extra_calls, night_floor_minutes=night_floor_minutes,
    )

    if ctx.effective_remaining <= 0:
        _LOGGER.debug(
            "Polling: no quota remaining (effective_remaining=%s), "
            "falling back to max interval %d min",
            ctx.effective_remaining, MAX_POLLING_INTERVAL,
        )
        return MAX_POLLING_INTERVAL

    if ctx.is_uniform_mode:
        return _calculate_uniform_interval(ctx)

    if ctx.levels.low_quota_mode(ctx.remaining):
        return _calculate_low_quota_interval(ctx, config_manager)

    return _calculate_day_night_interval(ctx, config_manager)


def should_pause_polling(
    ratelimit_data: dict[str, Any],
    config_manager: ConfigurationManager,
    *,
    poll_cost: int,
    was_paused: bool = False,
    can_keep_entities_live: bool = False,
) -> tuple[bool, str]:
    """Return (should_pause, user_facing_reason) when quota is too low to keep polling.

    Pausing reserves the last few calls so the user can still set
    temperature, change mode, etc. Resumes automatically once the
    reset time has passed so we can detect the actual reset from the
    next API response.

    `was_paused` lets the caller suppress the "resuming polling" INFO
    line when polling was never actually paused (e.g. high-tier user
    with plenty of quota). Without it, the INFO line would fire every
    cycle once the expected reset window had passed but the API
    response hadn't yet refreshed `last_reset_utc`.

    `can_keep_entities_live` picks the reason text: the manual-actions
    claim is only true when the caller's own decision keeps entities live
    through the pause.
    """
    levels = QuotaLevels.build(ratelimit_data, config_manager)
    now = dt_util.utcnow()
    last_reset_utc = ratelimit_data.get("last_reset_utc")
    if levels.reserve_enabled and last_reset_utc:
        next_reset = predicted_next_reset(last_reset_utc)
        if next_reset is None:
            _LOGGER.debug(
                "Polling: could not parse last_reset_utc (%r), "
                "treating reset time as unknown",
                last_reset_utc,
            )
        elif now >= next_reset and was_paused:
            _LOGGER.info(
                "Polling: expected reset time %s UTC has passed, "
                "resuming polling to pick up the actual reset",
                next_reset.strftime("%H:%M"),
            )

    if levels.poll_holds(ratelimit_data, poll_cost, now):
        remaining = ratelimit_data["remaining"]
        # WHY: the caller prices the poll about to run, so a poll never ends inside the reserve.
        reserve_threshold = levels.hard_reserve + poll_cost
        _rs = ratelimit_data.get("reset_seconds")
        reset_seconds = _rs if _rs is not None else 0

        if not reset_seconds and last_reset_utc:
            from .ratelimit import calculate_seconds_until_reset

            reset_seconds = calculate_seconds_until_reset(last_reset_utc) or 0

        if reset_seconds > 0:
            hours = reset_seconds // 3600
            minutes = (reset_seconds % 3600) // 60
            reset_info = f"reset in {hours}h {minutes}m"
        else:
            reset_info = "reset time unknown, will resume after the next successful API response"

        if can_keep_entities_live:
            tail = "Manual actions (set temperature, change mode, etc.) still work."
        else:
            tail = "Entities are unavailable until quota resets or the next successful update."
        reason = (
            f"Quota critically low ({remaining} remaining, reserve threshold={reserve_threshold}). "
            f"Polling paused until {reset_info}. {tail}"
        )
        return True, reason

    return False, ""


def is_daytime(config_manager: ConfigurationManager) -> bool:
    """Return True when the current local time falls inside the configured day window.

    Uniform mode (`day_start == night_start`) always reports day so
    every poll uses the day interval. Handles the wrap-around case
    where night_start < day_start (e.g. night=01:00, day=06:00).
    """
    now = dt_util.now()
    hour = now.hour

    day_start = config_manager.get_day_start_hour()
    night_start = config_manager.get_night_start_hour()

    if day_start == night_start:
        return True

    if day_start < night_start:
        return day_start <= hour < night_start

    return hour >= day_start or hour < night_start


def clamp_interval_to_reset(interval: int, floor: int, last_reset_utc: str | None) -> int:
    """Return the interval cut so the next poll lands just after the predicted quota reset, never below the floor."""
    if not last_reset_utc:
        return interval
    from .ratelimit import calculate_seconds_until_reset

    seconds = calculate_seconds_until_reset(last_reset_utc)
    if seconds is None:
        return interval
    return max(floor, min(interval, math.ceil(seconds / 60) + RESET_CLAMP_SLACK_MINUTES))


def _is_at_quota_reserve(ratelimit_data: dict[str, Any]) -> bool:
    """Return True when remaining is unknown or at the hard reserve, where no optional polling may spend."""
    levels = QuotaLevels(QuotaLevels.normalise_limit(ratelimit_data.get("limit")))
    return levels.quota_level(ratelimit_data) in (None, "empty", "reserve")


def late_reset_floor_minutes(interval: int, floor: int, ratelimit_data: dict[str, Any]) -> int:
    """Return the interval capped at 15 min while the predicted reset is up to 2 h overdue and has not been seen."""
    # WHY: the stored last_reset_utc moves when a reset is detected, so "predicted <= now" with an unmoved value means it is still overdue.
    predicted = predicted_next_reset(ratelimit_data.get("last_reset_utc"))
    if predicted is None or _is_at_quota_reserve(ratelimit_data):
        return interval
    now = dt_util.utcnow()
    if not predicted <= now < predicted + timedelta(hours=LATE_RESET_WINDOW_HOURS):
        return interval
    capped = min(interval, max(floor, LATE_RESET_FLOOR_MINUTES))
    if capped != interval:
        _LOGGER.debug("Polling: predicted reset is overdue, capping interval %s -> %s min", interval, capped)
    return capped


def _interval_floor(config_manager: ConfigurationManager, homekit_connected: bool) -> int:
    """Return the shortest interval the reset clamp may pick: the dial with HomeKit, the adaptive floor without."""
    return config_manager.get_homekit_cloud_sync_minutes() if homekit_connected else MIN_POLLING_INTERVAL


def get_polling_interval(
    config_manager: ConfigurationManager,
    cached_ratelimit: dict[str, Any] | None = None,
    homekit_connected: bool = False,
    *,
    full_sync_extra_calls: int,
    night_floor_minutes: int | None = None,
) -> int:
    """Return the polling interval in minutes for the current day / night window, cut to the predicted reset."""
    interval, user_set_custom = _select_polling_interval(
        config_manager, cached_ratelimit, homekit_connected, full_sync_extra_calls=full_sync_extra_calls,
        night_floor_minutes=night_floor_minutes,
    )
    if user_set_custom or not cached_ratelimit:
        return interval
    floor = _interval_floor(config_manager, homekit_connected)
    interval = late_reset_floor_minutes(interval, floor, cached_ratelimit)
    return clamp_interval_to_reset(interval, floor, cached_ratelimit.get("last_reset_utc"))


def _homekit_effective_interval(
    config_manager: ConfigurationManager,
    cached_ratelimit: dict[str, Any] | None,
    daytime: bool,
    *,
    full_sync_extra_calls: int,
    night_floor_minutes: int | None,
) -> int:
    """Return max(Cloud Data Refresh dial, adaptive or the window default, capped at the night floor); the dial alone without a snapshot."""
    dial = config_manager.get_homekit_cloud_sync_minutes()
    if not cached_ratelimit:
        return dial
    adaptive_interval = None
    try:
        adaptive_interval = calculate_adaptive_interval(
            cached_ratelimit, config_manager, homekit_connected=True, full_sync_extra_calls=full_sync_extra_calls,
            night_floor_minutes=night_floor_minutes,
        )
    except (ValueError, TypeError, AttributeError) as e:
        _LOGGER.debug("Polling: could not calculate adaptive interval (%s), falling back to default", e)
    if adaptive_interval is None:
        adaptive_interval = DEFAULT_DAY_INTERVAL if daytime else DEFAULT_NIGHT_INTERVAL
    if night_floor_minutes and not daytime:
        adaptive_interval = min(adaptive_interval, night_floor_minutes)
    return max(dial, adaptive_interval)


def _select_polling_interval(
    config_manager: ConfigurationManager,
    cached_ratelimit: dict[str, Any] | None,
    homekit_connected: bool,
    *,
    full_sync_extra_calls: int,
    night_floor_minutes: int | None,
) -> tuple[int, bool]:
    """Return (interval, is_user_custom) before the reset clamp: a custom interval wins unless quota cannot sustain it."""
    daytime = is_daytime(config_manager)

    custom_day_interval = config_manager.get_custom_day_interval()
    custom_night_interval = config_manager.get_custom_night_interval()

    user_set_custom = False
    custom_interval = None
    if daytime and custom_day_interval is not None:
        custom_interval = custom_day_interval
        user_set_custom = True
    elif not daytime and custom_night_interval is not None:
        custom_interval = custom_night_interval
        user_set_custom = True

    if homekit_connected and not user_set_custom:
        return _homekit_effective_interval(
            config_manager, cached_ratelimit, daytime, full_sync_extra_calls=full_sync_extra_calls,
            night_floor_minutes=night_floor_minutes,
        ), False

    adaptive_interval = None
    try:
        ratelimit_data = None

        if cached_ratelimit is not None:
            ratelimit_data = cached_ratelimit

        if ratelimit_data:
            adaptive_interval = calculate_adaptive_interval(
                ratelimit_data, config_manager, homekit_connected=homekit_connected,
                full_sync_extra_calls=full_sync_extra_calls, night_floor_minutes=night_floor_minutes,
            )

    except (ValueError, TypeError, AttributeError) as e:
        _LOGGER.debug(
            "Polling: could not calculate adaptive interval (%s), "
            "falling back to default",
            e,
        )

    # Honour the user's explicit custom interval unless adaptive math shows the
    # quota genuinely cannot sustain it. A custom interval is an explicit user
    # choice, so the adaptive floor (which caps only the *automatic* cadence)
    # must not bump it up. The override therefore fires only when the adaptive
    # value sits ABOVE the floor, which means the quota math itself widened the
    # cadence, a real constraint. When adaptive is clamped down onto the floor
    # the quota is healthy, so a faster custom interval (even below the floor)
    # is honoured as-is.
    if user_set_custom and custom_interval is not None:
        if (
            adaptive_interval is not None
            and adaptive_interval > custom_interval
            and adaptive_interval > MIN_POLLING_INTERVAL
        ):
            _LOGGER.warning(
                "Polling: custom interval %s min would burn through "
                "the remaining quota, using adaptive %s min instead "
                "to protect the day's calls",
                custom_interval, adaptive_interval,
            )
            return adaptive_interval, False
        _LOGGER.debug(
            "Polling: using custom %s interval %s min",
            "day" if daytime else "night",
            custom_interval,
        )
        return custom_interval, True
    if adaptive_interval is not None:
        return adaptive_interval, False
    return (DEFAULT_DAY_INTERVAL if daytime else DEFAULT_NIGHT_INTERVAL), False

