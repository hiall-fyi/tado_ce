"""Tado CE API client: async HTTP with rate-limit accounting and per-entry isolation."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timedelta
from http import HTTPStatus
import logging
import math
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import probatio as vol

from .api_auth import TadoAuthMixin
from .api_call_tracker import (
    CALL_TYPE_CAPABILITIES,
    CALL_TYPE_CHILD_LOCK,
    CALL_TYPE_DEVICE_OFFSET_READ,
    CALL_TYPE_DEVICE_OFFSET_WRITE,
    CALL_TYPE_DEVICES,
    CALL_TYPE_GRAPHQL,
    CALL_TYPE_HEATING_CIRCUITS,
    CALL_TYPE_HOME_STATE,
    CALL_TYPE_MOBILE_DEVICES,
    CALL_TYPE_OVERLAY,
    CALL_TYPE_PRESENCE_LOCK,
    CALL_TYPE_WEATHER,
    CALL_TYPE_ZONE_STATES,
    CALL_TYPE_ZONES,
    is_quota_call,
)
from .api_schemas import (
    ACTIVE_TIMETABLE,
    DEVICE_OFFSET,
    EARLY_START,
    HEATING_CIRCUITS,
    HOME_DEVICES,
    HOME_STATE,
    MOBILE_DEVICES,
    TIMETABLE_BLOCKS,
    WEATHER,
    ZONE_CAPABILITIES,
    ZONE_CONTROL,
    ZONE_STATES,
    ZONES_INFO,
    ResponseSchema,
    describe_invalid,
)
from .cloud_health import BACKOFF_CAP_SECONDS, CloudEvent, CloudHealth
from .const import (
    ACTIVE_TIMETABLE_IDS,
    API_ENDPOINT_DEVICES,
    CAPABILITY_ZONE_TYPES,
    DAY_TYPES,
    DEVICE_OFFSET_MAX,
    DEVICE_OFFSET_MIN,
    DOMAIN,
    MAX_CUSTOM_INTERVAL,
    MAX_RETRY_ATTEMPTS,
    QUOTA_RESET_EARLY_WINDOW_MINUTES,
    QUOTA_WARNING_PERCENTAGE,
    TADO_API_BASE,
    TADO_GRAPHQL_API,
    ZONE_STATES_EMPTY_CONFIRMATIONS,
    is_valid_device_offset,
)
from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError, TadoSyncError
from .fetch_plan import (
    GROUP_FULL_SYNC,
    KIND_CAPABILITIES,
    KIND_CIRCUIT_CONTROL,
    KIND_DEVICES,
    KIND_EARLY_START,
    KIND_HEATING_CIRCUITS,
    KIND_HOME_STATE,
    KIND_MOBILE_FULL,
    KIND_MOBILE_QUICK,
    KIND_OFFSETS_FULL,
    KIND_SVC_SCHEDULES,
    KIND_WEATHER,
    KIND_ZONE_STATES,
    KIND_ZONES_INFO,
    FetchPlan,
    FetchTally,
    ItemResult,
)
from .helpers import (
    mask_api_url,
    mask_response_body,
    mask_serial,
    parse_iso_datetime,
    predicted_next_reset,
    retry_delay,
    unlisted_zone_keys,
)
from .quota_levels import QuotaLevels
from .ratelimit import calculate_seconds_until_reset

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api_call_tracker import APICallTracker
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)

# Holds the task that opened a user-write block, so only that task gets a lost connection as an error instead of a plain None.
_RAISE_NETWORK_ERRORS: ContextVar[asyncio.Task[Any] | None] = ContextVar("tado_raise_network_errors", default=None)


@contextmanager
def raising_network_errors() -> Iterator[None]:
    """Make every api_call awaited inside the block raise a lost connection once retries are exhausted."""
    token = _RAISE_NETWORK_ERRORS.set(asyncio.current_task())
    try:
        yield
    finally:
        _RAISE_NETWORK_ERRORS.reset(token)

# HTTP timeout for Tado Cloud API calls (30s covers slow responses; normal < 5s)
_API_CALL_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Methods safe to retry on transient 403 (idempotent)
_RETRYABLE_METHODS = frozenset({"GET", "PUT", "DELETE"})
_TOKEN_OUTAGE_EVENTS = frozenset({CloudEvent.TOKEN_5XX_EXHAUSTED, CloudEvent.TOKEN_TIMEOUT, CloudEvent.TOKEN_NETWORK_EXHAUSTED})


def _finite_seconds(value: object) -> float:
    """Return `value` as a finite non-negative float; anything else counts as 0."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def _format_reset_display(
    now_utc: datetime,
    calculated_reset_seconds: int | None,
    fallback_reset_seconds: int,
) -> tuple[str | None, str | None, int]:
    """Return `(reset_at_iso, reset_human, reset_seconds)` for sensor display."""
    if calculated_reset_seconds and calculated_reset_seconds > 0:
        hours = calculated_reset_seconds // 3600
        minutes = (calculated_reset_seconds % 3600) // 60
        reset_human = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        reset_dt = now_utc + timedelta(seconds=calculated_reset_seconds)
        return reset_dt.isoformat(), reset_human, calculated_reset_seconds
    return None, None, fallback_reset_seconds


def _detect_call_type(endpoint: str, method: str = "GET") -> int | None:
    """Map an endpoint and HTTP method to the matching `CALL_TYPE_*` constant."""
    if endpoint == "devices":
        return CALL_TYPE_DEVICES
    if endpoint.startswith("devices/"):
        if endpoint.endswith("/temperatureOffset"):
            return CALL_TYPE_DEVICE_OFFSET_WRITE if method == "PUT" else CALL_TYPE_DEVICE_OFFSET_READ
        if endpoint.endswith("/childLock"):
            return CALL_TYPE_CHILD_LOCK
    if endpoint == "heatingCircuits":
        return CALL_TYPE_HEATING_CIRCUITS
    if endpoint == "graphql":
        return CALL_TYPE_GRAPHQL
    if "zoneStates" in endpoint:
        return CALL_TYPE_ZONE_STATES
    if "weather" in endpoint:
        return CALL_TYPE_WEATHER
    if "capabilities" in endpoint:
        return CALL_TYPE_CAPABILITIES
    if "zones" in endpoint and "overlay" not in endpoint:
        return CALL_TYPE_ZONES
    if "mobileDevices" in endpoint:
        return CALL_TYPE_MOBILE_DEVICES
    if "overlay" in endpoint:
        return CALL_TYPE_OVERLAY
    if "presenceLock" in endpoint:
        return CALL_TYPE_PRESENCE_LOCK
    if endpoint == "state":
        return CALL_TYPE_HOME_STATE
    return None


# Empty zone replies in a row per config entry; module level so a setup retry (a new client) still counts them.
_EMPTY_ZONE_REPLIES: dict[str, int] = {}


