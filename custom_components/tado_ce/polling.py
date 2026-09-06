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
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_DAY_INTERVAL,
    DEFAULT_NIGHT_INTERVAL,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    POLLING_SAFETY_BUFFER,
    QUOTA_RESERVE_CALLS,
)
from .helpers import hard_quota_reserve, low_quota_threshold, parse_iso_datetime

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


def _get_calls_per_sync(
    config_manager: ConfigurationManager,
    homekit_connected: bool = False,
) -> int:
    """Return the number of cloud API calls one poll cycle costs.

    Skips the zone-states call when HomeKit is providing live data,
    then adds optional weather, mobile-devices, and home-state calls
    based on the user's feature config.
    """
    calls = 0 if homekit_connected else 1

    if config_manager.get_weather_enabled():
        calls += 1

    if config_manager.get_mobile_devices_enabled() and config_manager.get_mobile_devices_frequent_sync():
        calls += 1

    if config_manager.get_home_state_sync_enabled():
        calls += 1

    return calls


def _build_polling_context(
    ratelimit_data: dict[str, Any],
    config_manager: ConfigurationManager,
    homekit_connected: bool = False,
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

    calls_per_sync = max(1, _get_calls_per_sync(config_manager, homekit_connected=homekit_connected))
    effective_remaining = remaining / calls_per_sync
    usable_quota = effective_remaining * POLLING_SAFETY_BUFFER - QUOTA_RESERVE_CALLS

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
        "(remaining=%s, usable=%.0f, reset=%.1fh)",
        interval_minutes, ctx.remaining, ctx.usable_quota, ctx.reset_hours,
    )

    return interval_minutes


def _calculate_low_quota_interval(ctx: _PollingContext) -> int | None:
    """Return the polling interval for low-quota users.

    Night holds the maximum interval to conserve quota; daytime
    distributes whatever is left after reserving night calls. Returns
    None at night so the caller falls back to the default / custom
    night interval.
    """
    night_calls = (ctx.night_duration * 60) / MAX_POLLING_INTERVAL
    usable_remaining = ctx.effective_remaining * POLLING_SAFETY_BUFFER - QUOTA_RESERVE_CALLS
    day_calls = usable_remaining - night_calls

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
        "(remaining=%s, day_calls=%.0f, night_reserved=%.0f)",
        day_interval, ctx.remaining, day_calls, night_calls,
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
        custom_night = config_manager.get_custom_night_interval()
        night_interval_for_calc = custom_night if custom_night is not None else MAX_POLLING_INTERVAL
        night_calls_needed = (ctx.night_duration * 60) / night_interval_for_calc
        time_boundary = f"night ({hours_until_night}h)"

    day_quota = max(0, ctx.usable_quota - night_calls_needed)

    if day_quota <= 0 or effective_hours <= 0:
        return MAX_POLLING_INTERVAL

    effective_minutes = effective_hours * 60
    interval_minutes = effective_minutes / day_quota
    interval_minutes = int(max(MIN_POLLING_INTERVAL, min(MAX_POLLING_INTERVAL, interval_minutes)))

    _LOGGER.debug(
        "Polling: daytime adaptive → %d min until %s "
        "(remaining=%s, day_quota=%.0f, night_reserved=%.0f)",
        interval_minutes, time_boundary, ctx.remaining,
        day_quota, night_calls_needed,
    )

    return interval_minutes


def calculate_adaptive_interval(
    ratelimit_data: dict[str, Any],
    config_manager: ConfigurationManager,
    homekit_connected: bool = False,
) -> int | None:
    """Pick the right mode-specific helper and return the calculated interval."""
    ctx = _build_polling_context(ratelimit_data, config_manager, homekit_connected=homekit_connected)

    if ctx.effective_remaining <= 0:
        _LOGGER.debug(
            "Polling: no quota remaining (effective_remaining=%s), "
            "falling back to max interval %d min",
            ctx.effective_remaining, MAX_POLLING_INTERVAL,
        )
        return MAX_POLLING_INTERVAL

    if ctx.is_uniform_mode:
        return _calculate_uniform_interval(ctx)

    if ctx.remaining <= low_quota_threshold(ctx.daily_limit):
        return _calculate_low_quota_interval(ctx)

    return _calculate_day_night_interval(ctx, config_manager)
def should_pause_polling(
    ratelimit_data: dict[str, Any],
    config_manager: ConfigurationManager,
    *,
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
    if not config_manager.get_quota_reserve_enabled():
        return False, ""

    last_reset_utc = ratelimit_data.get("last_reset_utc")
    if last_reset_utc:
        try:
            last_reset = parse_iso_datetime(last_reset_utc)
            next_reset = last_reset + timedelta(hours=24)
            now_utc = dt_util.utcnow()

            if now_utc >= next_reset:
                if was_paused:
                    _LOGGER.info(
                        "Polling: expected reset time %s UTC has passed, "
                        "resuming polling to pick up the actual reset",
                        next_reset.strftime("%H:%M"),
                    )
                return False, ""
        except (ValueError, TypeError) as e:
            _LOGGER.debug(
                "Polling: could not parse last_reset_utc (%s), "
                "treating reset time as unknown",
                e,
            )
    else:
        # No reset time known (fresh install / stale snapshot), so let
        # the next poll bootstrap rate-limit headers.
        return False, ""

    _remaining = ratelimit_data.get("remaining")
    remaining = _remaining if _remaining is not None else 100
    _limit = ratelimit_data.get("limit")
    daily_limit = _limit if _limit is not None else 100

    reserve_threshold = hard_quota_reserve(daily_limit)

    if remaining <= reserve_threshold:
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


def get_polling_interval(
    config_manager: ConfigurationManager,
    cached_ratelimit: dict[str, Any] | None = None,
    homekit_connected: bool = False,
) -> int:
    """Return the polling interval in minutes for the current day / night window.

    Honours a user-set custom interval unless the adaptive value
    indicates the quota is genuinely insufficient. Adaptive picks
    based on remaining quota + time until reset; falls back to the
    DEFAULT_DAY_INTERVAL / DEFAULT_NIGHT_INTERVAL if no rate-limit
    snapshot is available.
    """
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

    # Layer 1, HomeKit defer. When HomeKit provides live local data and the
    # user hasn't pinned a custom interval, the cloud-sync dial IS the cadence.
    # Don't run quota math: the dial already encodes the user's freshness vs.
    # quota trade-off. Without this, adaptive could pick a far faster cadence
    # and silently override the dial the user set.
    if homekit_connected and not user_set_custom:
        cloud_sync = config_manager.get_homekit_cloud_sync_minutes()
        _LOGGER.debug(
            "Polling: HomeKit connected, no custom interval, using "
            "cloud-sync dial %s min", cloud_sync,
        )
        return cloud_sync

    adaptive_interval = None
    try:
        ratelimit_data = None

        if cached_ratelimit is not None:
            ratelimit_data = cached_ratelimit

        if ratelimit_data:
            adaptive_interval = calculate_adaptive_interval(
                ratelimit_data, config_manager, homekit_connected=homekit_connected,
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
            return adaptive_interval
        _LOGGER.debug(
            "Polling: using custom %s interval %s min",
            "day" if daytime else "night",
            custom_interval,
        )
        return custom_interval
    if adaptive_interval is not None:
        return adaptive_interval
    return DEFAULT_DAY_INTERVAL if daytime else DEFAULT_NIGHT_INTERVAL