class TadoApiClient(TadoAuthMixin):
    """Async Tado cloud API client scoped to one config entry."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        hass: HomeAssistant | None = None,
        home_id: str | None = None,
        refresh_token: str | None = None,
        api_tracker: APICallTracker | None = None,
        data_loader: DataLoader | None = None,
        config_entry: ConfigEntry | None = None,
        cloud_health: CloudHealth | None = None,
    ) -> None:
        """Initialise the client. `home_id` and `refresh_token` come from the entry data."""
        self._session = session
        self._hass = hass  # Store hass for real-time config access
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._refresh_lock = asyncio.Lock()
        self._rate_limit: dict[str, Any] = {}
        self._home_id: str | None = home_id
        self._injected_refresh_token: str | None = refresh_token
        self._api_tracker = api_tracker
        self._data_loader = data_loader
        self._config_entry = config_entry
        self._schema_warned: set[str] = set()
        self._cloud_health = cloud_health if cloud_health is not None else CloudHealth(home_id)

    @property
    def cloud_health(self) -> CloudHealth:
        """The CloudHealth shared with the coordinator."""
        return self._cloud_health

    async def _ensure_home_id(self) -> str | None:
        """Return the home ID, loading from config if not injected at construction."""
        if self._home_id is None:
            config = await self._load_config()
            self._home_id = config.get("home_id")
        return self._home_id

    def _parse_ratelimit_headers(self, headers: dict[str, Any]) -> None:
        """Parse Tado's `RateLimit-Policy` and `RateLimit` response headers.

        Tado uses RFC 8030-style headers. On 200 responses, `t=` often
        points to midnight UTC rather than the actual ~11:24 UTC
        reset, and `w=86400` is the window size, not time-to-reset.
        We clear `reset_seconds` here so save_ratelimit falls back to
        one of the other reset strategies. Exception: if a prior 429
        set `reset_seconds` via Retry-After (RFC 6585, reliable),
        the `_from_429` flag preserves it across this parse and gets
        consumed.
        """
        policy = ""
        ratelimit = ""
        others: dict[str, Any] = {}
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower == "ratelimit-policy":
                policy = value
            elif key_lower == "ratelimit":
                ratelimit = value
            elif key_lower.startswith(("ratelimit", "x-ratelimit")) or key_lower == "retry-after":
                others[key] = value

        _LOGGER.debug(
            "API: rate-limit headers, policy=%s, ratelimit=%s",
            policy, ratelimit,
        )
        if others:
            _LOGGER.debug("API: other rate-limit headers: %s", others)

        if "q=" in policy:
            with suppress(ValueError, IndexError):
                self._rate_limit["limit"] = int(policy.split("q=")[1].split(";")[0])

        parsed_remaining = False
        if "r=" in ratelimit:
            with suppress(ValueError, IndexError):
                self._rate_limit["remaining"] = int(ratelimit.split("r=")[1].split(";")[0])
                parsed_remaining = True

        # Decisions read this pair, so calls made since the last saved snapshot count.
        if parsed_remaining and self._data_loader is not None:
            live = {
                key: value for key in ("limit", "remaining")
                if isinstance(value := self._rate_limit.get(key), int)
            }
            self._data_loader.update_cache("ratelimit_live", live)

        if not self._rate_limit.get("_from_429"):
            self._rate_limit.pop("reset_seconds", None)
        else:
            del self._rate_limit["_from_429"]

        _LOGGER.debug("API: parsed rate limit: %s", self._rate_limit)

    def _seconds_until_quota_reset(self) -> int | None:
        """Return seconds to the next quota reset from the cached snapshot, or None when it is absent or unusable."""
        cached = self._data_loader.get_cached("ratelimit") if self._data_loader is not None else None
        last_reset = cached.get("last_reset_utc") if isinstance(cached, dict) else None
        return calculate_seconds_until_reset(last_reset) if isinstance(last_reset, str) else None

    async def _load_ratelimit(self) -> dict[str, Any]:
        """Load rate limit from DataLoader cache, with Store fallback."""
        if self._data_loader is not None:
            data = self._data_loader.get_cached("ratelimit")
            if data is not None and isinstance(data, dict):
                return data
            # Cache not yet populated, load directly from Store
            store = self._data_loader._stores.get("ratelimit")
            if store:
                loaded = await store.async_load()
                if loaded is not None and isinstance(loaded, dict):
                    return loaded
        return {}

    def _calculate_live_ratelimit(
        self,
        now_utc: datetime,
        prev_data: dict[str, Any],
        real_limit: int,
        real_remaining: int,
        prev_remaining: int | None,
        last_reset_utc: str | None,
    ) -> dict[str, Any]:
        """Calculate ratelimit values from real API headers."""
        limit = real_limit
        remaining = real_remaining
        used = limit - remaining
        percentage_used = round((used / limit) * 100, 1) if limit > 0 else 0

        stored_slack = prev_data.get("last_reset_slack_minutes")
        # A bool is an int in Python, so it is excluded by hand.
        slack = (
            min(stored_slack, MAX_CUSTOM_INTERVAL)
            if isinstance(stored_slack, int) and not isinstance(stored_slack, bool) and stored_slack >= 0
            else 0
        )
        window = max(QUOTA_RESET_EARLY_WINDOW_MINUTES, slack)

        # Remaining only falls between resets, so any rise near or after the predicted reset counts; the jump threshold misses quiet days.
        # A reset leaves calls remaining, so an exhausted count (used is then the limit, not a counter) never is one.
        if prev_remaining is not None and remaining is not None and remaining > 0:
            reset_threshold = max(20, int(limit * 0.05))
            rise = remaining - prev_remaining
            next_reset = predicted_next_reset(last_reset_utc)
            prev_limit = prev_data.get("limit")
            prev_used = prev_data.get("used")
            if not isinstance(prev_used, int) or isinstance(prev_used, bool):
                prev_used = (
                    prev_limit - prev_remaining
                    if isinstance(prev_limit, int) and not isinstance(prev_limit, bool)
                    else None
                )
            # A fall in used is the counter resetting; a raised limit lifts remaining without it.
            if prev_used is not None:
                rise = prev_used - used
            reason = ""
            if rise > reset_threshold:
                reason = "jump above the threshold"
            elif (
                rise > 0
                and next_reset is not None
                and now_utc >= next_reset - timedelta(minutes=window)
            ):
                reason = "rise near the predicted reset"
            elif (
                isinstance(prev_limit, int)
                and not isinstance(prev_limit, bool)
                and limit != prev_limit
                and prev_used is not None
                and used < prev_used
            ):
                reason = "limit changed and used fell"
            if reason:
                last_reset_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                slack = self._reset_record_lag_minutes(now_utc, prev_data)
                _LOGGER.info(
                    "API: Tado quota reset detected at %s "
                    "(remaining %s → %s, threshold %s, %s, slack %d min)",
                    last_reset_utc, prev_remaining, remaining, reset_threshold, reason, slack,
                )

        return {
            "limit": limit,
            "used": used,
            "remaining": remaining,
            "percentage_used": percentage_used,
            "last_reset_utc": last_reset_utc,
            "last_reset_slack_minutes": slack,
        }

    @staticmethod
    def _reset_record_lag_minutes(now_utc: datetime, prev_data: dict[str, Any]) -> int:
        """Return the minutes since the previous save, an upper bound of how late a reset was seen."""
        updated = prev_data.get("last_updated")
        if not isinstance(updated, str):
            return 0
        try:
            gap = now_utc - parse_iso_datetime(updated)
        except (ValueError, TypeError, OverflowError):
            # A missing or malformed stamp only means no widened window.
            _LOGGER.debug("API: previous rate-limit save time unreadable, reset slack 0")
            return 0
        return max(0, min(math.ceil(gap.total_seconds() / 60), MAX_CUSTOM_INTERVAL))

    def _calculate_reset_seconds(
        self,
        now_utc: datetime,
        api_reset_seconds: int,
        last_reset_utc: str | None,
        used: int,
    ) -> tuple[int | None, str | None]:
        """Resolve seconds-until-reset using four strategies in order of accuracy."""
        calculated: int | None = None

        if api_reset_seconds and api_reset_seconds > 0:
            return api_reset_seconds, last_reset_utc

        if last_reset_utc:
            calculated = self._reset_from_last_known(now_utc, last_reset_utc)
            if calculated is not None:
                return calculated, last_reset_utc

        if used > 0:
            calculated, last_reset_utc = self._reset_from_extrapolation(
                now_utc, used, last_reset_utc,
            )
            if calculated is not None:
                return calculated, last_reset_utc

        calculated = self._reset_from_call_history(now_utc)
        return calculated, last_reset_utc

    def _reset_from_last_known(
        self, now_utc: datetime, last_reset_utc: str,
    ) -> int | None:
        """Strategy 2: roll the last known reset forward 24 h until it lands in the future."""
        try:
            last_reset = parse_iso_datetime(last_reset_utc)
            next_reset = last_reset + timedelta(hours=24)
            while next_reset <= now_utc:
                next_reset += timedelta(hours=24)
            seconds_until_reset = int((next_reset - now_utc).total_seconds())
            if seconds_until_reset > 0:
                _LOGGER.debug(
                    "API: reset strategy 2, next reset at %s UTC "
                    "(rolled forward from last known)",
                    next_reset.strftime("%H:%M"),
                )
                return seconds_until_reset
        except (ValueError, TypeError) as e:
            _LOGGER.debug(
                "API: reset strategy 2, could not parse last_reset_utc "
                "(%s)",
                e,
            )
        return None

    def _reset_from_extrapolation(
        self,
        now_utc: datetime,
        used: int,
        last_reset_utc: str | None,
    ) -> tuple[int | None, str | None]:
        """Strategy 3: derive the last reset from the current usage rate."""
        tracker = self._api_tracker
        if not tracker:
            return None, last_reset_utc
        try:
            estimated_reset = tracker.extrapolate_reset_time(used)
            if estimated_reset:
                if not last_reset_utc:
                    last_reset_utc = estimated_reset.strftime("%Y-%m-%dT%H:%M:%SZ")
                    _LOGGER.debug(
                        "API: reset strategy 3, derived last reset %s "
                        "from usage rate",
                        last_reset_utc,
                    )
                next_reset = estimated_reset + timedelta(hours=24)
                seconds_until_reset = int((next_reset - now_utc).total_seconds())
                if seconds_until_reset > 0:
                    _LOGGER.debug(
                        "API: reset strategy 3, using extrapolated "
                        "reset at %s UTC",
                        estimated_reset.strftime("%H:%M"),
                    )
                    return seconds_until_reset, last_reset_utc
        except (ValueError, TypeError, KeyError) as e:
            _LOGGER.debug(
                "API: reset strategy 3, extrapolation failed (%s)", e,
            )
        return None, last_reset_utc

    @staticmethod
    def _find_first_calls_by_day(all_calls: list[dict[str, Any]]) -> dict[str, Any]:
        """Find the earliest API call for each day from call history."""
        first_calls_by_day: dict[str, Any] = {}
        for call in all_calls:
            call_time = parse_iso_datetime(call["timestamp"])
            date_key = call_time.strftime("%Y-%m-%d")
            if date_key not in first_calls_by_day or call_time < first_calls_by_day[date_key]:
                first_calls_by_day[date_key] = call_time
        return first_calls_by_day

    @staticmethod
    def _find_most_common_reset_hour(
        first_calls_by_day: dict[str, Any],
    ) -> tuple[int, int] | None:
        """Find the most common hour from first-call-of-day data.

        Returns (most_common_hour, count) or None if insufficient data.
        """
        hour_counts: dict[int, int] = {}
        for first_call in first_calls_by_day.values():
            hour = first_call.hour
            if first_call.minute >= 30:
                hour = (hour + 1) % 24
            hour_counts[hour] = hour_counts.get(hour, 0) + 1

        if not hour_counts:
            return None
        most_common_hour = max(hour_counts, key=hour_counts.get)  # type: ignore[arg-type]
        count = hour_counts[most_common_hour]
        if count < 2:
            return None
        return most_common_hour, count

    @staticmethod
    def _average_reset_minute(
        first_calls_by_day: dict[str, Any], target_hour: int,
    ) -> int | None:
        """Calculate average minute-of-day for calls matching the target hour."""
        minutes_in_hour = []
        for first_call in first_calls_by_day.values():
            call_hour = first_call.hour
            if first_call.minute >= 30:
                call_hour = (call_hour + 1) % 24
            if call_hour == target_hour:
                minutes_in_hour.append(int(first_call.hour * 60 + first_call.minute))
        if not minutes_in_hour:
            return None
        return sum(minutes_in_hour) // len(minutes_in_hour)

    def _reset_from_call_history(self, now_utc: datetime) -> int | None:
        """Strategy 4: pick the modal first-call-of-day hour from 14 d of call history."""
        tracker = self._api_tracker
        if not tracker:
            return None
        try:
            all_calls = [c for c in tracker.get_call_history(days=14) if is_quota_call(c)]
            first_calls_by_day = self._find_first_calls_by_day(all_calls)

            if len(first_calls_by_day) < 2:
                return None

            hour_result = self._find_most_common_reset_hour(first_calls_by_day)
            if hour_result is None:
                _LOGGER.debug(
                    "API: reset strategy 4, only %d day(s) of history "
                    "and no hour with 2+ matches",
                    len(first_calls_by_day),
                )
                return None

            most_common_hour, match_count = hour_result
            avg_minutes = self._average_reset_minute(first_calls_by_day, most_common_hour)
            if avg_minutes is None:
                return None

            reset_hour = avg_minutes // 60
            reset_minute = avg_minutes % 60

            today_reset = now_utc.replace(
                hour=reset_hour, minute=reset_minute, second=0, microsecond=0,
            )
            next_reset = today_reset + timedelta(days=1) if today_reset <= now_utc else today_reset

            seconds_until_reset = int((next_reset - now_utc).total_seconds())
            if seconds_until_reset > 0:
                _LOGGER.debug(
                    "API: reset strategy 4, estimated reset at "
                    "%02d:%02d UTC (mode of %d day(s), %d match(es))",
                    reset_hour, reset_minute,
                    len(first_calls_by_day), match_count,
                )
                return seconds_until_reset
        except (ValueError, TypeError, KeyError) as e:
            _LOGGER.debug(
                "API: reset strategy 4, call-history mode failed (%s)",
                e,
            )
        return None

    async def save_ratelimit(self, status: str = "ok") -> None:
        """Persist the current rate-limit snapshot, running the four-strategy reset resolver."""
        now_utc = dt_util.utcnow()
        prev_data = await self._load_ratelimit()

        # No response parsed since start: keep the saved numbers, not a made-up 5000 that reads as a reset.
        real_limit = self._rate_limit.get("limit", prev_data.get("limit", 5000))
        real_remaining = self._rate_limit.get("remaining", prev_data.get("remaining", 5000))
        reset_seconds = self._rate_limit.get("reset_seconds", 0)

        prev_remaining = prev_data.get("remaining")
        last_reset_utc = prev_data.get("last_reset_utc")

        result = self._calculate_live_ratelimit(
            now_utc, prev_data, real_limit, real_remaining, prev_remaining, last_reset_utc,
        )

        limit = result["limit"]
        used = result["used"]
        remaining = result["remaining"]
        percentage_used = result["percentage_used"]
        last_reset_utc = result.get("last_reset_utc", last_reset_utc)

        calculated_reset_seconds, last_reset_utc = self._calculate_reset_seconds(
            now_utc, reset_seconds, last_reset_utc, used,
        )
        reset_at, reset_human, reset_seconds = _format_reset_display(
            now_utc, calculated_reset_seconds, reset_seconds,
        )

        if remaining == 0:
            status = "rate_limited"
        elif percentage_used > QUOTA_WARNING_PERCENTAGE:
            status = "warning"

        data: dict[str, Any] = {
            "limit": limit,
            "remaining": remaining,
            "used": used,
            "percentage_used": percentage_used,
            "reset_seconds": reset_seconds or None,
            "reset_at": reset_at,
            "reset_human": reset_human,
            "last_updated": now_utc.isoformat(),
            "last_reset_utc": last_reset_utc,
            "last_reset_slack_minutes": result["last_reset_slack_minutes"],
            "status": status,
        }

        try:
            await self._save_ratelimit(data)
            _LOGGER.debug(
                "API: rate-limit saved: %s/%s used (%s%%)",
                used, limit, percentage_used,
            )
        except (OSError, HomeAssistantError) as e:
            _LOGGER.debug(
                "API: could not save rate limit (%s), using in-memory "
                "value for this cycle",
                e,
            )

    async def _save_ratelimit(self, data: dict[str, Any]) -> None:
        """Persist rate-limit data through the DataLoader Store."""
        if self._data_loader is not None:
            await self._data_loader.async_update_store("ratelimit", data)

    async def _handle_401(
        self, method: str, endpoint: str, attempt: int,
    ) -> bool:
        """Handle a 401, refreshing the token once for idempotent first-attempt calls."""
        endpoint = mask_api_url(endpoint)  # logged and raised below
        if method in _RETRYABLE_METHODS and attempt == 1:
            _LOGGER.debug(
                "API: token expired mid-call (%s %s), refreshing and retrying",
                method, endpoint,
            )
            self._access_token = None
            self._token_expiry = None
            return True
        self._access_token = None
        self._token_expiry = None
        if method not in _RETRYABLE_METHODS:
            _LOGGER.warning(
                "API: token expired on %s %s, not retried (non-idempotent), "
                "token cleared for next attempt",
                method, endpoint,
            )
            raise TadoSyncError(
                f"Token expired on {method} {endpoint}, try again",
            )
        _LOGGER.warning(
            "API: token expired on %s %s, retry exhausted, surfacing for reauth",
            method, endpoint,
        )
        raise TadoAuthError(
            f"Token rejected on non-retryable call {method} {endpoint}",
        )

    async def _handle_403(
        self, method: str, endpoint: str, attempt: int,
    ) -> bool:
        """Retry an idempotent 403: usually a transient CDN/WAF block, not a token issue.

        Only clears the token after retries are exhausted, so a
        single transient 403 doesn't cost the user a re-auth round
        trip on the next call.
        """
        endpoint = mask_api_url(endpoint)  # logged and raised below
        if attempt < MAX_RETRY_ATTEMPTS:
            _LOGGER.debug(
                "API: HTTP 403 on %s %s, retry %s/%s",
                method, endpoint, attempt, MAX_RETRY_ATTEMPTS,
            )
            delay = retry_delay(attempt)
            await asyncio.sleep(delay)
            return True
        self._access_token = None
        self._token_expiry = None
        _LOGGER.warning(
            "API: HTTP 403 after %s retry attempts on %s %s, Tado is refusing "
            "this connection (not a login problem)%s",
            MAX_RETRY_ATTEMPTS, method, endpoint,
            ", will retry on the next poll" if method == "GET" else ", this change was not made",
        )
        # WHY: a fresh token was refused, so the credentials are not at fault; TadoBlockedError keeps the reauth flow out of it.
        raise TadoBlockedError(
            f"Persistent 403 after {MAX_RETRY_ATTEMPTS} retries on {method} {endpoint}",
        )

    async def _resolve_api_url(self, endpoint: str, full_url: str | None) -> str | None:
        """Resolve the absolute URL: prefer `full_url` when given, else build from home ID."""
        if full_url:
            return full_url
        config = await self._load_config()
        home_id = config.get("home_id")
        if not home_id:
            self.note_cloud(CloudEvent.HOME_ID_MISSING)
            raise TadoAuthError(
                f"Home ID missing, cannot resolve {mask_api_url(endpoint)}, re-authenticate to fix",
            )
        return f"{TADO_API_BASE}/homes/{home_id}/{endpoint}"

    async def _handle_error_status(
        self,
        status: int,
        method: str,
        endpoint: str,
        attempt: int,
        is_safe_to_retry: bool,
        response_body: str = "",
    ) -> str:
        """Decide how to react to a non-2xx status: `continue` / `return_none` / `return_success`."""
        endpoint = mask_api_url(endpoint)  # logged and raised below
        if status == HTTPStatus.UNAUTHORIZED:
            if await self._handle_401(method, endpoint, attempt):
                return "continue"
            return "return_none"

        if status == HTTPStatus.FORBIDDEN and is_safe_to_retry:
            if await self._handle_403(method, endpoint, attempt):
                return "continue"
            return "return_none"

        # 422 Unprocessable Entity is a semantic rejection from
        # Tado, not a server failure, so it is never retried.
        if status == HTTPStatus.UNPROCESSABLE_ENTITY:
            _LOGGER.warning(
                "API: %s %s rejected by Tado (HTTP 422): %s",
                method, endpoint,
                mask_response_body(response_body) if response_body else "no response body",
            )
            return "return_none"

        # DELETE-then-404 means the resource was already cleared by
        # someone else. Treat as success so the caller doesn't retry.
        if status == HTTPStatus.NOT_FOUND and method == "DELETE":
            _LOGGER.debug(
                "API: DELETE %s returned 404, resource already gone, "
                "treating as success",
                endpoint,
            )
            return "return_success"

        if status == HTTPStatus.TOO_MANY_REQUESTS:
            retry_after_seconds = self._rate_limit.get("reset_seconds", 0) or 0
            _LOGGER.warning(
                "API: HTTP 429 on %s %s, surfacing to coordinator for "
                "honoured backoff (retry_after=%ss)",
                method, endpoint, retry_after_seconds,
            )
            raise TadoRateLimitError(
                f"Rate-limited on {method} {endpoint}",
                retry_after=min(retry_after_seconds, BACKOFF_CAP_SECONDS),
            )

        if status >= HTTPStatus.INTERNAL_SERVER_ERROR and is_safe_to_retry:
            if attempt < MAX_RETRY_ATTEMPTS:
                delay = retry_delay(attempt)
                _LOGGER.debug(
                    "API: HTTP %s on %s %s (attempt %s/%s), retrying in %.1fs",
                    status, method, endpoint,
                    attempt, MAX_RETRY_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)
                return "continue"
            _LOGGER.warning(
                "API: HTTP %s on %s %s after %s retry attempts, giving up",
                status, method, endpoint, MAX_RETRY_ATTEMPTS,
            )
            return "return_none"

        if response_body:
            _LOGGER.warning(
                "API: %s %s failed with HTTP %s: %s",
                method, endpoint, status, mask_response_body(response_body),
            )
        else:
            _LOGGER.warning(
                "API: %s %s failed with HTTP %s",
                method, endpoint, status,
            )
        return "return_none"

    async def _should_retry_network_error(
        self, attempt: int, error_type: str, method: str, endpoint: str,
    ) -> bool:
        """Sleep + return True when a network error has retries left, else log and return False."""
        endpoint = mask_api_url(endpoint)  # logged below
        if attempt < MAX_RETRY_ATTEMPTS:
            delay = retry_delay(attempt)
            _LOGGER.debug(
                "API: %s on %s %s (attempt %s/%s), retrying in %.1fs",
                error_type, method, endpoint,
                attempt, MAX_RETRY_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)
            return True
        _LOGGER.warning(
            "API: %s on %s %s, exhausted %s retry attempts",
            error_type, method, endpoint, MAX_RETRY_ATTEMPTS,
        )
        return False

    async def _execute_single_api_attempt(
        self,
        method: str,
        url: str,
        token: str,
        data: dict[str, Any] | None,
        success_statuses: set[int | HTTPStatus],
        parse_ratelimit: bool,
        attempt: int,
        tracker: APICallTracker | None,
        call_type: int | None,
    ) -> tuple[dict[str, Any] | None, bool, int]:
        """Execute a single API attempt. Returns (result, should_continue, status)."""
        async with self._session.request(
            method, url,
            headers={"Authorization": f"Bearer {token}"},
            json=data if method in ("PUT", "POST") else None,
            timeout=_API_CALL_TIMEOUT,
        ) as resp:
            if parse_ratelimit and attempt == 1:
                self._parse_ratelimit_headers(dict(resp.headers))
            if tracker and call_type is not None:
                await tracker.async_record_call(call_type, resp.status)

            if resp.status in success_statuses:
                if resp.status == HTTPStatus.NO_CONTENT or resp.content_length == 0:
                    return {}, False, resp.status
                return await resp.json(), False, resp.status

            try:
                response_body = await resp.text()
            except Exception:
                # Body read is diagnostic-only; the outer error path logs
                # the request status, so an empty body is acceptable.
                response_body = ""

            if resp.status == HTTPStatus.TOO_MANY_REQUESTS:
                # A header-less 429 must not reuse the previous 429's window
                self._rate_limit.pop("reset_seconds", None)
                retry_after_header = resp.headers.get("Retry-After")
                if retry_after_header:
                    try:
                        self._rate_limit["reset_seconds"] = int(float(retry_after_header))
                        self._rate_limit["_from_429"] = True
                    except (ValueError, TypeError, OverflowError):
                        _LOGGER.debug("api_client: non-numeric Retry-After header %r, ignoring", retry_after_header)

            action = await self._handle_error_status(
                resp.status, method, mask_api_url(url), attempt,
                method in _RETRYABLE_METHODS,
                response_body=response_body,
            )
            if action == "return_success":
                return {}, False, resp.status
            return None, action == "continue", resp.status

    def _checked(
        self, result: dict[str, Any] | None, schema: ResponseSchema | None,
    ) -> dict[str, Any] | None:
        """Return `result` if it has the expected shape, else warn once per label and return None."""
        if schema is None or result is None:
            return result
        try:
            schema.check(result)
        except vol.Invalid as err:
            if schema.label not in self._schema_warned:
                self._schema_warned.add(schema.label)
                _LOGGER.warning(
                    "API: %s response did not have the expected shape (%s), "
                    "keeping the previous data",
                    schema.label, describe_invalid(err),
                )
            return None
        self._schema_warned.discard(schema.label)
        return result

    def _note_rest_result(self, checked: dict[str, Any] | None, method: str, status: int) -> None:
        """Write a REST success (2xx with a valid body) or an exhausted retried 5xx to CloudHealth."""
        if checked is not None and 200 <= status < 300:
            self.note_cloud(CloudEvent.REST_SUCCESS)
        elif checked is None and status >= HTTPStatus.INTERNAL_SERVER_ERROR and method in _RETRYABLE_METHODS:
            self.note_cloud(CloudEvent.REST_5XX_EXHAUSTED)

    async def api_call(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        parse_ratelimit: bool = True,
        full_url: str | None = None,
        extra_success_statuses: frozenset[int] | None = None,
        schema: ResponseSchema | None = None,
        raise_network_errors: bool = False,
    ) -> dict[str, Any] | None:
        """Authenticated request with transient-403 retry on idempotent methods.

        GET / PUT / DELETE retry on 403 (typically a CDN / WAF block).
        POST never retries, being non-idempotent. A 401 on the first
        attempt of an idempotent method triggers a single token
        refresh + retry; on POST or subsequent attempts it's a hard
        failure.
        """
        url = await self._resolve_api_url(endpoint, full_url)
        if url is None:
            return None
        rest = url.startswith(TADO_API_BASE)

        call_type = _detect_call_type(endpoint, method)
        tracker = self._api_tracker
        log_endpoint = mask_api_url(endpoint)

        _success: set[int | HTTPStatus] = {HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.NO_CONTENT}
        if extra_success_statuses:
            _success |= extra_success_statuses

        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            self._last_noted_event = None
            token = await self.get_access_token()
            if not token:
                if self._last_noted_event in _TOKEN_OUTAGE_EVENTS:
                    cause = "a network failure is blocking the token refresh"
                elif not self._injected_refresh_token:
                    cause = "there is no saved refresh token, you may need to sign in to Tado again"
                else:
                    cause = "the token refresh did not succeed, will try again on the next call"
                _LOGGER.warning(
                    "API: could not get access token for %s %s, %s",
                    method, log_endpoint, cause,
                )
                return None

            try:
                result, should_continue, status = await self._execute_single_api_attempt(
                    method, url, token, data, _success,
                    parse_ratelimit, attempt, tracker, call_type,
                )
                if should_continue:
                    continue
                checked = self._checked(result, schema)
                if rest:
                    self._note_rest_result(checked, method, status)
                return checked
            except (TimeoutError, aiohttp.ClientError):
                # A 200 with a non-JSON content type (ContentTypeError) is a ClientError too: it counts as an unreachable answer
                if not await self._should_retry_network_error(attempt, "network error", method, log_endpoint):
                    if rest:
                        self.note_cloud(CloudEvent.REST_NETWORK_EXHAUSTED)
                    # A caller that must tell an unknown outcome from a refusal asks for the error.
                    if raise_network_errors or _RAISE_NETWORK_ERRORS.get() is asyncio.current_task():
                        raise
                    return None
            except TadoAuthError:
                if rest:
                    self.note_cloud(CloudEvent.REST_401_SECOND)
                raise
            except TadoRateLimitError as exc:
                # Quota exhaustion must reach the coordinator dispatch
                # (repair issue + retry-after backoff) and the background
                # write dispatcher. Never collapse to a None return.
                if rest:
                    self.note_cloud(
                        CloudEvent.REST_429,
                        retry_after=_finite_seconds(exc.retry_after),
                        exhausted=self._rate_limit.get("remaining") == 0,
                        reset_cap_seconds=self._seconds_until_quota_reset(),
                    )
                raise
            except TadoBlockedError:
                if rest:
                    self.note_cloud(CloudEvent.REST_403_PERSISTENT)
                raise
            except TadoSyncError:
                # Transient sync errors are the coordinator's to classify;
                # swallowing here would mislabel them as a generic failure.
                raise
            except Exception:
                _LOGGER.warning(
                    "API: unexpected error on %s %s, call abandoned, "
                    "next poll will retry",
                    method, log_endpoint,
                    exc_info=True,
                )
                return None

        return None

    async def _graphql_call(
        self, query: str, variables: dict[str, Any],
    ) -> dict[str, Any] | None:
        """POST a query/mutation to Tado's GraphQL gateway; returns `data`, or None on failure.

        Reuses `api_call`'s auth/retry handling via `full_url`. Rate-limit
        header parsing is off: the tracked quota belongs to `my.tado.com`,
        and a gateway response parsed into it would clear the reset time a
        prior 429 left there. GraphQL returns HTTP 200 even on failure, so a non-empty top-level
        `errors` array is treated as failure here; a mutation's own
        `success`/`failureReason` is the caller's to interpret, since only
        the caller knows the field name.
        """
        result = await self.api_call(
            "graphql",
            method="POST",
            data={"query": query, "variables": variables},
            parse_ratelimit=False,
            full_url=TADO_GRAPHQL_API,
        )
        if result is None:
            return None

        errors = result.get("errors")
        if errors:
            _LOGGER.warning(
                "API: GraphQL request rejected: %s",
                errors[0].get("message", errors) if errors else errors,
            )
            return None

        return result.get("data")

    async def get_device_offset(self, serial: str) -> float | None:
        """Return the device's stored temperature offset in °C, or None on failure."""
        url = f"{API_ENDPOINT_DEVICES}/{serial}/temperatureOffset"
        result = await self.api_call(
            f"devices/{serial}/temperatureOffset",
            full_url=url,
            schema=DEVICE_OFFSET,
        )
        if result is None:
            return None
        return result.get("celsius")

    async def get_flow_temperature_settings(self) -> dict[str, Any] | None:
        """Return Tado's stored flow temperature body (also for a refused 403), `{}` for an empty body, None on failure."""
        # WHY: a refused request here must come back as a body, never as the retry-then-reauth path.
        result = await self.api_call(
            "flowTemperatureOptimization",
            extra_success_statuses=frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.FORBIDDEN}),
        )
        _LOGGER.debug(
            "API: flowTemperatureOptimization read finished (%s)",
            "failed" if result is None else "empty" if not result else "body",
        )
        return result

    async def set_device_offset(self, serial: str, offset: float) -> bool:
        """Write `offset` to the device, rejecting out-of-range values without an API call."""
        if not is_valid_device_offset(offset):
            _LOGGER.warning(
                "API: refusing to write offset %s°C to device %s, "
                "outside Tado's valid range [%s, %s]°C",
                offset, mask_serial(serial), DEVICE_OFFSET_MIN, DEVICE_OFFSET_MAX,
            )
            return False
        url = f"{API_ENDPOINT_DEVICES}/{serial}/temperatureOffset"
        result = await self.api_call(
            f"devices/{serial}/temperatureOffset",
            method="PUT",
            data={"celsius": offset},
            full_url=url,
        )
        if result is not None:
            _LOGGER.debug(
                "API: wrote offset %s°C to device %s",
                offset, mask_serial(serial),
            )
            return True
        return False

    async def set_child_lock(self, serial: str, enabled: bool) -> bool:
        """Enable or disable the device's child lock."""
        url = f"{API_ENDPOINT_DEVICES}/{serial}/childLock"
        result = await self.api_call(
            f"devices/{serial}/childLock",
            method="PUT",
            data={"childLockEnabled": enabled},
            full_url=url,
        )
        if result is not None:
            state_str = "enabled" if enabled else "disabled"
            _LOGGER.info(
                "API: child lock %s on device %s",
                state_str, mask_serial(serial),
            )
            return True
        return False

    async def set_zone_overlay(
        self,
        zone_id: str,
        setting: dict[str, Any],
        termination: dict[str, Any],
        *,
        raise_network_errors: bool = False,
    ) -> bool:
        """Set a zone overlay (manual control); optionally raise when the outcome is unknown."""
        result = await self.api_call(
            f"zones/{zone_id}/overlay",
            method="PUT",
            data={"setting": setting, "termination": termination},
            raise_network_errors=raise_network_errors,
        )
        return result is not None

    async def delete_zone_overlay(self, zone_id: str) -> bool:
        """Delete the zone's overlay so the schedule resumes."""
        result = await self.api_call(
            f"zones/{zone_id}/overlay",
            method="DELETE",
        )
        return result is not None

    async def get_heating_circuits(self) -> list[dict[str, Any]] | None:
        """Return the home's boiler heating circuits.

        GET /homes/{id}/heatingCircuits. Each item carries a stable ``number``
        (identity / write key) and a ``driverShortSerialNo`` (display). Returns
        None when the fetch failed or the response was malformed, so the caller
        keeps its previous list; a home with no circuits returns an empty list.
        """
        result = await self.api_call("heatingCircuits", schema=HEATING_CIRCUITS)
        return result if isinstance(result, list) else None

    async def get_zone_control(self, zone_id: str) -> dict[str, Any] | None:
        """Return a zone's control block (carries ``heatingCircuit``), or None when the fetch failed.

        GET /zones/{id}/control.
        """
        result = await self.api_call(f"zones/{zone_id}/control", schema=ZONE_CONTROL)
        return result if isinstance(result, dict) else None

    async def set_zone_heating_circuit(self, zone_id: str, circuit_number: int | None) -> bool:
        """Reassign a zone to a boiler circuit (None unassigns it); True when Tado accepted the PUT."""
        result = await self.api_call(
            f"zones/{zone_id}/control/heatingCircuit",
            method="PUT",
            data={"circuitNumber": circuit_number},
        )
        return result is not None

    async def set_zone_active_timetable(self, zone_id: str, timetable_type: str) -> bool:
        """Switch a zone's active timetable; True only when Tado echoes the requested timetable."""
        timetable_id = ACTIVE_TIMETABLE_IDS.get(timetable_type)
        if timetable_id is None:
            raise ValueError(f"unknown timetable type {timetable_type!r}")
        result = await self.api_call(
            f"zones/{zone_id}/schedule/activeTimetable",
            method="PUT",
            data={"id": timetable_id},
            schema=ACTIVE_TIMETABLE,
        )
        if not isinstance(result, dict) or result.get("id") != timetable_id:
            return False
        return bool(result.get("type", timetable_type) == timetable_type)

    async def get_zone_schedule(self, zone_id: str) -> dict[str, Any] | None:
        """Return the zone's full schedule (timetable type + blocks per day)."""
        active = await self.api_call(
            f"zones/{zone_id}/schedule/activeTimetable",
            schema=ACTIVE_TIMETABLE,
        )
        if active is None:
            return None

        timetable_id = active.get("id", 0)
        timetable_type = active.get("type", "ONE_DAY")

        day_types_map = {
            "ONE_DAY": ["MONDAY_TO_SUNDAY"],
            "THREE_DAY": ["MONDAY_TO_FRIDAY", "SATURDAY", "SUNDAY"],
            "SEVEN_DAY": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"],
        }
        day_types = day_types_map.get(timetable_type, ["MONDAY_TO_SUNDAY"])

        # A failed or non-list GET is recorded in the sibling `failed_days`
        # list, not inside blocks_by_day: every consumer reads each day's
        # value as a plain list of blocks.
        blocks_by_day: dict[str, Any] = {}
        failed_days: list[str] = []
        for day_type in day_types:
            blocks = await self.api_call(
                f"zones/{zone_id}/schedule/timetables/{timetable_id}/blocks/{day_type}",
                schema=TIMETABLE_BLOCKS,
            )
            if isinstance(blocks, list):
                blocks_by_day[day_type] = blocks
            else:
                _LOGGER.debug(
                    "API: zone %s schedule fetch missed blocks for %s, "
                    "will retry on the next full sync whose day-type matches",
                    zone_id, day_type,
                )
                blocks_by_day[day_type] = []
                failed_days.append(day_type)

        return {
            "type": timetable_type,
            "timetable_id": timetable_id,
            "blocks": blocks_by_day,
            "failed_days": failed_days,
        }

    async def set_presence_lock(self, state: str) -> bool:
        """Set the home's presence lock to HOME or AWAY."""
        result = await self.api_call(
            "presenceLock",
            method="PUT",
            data={"homePresence": state},
        )
        if result is not None:
            _LOGGER.info("API: presence lock set to %s", state)
            return True
        return False

    async def delete_presence_lock(self) -> bool:
        """Delete the presence lock so geofencing resumes (Auto mode).

        422 means the presence lock didn't exist (already in Auto
        mode), so we treat that as success rather than an error.
        """
        result = await self.api_call(
            "presenceLock",
            method="DELETE",
            extra_success_statuses=frozenset({HTTPStatus.UNPROCESSABLE_ENTITY}),
        )
        if result is not None:
            _LOGGER.info(
                "API: presence lock deleted, geofencing resumed (Auto mode)",
            )
            return True
        return False


    async def _sync_and_save(
        self, endpoint: str, file_key: str, label: str, schema: ResponseSchema | None = None,
    ) -> Any:
        """Fetch an API endpoint and persist the response to its Store key."""
        data = await self.api_call(endpoint, schema=schema)
        if data:
            if self._data_loader is not None:
                await self._data_loader.async_update_store(file_key, data)
            _LOGGER.debug("API: %s saved", label)
        return data

    async def _sync_quick_extras(
        self,
        plan: FetchPlan,
        results: dict[str, ItemResult] | None = None,
    ) -> dict[str, ItemResult]:
        """Run the planned weather, home-state and frequent mobile-device fetches and report each."""
        results = {} if results is None else results
        if plan.has(KIND_WEATHER):
            weather = await self._sync_and_save("weather", "weather", "weather data", WEATHER)
            results[KIND_WEATHER] = ItemResult(KIND_WEATHER, weather is not None, 1)

        if plan.has(KIND_HOME_STATE):
            home_state = await self._sync_and_save("state", "home_state", "home state", HOME_STATE)
            results[KIND_HOME_STATE] = ItemResult(KIND_HOME_STATE, home_state is not None, 1)
            if home_state:
                _LOGGER.debug(
                    "API: home state presence=%s", home_state.get("presence"),
                )

        if plan.has(KIND_MOBILE_QUICK):
            mobile_data = await self._sync_and_save(
                "mobileDevices", "mobile_devices",
                "mobile devices (frequent sync)", MOBILE_DEVICES,
            )
            results[KIND_MOBILE_QUICK] = ItemResult(KIND_MOBILE_QUICK, mobile_data is not None, 1)
            if mobile_data:
                _LOGGER.debug(
                    "API: mobile-devices count %s", len(mobile_data),
                )
        return results

    async def _sync_full_extras(
        self,
        plan: FetchPlan,
        results: dict[str, ItemResult] | None = None,
    ) -> dict[str, ItemResult]:
        """Run the planned full-sync group in order and report a result for every planned item."""
        results = {} if results is None else results
        planned = [i.kind for i in plan.items if i.group == GROUP_FULL_SYNC]
        if not planned:
            return results

        if plan.has(KIND_ZONES_INFO):
            zones_info = await self._sync_and_save("zones", "zones_info", "zone info", ZONES_INFO)
            results[KIND_ZONES_INFO] = ItemResult(KIND_ZONES_INFO, isinstance(zones_info, list) and bool(zones_info), 1)
        else:
            # A partial retry has no zones_info item: its sub-items run from the stored zone list.
            zones_info = self._data_loader.get_cached("zones_info") if self._data_loader is not None else None
        if not (isinstance(zones_info, list) and zones_info):
            for kind in planned:
                results.setdefault(kind, ItemResult(kind, False))
            return results

        _LOGGER.debug("API: zone info covers %s zone(s)", len(zones_info))

        if plan.has(KIND_MOBILE_FULL):
            mobile_data = await self._sync_and_save(
                "mobileDevices", "mobile_devices", "mobile devices", MOBILE_DEVICES,
            )
            results[KIND_MOBILE_FULL] = ItemResult(KIND_MOBILE_FULL, mobile_data is not None, 1)
            if mobile_data:
                _LOGGER.debug(
                    "API: mobile-devices count %s", len(mobile_data),
                )

        if plan.has(KIND_DEVICES):
            # Home device list: carries each device's connectionState, incl. the
            # Internet Bridge's own online flag. The only cloud source for a
            # standalone bridge's state (it isn't in a heating zone).
            home_devices = await self._sync_and_save("devices", "home_devices", "home devices", HOME_DEVICES)
            results[KIND_DEVICES] = ItemResult(KIND_DEVICES, home_devices is not None, 1)
            if home_devices:
                _LOGGER.debug("API: home-devices count %s", len(home_devices))

        if plan.has(KIND_OFFSETS_FULL):
            offsets = await self._read_offsets(zones_info)
            # No serial zone means nothing to read, which is not a failure.
            results[KIND_OFFSETS_FULL] = ItemResult(KIND_OFFSETS_FULL, offsets.obtained > 0 or offsets.calls == 0, offsets.calls)

        if plan.has(KIND_CAPABILITIES):
            caps = await self._sync_zone_capabilities(zones_info)
            results[KIND_CAPABILITIES] = ItemResult(KIND_CAPABILITIES, caps.obtained == caps.calls, caps.calls)

        if plan.has(KIND_EARLY_START):
            early = await self._sync_early_start(zones_info)
            results[KIND_EARLY_START] = ItemResult(KIND_EARLY_START, early.obtained == early.calls, early.calls)

        circuits_planned = plan.has(KIND_HEATING_CIRCUITS)
        control_planned = plan.has(KIND_CIRCUIT_CONTROL)
        if circuits_planned or control_planned:
            listed, control = await self._sync_heating_circuits(
                zones_info, circuits=circuits_planned, control=control_planned,
            )
            if circuits_planned:
                results[KIND_HEATING_CIRCUITS] = ItemResult(KIND_HEATING_CIRCUITS, listed, 1)
            if control_planned:
                results[KIND_CIRCUIT_CONTROL] = ItemResult(
                    KIND_CIRCUIT_CONTROL, control.obtained == control.calls, control.calls,
                )

        svc = plan.item(KIND_SVC_SCHEDULES)
        if svc is not None:
            # Zones the live-quota gate or a healthy cache skipped count as returned, so only a failed fetch retries.
            schedules = await self._sync_svc_schedules(zones_info, svc.zone_ids)
            results[KIND_SVC_SCHEDULES] = ItemResult(KIND_SVC_SCHEDULES, schedules.obtained == schedules.calls)
        return results

    async def _sync_svc_schedules(
        self,
        zones_info: list[dict[str, Any]],
        svc_schedule_zone_ids: frozenset[str],
    ) -> FetchTally:
        """Fetch SVC zones' schedules even with Schedule Calendar off, gated per zone on the live quota; calls counts zones tried."""
        if self._data_loader is None:
            return FetchTally(0, 0)

        attempted = 0
        cached_schedules = self._data_loader.get_cached("schedules")
        if not isinstance(cached_schedules, dict):
            cached_schedules = {}

        levels = QuotaLevels(QuotaLevels.normalise_limit(self._rate_limit.get("limit")))

        fetched: dict[str, Any] = {}
        refusal: TadoAuthError | TadoRateLimitError | TadoBlockedError | None = None

        for zone_data in zones_info:
            zone_id = str(zone_data.get("id", ""))
            if zone_id not in svc_schedule_zone_ids:
                continue
            if zone_data.get("type") != "HEATING":
                continue

            existing = cached_schedules.get(zone_id)
            if isinstance(existing, dict) and existing.get("blocks"):
                today_types = _today_day_types(existing.get("type", "ONE_DAY"))
                if not any(schedule_day_failed(existing, dt) for dt in today_types):
                    continue  # a cache hit that isn't failing for today

            remaining = self._rate_limit.get("remaining")
            if levels.svc_zone_deferred(remaining):
                if remaining is None:
                    _LOGGER.debug(
                        "API: no rate-limit reading yet, deferring zone %s's "
                        "schedule fetch to a later cycle",
                        zone_id,
                    )
                else:
                    _LOGGER.debug(
                        "API: zone %s schedule fetch deferred, quota too "
                        "low (remaining=%s, defer at=%s)",
                        zone_id, remaining, levels.svc_defer_at,
                    )
                continue

            zone_name = zone_data.get("name", f"Zone {zone_id}")
            attempted += 1
            try:
                schedule_data = await self.get_zone_schedule(zone_id)
            except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as err:
                # The cloud is refusing us: stop here, save what was fetched, then let the poll handle the refusal.
                refusal = err
                break
            except Exception:
                # Broad on purpose: one zone's failure (rate limit, network
                # blip, auth hiccup) must not abort the loop for the other
                # SVC zones or anything already fetched earlier this cycle.
                _LOGGER.warning(
                    "API: SVC schedule fetch for %s failed, will retry "
                    "next full sync",
                    zone_name,
                    exc_info=True,
                )
                continue

            if not schedule_data:
                continue

            fetched[zone_id] = {
                "name": zone_name,
                "type": schedule_data.get("type", "ONE_DAY"),
                "blocks": schedule_data.get("blocks") or {},
                "failed_days": schedule_data.get("failed_days") or [],
            }

            if self._hass is not None:
                self._hass.bus.async_fire(
                    f"{DOMAIN}_schedule_updated",
                    {"zone_id": zone_id, "zone_name": zone_name},
                )

        removed = _unlisted_zone_ids(self._data_loader.get_cached("schedules"), zones_info)
        if fetched or removed:
            await self._data_loader.async_merge_store("schedules", fetched, removed)
        if refusal is not None:
            raise refusal
        return FetchTally(attempted, len(fetched), frozenset(fetched))

    async def _sync_heating_circuits(
        self, zones_info: list[dict[str, Any]], *, circuits: bool = True, control: bool = True,
    ) -> tuple[bool, FetchTally]:
        """Refresh the circuit list and each heating zone's circuit; return (list fetched, per-zone control tally)."""
        if self._data_loader is None:
            return False, FetchTally(0, 0)
        listed = False
        if circuits:
            circuit_list = await self.get_heating_circuits()
            listed = circuit_list is not None
            if circuit_list is not None:
                self._data_loader.update_cache("heating_circuits", {"circuits": circuit_list})
                await self._data_loader.async_update_store("heating_circuits", {"circuits": circuit_list})
        if not control:
            return listed, FetchTally(0, 0)

        heating_ids = [str(z.get("id")) for z in zones_info if z.get("type") == "HEATING"]
        fetched: dict[str, Any] = {}
        for zone_id in heating_ids:
            zone_control = await self.get_zone_control(zone_id)
            if zone_control is not None:
                fetched[zone_id] = {"heatingCircuit": zone_control.get("heatingCircuit")}
        live = self._data_loader.get_cached("heating_circuit_control")
        removed = [k for k in live if k not in heating_ids] if isinstance(live, dict) else []
        await self._data_loader.async_merge_store("heating_circuit_control", fetched, removed)
        return listed, FetchTally(len(heating_ids), len(fetched), frozenset(fetched))

    def _holds_back_empty_zone_reply(self, zones_data: dict[str, Any]) -> bool:
        """Return True for an empty zone reply that should be treated as a failed fetch."""
        key = getattr(self._config_entry, "entry_id", None) or self._home_id or "default"
        if zones_data.get("zoneStates"):
            _EMPTY_ZONE_REPLIES[key] = 0
            return False
        stored = self._data_loader.get_cached("zones") if self._data_loader else None
        stored_zones = stored.get("zoneStates") if isinstance(stored, dict) else None
        if not isinstance(stored_zones, dict) or not stored_zones:
            _EMPTY_ZONE_REPLIES[key] = 0
            return False
        _EMPTY_ZONE_REPLIES[key] = _EMPTY_ZONE_REPLIES.get(key, 0) + 1
        if _EMPTY_ZONE_REPLIES[key] < ZONE_STATES_EMPTY_CONFIRMATIONS:
            _LOGGER.warning(
                "API: Tado returned no zones, treating it as a failed fetch and keeping "
                "the previous data; if the next zone reply is also empty it will be used",
            )
            return True
        _LOGGER.info(
            "API: Tado reports no zones for this home; zone entities stay unavailable "
            "until a zone is added",
        )
        return False

    async def async_sync(self, plan: FetchPlan) -> dict[str, ItemResult]:
        """Execute the plan and report a result per item, raising typed errors for the coordinator."""
        sync_type = "full" if any(i.group for i in plan.items) else "quick"
        _LOGGER.debug("API: starting %s sync", sync_type)
        await self._ensure_home_id()
        results: dict[str, ItemResult] = {}

        try:
            if not plan.has(KIND_ZONE_STATES):
                _LOGGER.debug("API: skipping zone-states fetch, the plan has none")
            else:
                zones_data = await self.api_call("zoneStates", schema=ZONE_STATES)
                if zones_data is None:
                    _LOGGER.warning(
                        "API: zone-states fetch failed, sync abandoned, "
                        "coordinator will retry on next poll",
                    )
                    await self.save_ratelimit("error")
                    raise TadoSyncError("Failed to fetch zone states")

                if self._holds_back_empty_zone_reply(zones_data):
                    await self.save_ratelimit("error")
                    raise TadoSyncError("Tado returned no zones")

                await self._data_loader.async_update_store("zones", zones_data) if self._data_loader else None
                _LOGGER.debug(
                    "API: zone states saved for %s zone(s)",
                    len((zones_data.get("zoneStates") or {}).keys()),
                )
                results[KIND_ZONE_STATES] = ItemResult(KIND_ZONE_STATES, True, 1)

            if any(plan.has(k) for k in (KIND_WEATHER, KIND_HOME_STATE, KIND_MOBILE_QUICK)):
                await self._sync_quick_extras(plan, results)

            if any(i.group for i in plan.items):
                await self._sync_full_extras(plan, results)

            await self.save_ratelimit("ok")

            rl = self._rate_limit
            used = rl.get("limit", 0) - rl.get("remaining", 0) if rl.get("limit") else 0
            _LOGGER.debug(
                "API: %s sync complete: %s/%s daily calls used",
                sync_type, used, rl.get("limit", "?"),
            )

        except TadoAuthError:
            raise
        except TadoSyncError:
            raise
        except TadoRateLimitError:
            await self.save_ratelimit("error")
            raise
        except aiohttp.ClientError as e:
            _LOGGER.warning(
                "API: %s sync hit a network error, coordinator will "
                "retry on next poll",
                sync_type, exc_info=True,
            )
            await self.save_ratelimit("error")
            raise TadoSyncError(f"Network error during sync: {e}") from e
        return results

    async def async_resync_offsets(
        self, zones_info: list[Any], target_zone_id: str | None = None,
    ) -> FetchTally:
        """Re-read the offsets of one zone (the round-robin drift path) and tally the calls and offsets obtained."""
        return await self._read_offsets(zones_info, target_zone_id=target_zone_id)

    async def _read_offsets(
        self, zones_info: list[Any], target_zone_id: str | None = None,
    ) -> FetchTally:
        """Merge fresh device offsets (one zone or all) into the store and tally the reads."""
        offsets: dict[str, float] = {}
        calls_made = 0

        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_type = zone.get("type")

            if zone_type != "HEATING":
                continue

            if target_zone_id is not None and zone_id != target_zone_id:
                continue

            devices = zone.get("devices") or []
            for device in devices:
                serial = device.get("shortSerialNo")
                if serial:
                    calls_made += 1
                    offset = await self.get_device_offset(serial)
                    if offset is not None:
                        if not is_valid_device_offset(offset):
                            _LOGGER.warning(
                                "API: zone %s offset %s°C outside the "
                                "valid range [%s, %s], ignoring this "
                                "reading, cache unchanged",
                                zone_id, offset,
                                DEVICE_OFFSET_MIN, DEVICE_OFFSET_MAX,
                            )
                        else:
                            offsets[zone_id] = offset
                            _LOGGER.debug(
                                "API: zone %s offset cached at %s°C",
                                zone_id, offset,
                            )
                    break

        if self._data_loader is not None:
            removed = _unlisted_zone_ids(self._data_loader.get_cached("offsets"), zones_info)
            if offsets or removed:
                await self._data_loader.async_merge_store("offsets", offsets, removed)
        if offsets:
            _LOGGER.debug("API: offsets merged for %s zone(s)", len(offsets))

        return FetchTally(calls_made, len(offsets), frozenset(offsets))

    async def _sync_zone_capabilities(
        self, zones_info: list[Any], force_zone_ids: set[str] | None = None,
    ) -> FetchTally:
        """Cache AC and hot-water capabilities for zones not cached or forced, merging into the store; tally the fetches."""
        cached_raw = (
            self._data_loader.get_cached("zone_capabilities")
            if self._data_loader is not None else None
        )
        # A key snapshot, not the live dict: only membership at the start decides what to fetch.
        known = frozenset(cached_raw) if isinstance(cached_raw, dict) else frozenset()
        force = force_zone_ids or set()
        fetched: dict[str, Any] = {}
        attempted = 0

        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_type = zone.get("type")

            if zone_type not in CAPABILITY_ZONE_TYPES:
                continue

            if zone_id in known and zone_id not in force:
                continue

            attempted += 1
            caps = await self.api_call(f"zones/{zone_id}/capabilities", schema=ZONE_CAPABILITIES)
            if caps:
                fetched[zone_id] = caps
                modes = [m for m in ["COOL", "HEAT", "DRY", "FAN", "AUTO"] if m in caps]
                _LOGGER.debug(
                    "API: zone %s capabilities, modes=%s",
                    zone_id, modes,
                )

        if self._data_loader is not None:
            removed = _unlisted_zone_ids(self._data_loader.get_cached("zone_capabilities"), zones_info)
            if fetched or removed:
                merged = await self._data_loader.async_merge_store("zone_capabilities", fetched, removed)
                _LOGGER.debug(
                    "API: zone capabilities cached for %s zone(s)",
                    len(merged),
                )
        return FetchTally(attempted, len(fetched), frozenset(fetched))

    async def _sync_early_start(
        self, zones_info: list[Any], force_zone_ids: set[str] | None = None,
    ) -> FetchTally:
        """Cache each heating zone's Early Start setting for zones not cached or forced, merging into the store; tally the fetches."""
        cached_raw = (
            self._data_loader.get_cached("early_start")
            if self._data_loader is not None else None
        )
        # A snapshot, not the live dict: membership decides what to fetch and a value written during the loop beats the older reply.
        before = dict(cached_raw) if isinstance(cached_raw, dict) else {}
        force = force_zone_ids or set()
        fetched: dict[str, Any] = {}
        attempted = 0

        for zone in zones_info:
            zone_id = str(zone.get("id"))

            if zone.get("type") != "HEATING":
                continue

            if zone_id in before and zone_id not in force:
                continue

            attempted += 1
            reply = await self.api_call(f"zones/{zone_id}/earlyStart", schema=EARLY_START)
            if reply:
                fetched[zone_id] = {"enabled": reply["enabled"]}

        tally = FetchTally(attempted, len(fetched), frozenset(fetched))
        if self._data_loader is not None:
            current = self._data_loader.get_cached("early_start")
            live = current if isinstance(current, dict) else {}
            # The store changed during the loop (a toggle wrote through), so the newer write wins over this older reply.
            for zone_id in [z for z in fetched if live.get(z) != before.get(z)]:
                del fetched[zone_id]
            removed = _unlisted_zone_ids(current, zones_info)
            if fetched or removed:
                merged = await self._data_loader.async_merge_store("early_start", fetched, removed)
                _LOGGER.debug("API: early start cached for %s zone(s)", len(merged))
        return tally

    async def add_meter_reading(self, reading: int, date: str | None = None) -> bool:
        """Submit a meter reading via GraphQL (non-idempotent: no retry on failure).

        Tado's legacy REST `meterReadings` endpoint now rejects writes with
        a 403; this mirrors the mutation the app itself uses.
        """
        if not date:
            try:
                date = dt_util.now().strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date = dt_util.utcnow().strftime("%Y-%m-%d")

        home_id = await self._ensure_home_id()
        if not home_id:
            return False

        data = await self._graphql_call(
            "mutation SaveMeterReadings($homeId: ID!, $meterReadings: "
            "HeatingInsightsMeterReadingInput!) { addHeatingInsightsMeterReading("
            "homeId: $homeId, meterReading: $meterReadings) { success failureReason } }",
            {"homeId": str(home_id), "meterReadings": {"date": date, "reading": reading}},
        )
        result = (data or {}).get("addHeatingInsightsMeterReading") or {}
        if result.get("success"):
            _LOGGER.info(
                "API: meter reading %s saved for %s", reading, date,
            )
            return True

        _LOGGER.warning(
            "API: meter reading %s for %s rejected by Tado (%s)",
            reading, date, result.get("failureReason", "no reason given"),
        )
        return False

    async def identify_device(self, device_serial: str) -> bool:
        """Send an identify command via GraphQL (it flashes the device's LED).

        Supersedes the legacy REST identify endpoint. Addresses the device
        by a composite id, `{home_id}:PRE_LINE_X:{serial}` — empirically
        observed on TRVs, not documented by Tado; unverified for other
        device types.
        """
        home_id = await self._ensure_home_id()
        if not home_id:
            return False

        device_id = f"{home_id}:PRE_LINE_X:{device_serial}"
        data = await self._graphql_call(
            "mutation IdentifyDevice($deviceId: ID!) { identifyDevice(deviceId: $deviceId) "
            "{ success failureReason } }",
            {"deviceId": device_id},
        )
        result = (data or {}).get("identifyDevice") or {}
        if result.get("success"):
            _LOGGER.info(
                "API: identify command sent to device %s",
                mask_serial(device_serial),
            )
            return True

        _LOGGER.warning(
            "API: identify command for device %s rejected by Tado (%s)",
            mask_serial(device_serial), result.get("failureReason", "no reason given"),
        )
        return False

    async def set_away_configuration(
        self,
        zone_id: str,
        mode: str,
        temperature: float | None = None,
        comfort_level: int = 50,
    ) -> bool:
        """Set the zone's away configuration (mode = "auto" / "manual" / "off")."""
        if mode == "auto":
            payload: dict[str, Any] = {
                "type": "HEATING",
                "autoAdjust": True,
                "comfortLevel": comfort_level,
                "setting": {"type": "HEATING", "power": "OFF"},
            }
        elif mode == "manual" and temperature is not None:
            payload = {
                "type": "HEATING",
                "autoAdjust": False,
                "setting": {
                    "type": "HEATING",
                    "power": "ON",
                    "temperature": {"celsius": temperature},
                },
            }
        else:  # off
            payload = {
                "type": "HEATING",
                "autoAdjust": False,
                "setting": {"type": "HEATING", "power": "OFF"},
            }

        result = await self.api_call(
            f"zones/{zone_id}/schedule/awayConfiguration",
            method="PUT",
            data=payload,
        )
        if result is not None:
            _LOGGER.info(
                "API: zone %s away configuration set to %s", zone_id, mode,
            )
            return True
        return False


    async def activate_open_window(self, zone_id: str) -> bool:
        """POST `state/openWindow/activate` for one zone (non-idempotent: no retry)."""
        result = await self.api_call(
            f"zones/{zone_id}/state/openWindow/activate",
            method="POST",
        )
        return result is not None

    async def deactivate_open_window(self, zone_id: str) -> bool:
        """DELETE `state/openWindow` for one zone (idempotent: retries transient 403)."""
        result = await self.api_call(
            f"zones/{zone_id}/state/openWindow",
            method="DELETE",
        )
        return result is not None


def _unlisted_zone_ids(live: object, zones_info: list[Any]) -> list[str]:
    """Return the cached zone ids absent from this sync's zone list; none on an empty list."""
    listed = frozenset(str(zone.get("id")) for zone in zones_info)
    return unlisted_zone_keys(live, listed) if listed and isinstance(live, dict) else []


def _today_day_types(timetable_type: str) -> list[str]:
    """Return the day_type keys whose blocks could cover today, for this timetable."""
    weekday = dt_util.now().weekday()  # Monday=0 .. Sunday=6
    if timetable_type == "SEVEN_DAY":
        return [DAY_TYPES["SEVEN_DAY"][weekday]]
    if timetable_type == "THREE_DAY":
        if weekday < 5:
            return [DAY_TYPES["THREE_DAY"][0]]
        return [DAY_TYPES["THREE_DAY"][1]] if weekday == 5 else [DAY_TYPES["THREE_DAY"][2]]
    return [DAY_TYPES["ONE_DAY"][0]]


def schedule_day_failed(schedule_entry: dict[str, Any], day_type: str) -> bool:
    """True when `day_type`'s GET failed on the schedule's last fetch (`get_zone_schedule`)."""
    failed_days = schedule_entry.get("failed_days")
    return isinstance(failed_days, list) and day_type in failed_days
