"""Tado CE API client: async HTTP with rate-limit accounting and per-entry isolation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from http import HTTPStatus
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .api_auth import TadoAuthMixin
from .api_call_tracker import (
    CALL_TYPE_CAPABILITIES,
    CALL_TYPE_HOME_STATE,
    CALL_TYPE_MOBILE_DEVICES,
    CALL_TYPE_OVERLAY,
    CALL_TYPE_PRESENCE_LOCK,
    CALL_TYPE_WEATHER,
    CALL_TYPE_ZONE_STATES,
    CALL_TYPE_ZONES,
)
from .const import (
    API_ENDPOINT_DEVICES,
    DAY_TYPES,
    DEVICE_OFFSET_MAX,
    DEVICE_OFFSET_MIN,
    DOMAIN,
    MAX_RETRY_ATTEMPTS,
    MAX_SCHEDULE_FETCH_CALLS,
    QUOTA_WARNING_PERCENTAGE,
    TADO_API_BASE,
    is_climate_zone,
    is_valid_device_offset,
)
from .exceptions import TadoAuthError, TadoRateLimitError, TadoSyncError
from .helpers import hard_quota_reserve, mask_serial, parse_iso_datetime, retry_delay

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api_call_tracker import APICallTracker
    from .config_manager import ConfigurationManager
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)

# HTTP timeout for Tado Cloud API calls (30s covers slow responses; normal < 5s)
_API_CALL_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Methods safe to retry on transient 403 (idempotent)
_RETRYABLE_METHODS = frozenset({"GET", "PUT", "DELETE"})


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


def _detect_call_type(endpoint: str) -> int | None:
    """Map an endpoint string to the matching `CALL_TYPE_*` constant."""
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


class TadoApiClient(TadoAuthMixin):
    """Async Tado cloud API client scoped to one config entry."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        hass: HomeAssistant | None = None,
        home_id: str | None = None,
        refresh_token: str | None = None,
        config_manager: ConfigurationManager | None = None,
        api_tracker: APICallTracker | None = None,
        data_loader: DataLoader | None = None,
        config_entry: ConfigEntry | None = None,
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
        self._config_manager = config_manager
        self._api_tracker = api_tracker
        self._data_loader = data_loader
        self._config_entry = config_entry

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
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower == "ratelimit-policy":
                policy = value
            elif key_lower == "ratelimit":
                ratelimit = value

        _LOGGER.debug(
            "API: rate-limit headers, policy=%s, ratelimit=%s",
            policy, ratelimit,
        )

        if "q=" in policy:
            with suppress(ValueError, IndexError):
                self._rate_limit["limit"] = int(policy.split("q=")[1].split(";")[0])

        if "r=" in ratelimit:
            with suppress(ValueError, IndexError):
                self._rate_limit["remaining"] = int(ratelimit.split("r=")[1].split(";")[0])

        if not self._rate_limit.get("_from_429"):
            self._rate_limit.pop("reset_seconds", None)
        else:
            del self._rate_limit["_from_429"]

        _LOGGER.debug("API: parsed rate limit: %s", self._rate_limit)

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

        # When `remaining` jumps up by more than 5% of the daily
        # limit (or 20 calls, whichever is bigger), Tado's quota has
        # reset since the last poll. Record the moment so the
        # extrapolation strategies can lean on it.
        if prev_remaining is not None and remaining is not None:
            reset_threshold = max(20, int(limit * 0.05))
            if remaining > prev_remaining + reset_threshold:
                last_reset_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                _LOGGER.info(
                    "API: Tado quota reset detected at %s "
                    "(remaining %s → %s, threshold %s)",
                    last_reset_utc, prev_remaining, remaining, reset_threshold,
                )

        return {
            "limit": limit,
            "used": used,
            "remaining": remaining,
            "percentage_used": percentage_used,
            "last_reset_utc": last_reset_utc,
        }

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
    def _round_to_nearest_hour(dt_val: datetime) -> int:
        """Round a datetime to the nearest hour (0-23)."""
        hour = dt_val.hour
        if dt_val.minute >= 30:
            hour = (hour + 1) % 24
        return hour

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
            all_calls = tracker.get_call_history(days=14)
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

        real_limit = self._rate_limit.get("limit", 5000)
        real_remaining = self._rate_limit.get("remaining", 5000)
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
            "API: HTTP 403 after %s retry attempts on %s %s, credentials "
            "likely revoked, surfacing to coordinator for reauth",
            MAX_RETRY_ATTEMPTS, method, endpoint,
        )
        # WHY: persistent 403 after retries means the transient-WAF-block
        # hypothesis is exhausted. Raise TadoAuthError so the coordinator
        # dispatches reauth instead of falling through to cache fallback.
        raise TadoAuthError(
            f"Persistent 403 after {MAX_RETRY_ATTEMPTS} retries on {method} {endpoint}",
        )

    async def _resolve_api_url(self, endpoint: str, full_url: str | None) -> str | None:
        """Resolve the absolute URL: prefer `full_url` when given, else build from home ID."""
        if full_url:
            return full_url
        config = await self._load_config()
        home_id = config.get("home_id")
        if not home_id:
            raise TadoAuthError(
                f"Home ID missing, cannot resolve {endpoint}, re-authenticate to fix",
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
                response_body[:200] if response_body else "no response body",
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
                retry_after=retry_after_seconds,
            )

        if status >= HTTPStatus.INTERNAL_SERVER_ERROR and is_safe_to_retry:
            if attempt < MAX_RETRY_ATTEMPTS:
                delay = retry_delay(attempt)
                _LOGGER.warning(
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
                method, endpoint, status, response_body[:200],
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
    ) -> tuple[dict[str, Any] | None, bool]:
        """Execute a single API attempt. Returns (result, should_continue)."""
        async with self._session.request(
            method, url,
            headers={"Authorization": f"Bearer {token}"},
            json=data if method in ("PUT", "POST") else None,
            timeout=_API_CALL_TIMEOUT,
        ) as resp:
            if parse_ratelimit and attempt == 1:
                self._parse_ratelimit_headers(dict(resp.headers))
            if tracker and call_type:
                await tracker.async_record_call(call_type, resp.status)

            if resp.status in success_statuses:
                if resp.status == HTTPStatus.NO_CONTENT or resp.content_length == 0:
                    return {}, False
                return await resp.json(), False

            try:
                response_body = await resp.text()
            except Exception:
                # Body read is diagnostic-only; the outer error path logs
                # the request status, so an empty body is acceptable.
                response_body = ""

            if resp.status == HTTPStatus.TOO_MANY_REQUESTS:
                retry_after_header = resp.headers.get("Retry-After")
                if retry_after_header:
                    try:
                        self._rate_limit["reset_seconds"] = int(float(retry_after_header))
                        self._rate_limit["_from_429"] = True
                    except (ValueError, TypeError):
                        pass

            action = await self._handle_error_status(
                resp.status, method, url, attempt,
                method in _RETRYABLE_METHODS,
                response_body=response_body,
            )
            if action == "return_success":
                return {}, False
            return None, action == "continue"

    async def api_call(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        parse_ratelimit: bool = True,
        full_url: str | None = None,
        extra_success_statuses: frozenset[int] | None = None,
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

        call_type = _detect_call_type(endpoint)
        tracker = self._api_tracker

        _success: set[int | HTTPStatus] = {HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.NO_CONTENT}
        if extra_success_statuses:
            _success |= extra_success_statuses

        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            token = await self.get_access_token()
            if not token:
                _LOGGER.warning(
                    "API: could not get access token for %s %s, "
                    "re-authentication may be required",
                    method, endpoint,
                )
                return None

            try:
                result, should_continue = await self._execute_single_api_attempt(
                    method, url, token, data, _success,
                    parse_ratelimit, attempt, tracker, call_type,
                )
                if should_continue:
                    continue
                return result
            except (TimeoutError, aiohttp.ClientError):
                if not await self._should_retry_network_error(attempt, "network error", method, endpoint):
                    return None
            except TadoAuthError:
                raise
            except TadoRateLimitError:
                # Quota exhaustion must reach the coordinator dispatch
                # (repair issue + retry-after backoff) and the background
                # write dispatcher. Never collapse to a None return.
                raise
            except TadoSyncError:
                # Transient sync errors are the coordinator's to classify;
                # swallowing here would mislabel them as a generic failure.
                raise
            except Exception:
                _LOGGER.warning(
                    "API: unexpected error on %s %s, call abandoned, "
                    "next poll will retry",
                    method, endpoint,
                    exc_info=True,
                )
                return None

        return None

    async def get_device_offset(self, serial: str) -> float | None:
        """Return the device's stored temperature offset in °C, or None on failure."""
        url = f"{API_ENDPOINT_DEVICES}/{serial}/temperatureOffset"
        result = await self.api_call(
            f"devices/{serial}/temperatureOffset",
            full_url=url,
        )
        if result is None:
            return None
        return result.get("celsius")

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

    async def set_zone_overlay(self, zone_id: str, setting: dict[str, Any], termination: dict[str, Any]) -> bool:
        """Set a zone overlay (manual control)."""
        result = await self.api_call(
            f"zones/{zone_id}/overlay",
            method="PUT",
            data={"setting": setting, "termination": termination},
        )
        return result is not None

    async def delete_zone_overlay(self, zone_id: str) -> bool:
        """Delete the zone's overlay so the schedule resumes."""
        result = await self.api_call(
            f"zones/{zone_id}/overlay",
            method="DELETE",
        )
        return result is not None

    async def get_heating_circuits(self) -> list[dict[str, Any]]:
        """Return the home's boiler heating circuits.

        GET /homes/{id}/heatingCircuits. Each item carries a stable ``number``
        (identity / write key) and a ``driverShortSerialNo`` (display). Returns
        an empty list when unavailable so single- or no-circuit homes degrade cleanly.
        """
        result = await self.api_call("heatingCircuits")
        return result if isinstance(result, list) else []

    async def get_zone_control(self, zone_id: str) -> dict[str, Any]:
        """Return a zone's control block (carries ``heatingCircuit``).

        GET /zones/{id}/control. Returns an empty dict when unavailable.
        """
        result = await self.api_call(f"zones/{zone_id}/control")
        return result if isinstance(result, dict) else {}

    async def set_zone_heating_circuit(self, zone_id: str, circuit_number: int | None) -> None:
        """Reassign a zone to a boiler circuit, or unassign it.

        PUT /zones/{id}/control/heatingCircuit with ``{"circuitNumber": n}``;
        ``None`` unassigns the zone ("No heating circuit"), so its valves still
        open for residual heat but the room never fires the boiler itself.
        """
        await self.api_call(
            f"zones/{zone_id}/control/heatingCircuit",
            method="PUT",
            data={"circuitNumber": circuit_number},
        )

    async def get_zone_default_overlay(self, zone_id: str) -> str | None:
        """Return the zone's server-side default termination type, or None.

        Reads the per-zone default the user set in the Tado app
        (GET /zones/{id}/defaultOverlay). Returns MANUAL / TADO_MODE / TIMER,
        or None when unavailable so callers fall back to the integration default.
        """
        result = await self.api_call(f"zones/{zone_id}/defaultOverlay")
        if not isinstance(result, dict):
            return None
        term = result.get("terminationCondition")
        if isinstance(term, dict):
            value = term.get("type")
            if value in ("MANUAL", "TADO_MODE", "TIMER"):
                return value  # type: ignore[no-any-return]
        return None

    async def get_zone_schedule(self, zone_id: str) -> dict[str, Any] | None:
        """Return the zone's full schedule (timetable type + blocks per day)."""
        active = await self.api_call(
            f"zones/{zone_id}/schedule/activeTimetable",
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
            )
            if isinstance(blocks, list):
                blocks_by_day[day_type] = blocks
            else:
                _LOGGER.warning(
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


    async def _sync_and_save(self, endpoint: str, file_key: str, label: str) -> Any:
        """Fetch an API endpoint and persist the response to its Store key."""
        data = await self.api_call(endpoint)
        if data:
            if self._data_loader is not None:
                await self._data_loader.async_update_store(file_key, data)
            _LOGGER.debug("API: %s saved", label)
        return data

    async def _sync_quick_extras(
        self,
        *,
        weather_enabled: bool,
        home_state_sync_enabled: bool,
        mobile_devices_enabled: bool,
        mobile_devices_frequent_sync: bool,
    ) -> None:
        """Run the optional fetches that ride along with a quick sync."""
        if weather_enabled:
            await self._sync_and_save("weather", "weather", "weather data")

        if home_state_sync_enabled:
            home_state = await self._sync_and_save("state", "home_state", "home state")
            if home_state:
                _LOGGER.debug(
                    "API: home state presence=%s", home_state.get("presence"),
                )

        if mobile_devices_enabled and mobile_devices_frequent_sync:
            mobile_data = await self._sync_and_save(
                "mobileDevices", "mobile_devices",
                "mobile devices (frequent sync)",
            )
            if mobile_data:
                _LOGGER.debug(
                    "API: mobile-devices count %s", len(mobile_data),
                )

    async def _sync_full_extras(
        self,
        *,
        mobile_devices_enabled: bool,
        offset_enabled: bool,
        svc_schedule_zone_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Run the heavier fetches (zone info, mobile, offsets, AC caps) in a full sync."""
        zones_info = await self._sync_and_save("zones", "zones_info", "zone info")
        if not zones_info:
            return

        _LOGGER.debug("API: zone info covers %s zone(s)", len(zones_info))

        if mobile_devices_enabled:
            mobile_data = await self._sync_and_save(
                "mobileDevices", "mobile_devices", "mobile devices",
            )
            if mobile_data:
                _LOGGER.debug(
                    "API: mobile-devices count %s", len(mobile_data),
                )

        # Home device list: carries each device's connectionState, incl. the
        # Internet Bridge's own online flag. The only cloud source for a
        # standalone bridge's state (it isn't in a heating zone).
        home_devices = await self._sync_and_save("devices", "home_devices", "home devices")
        if home_devices:
            _LOGGER.debug("API: home-devices count %s", len(home_devices))

        if offset_enabled:
            await self._sync_offsets(zones_info)

        await self._sync_ac_capabilities(zones_info)

        if (
            self._config_manager is not None
            and self._config_manager.get_heating_circuit_enabled()
        ):
            await self._sync_heating_circuits(zones_info)

        if svc_schedule_zone_ids:
            await self._sync_svc_schedules(zones_info, svc_schedule_zone_ids)

    async def _sync_svc_schedules(
        self,
        zones_info: list[dict[str, Any]],
        svc_schedule_zone_ids: frozenset[str],
    ) -> None:
        """Fetch a Smart Valve Control zone's schedule on the full-sync cadence.

        Runs even when Schedule Calendar is off - SVC's valve_target mode
        needs a current schedule to know what to hold, independent of the
        display feature that historically was the only thing fetching one.
        Gated on the LIVE remaining-quota value (updated by every response
        this whole sync has made so far), re-checked before each zone, so
        the gate tightens as this cycle spends calls rather than working
        from a stale once-per-cycle snapshot.
        """
        if self._data_loader is None:
            return

        cached_schedules = self._data_loader.get_cached("schedules")
        if not isinstance(cached_schedules, dict):
            cached_schedules = {}

        limit = self._rate_limit.get("limit")
        reserve = hard_quota_reserve(limit)

        merged = dict(cached_schedules)
        any_fetched = False

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
            if remaining is None:
                _LOGGER.warning(
                    "API: no rate-limit reading yet, deferring zone %s's "
                    "schedule fetch to a later cycle",
                    zone_id,
                )
                continue
            if remaining <= reserve + MAX_SCHEDULE_FETCH_CALLS:
                _LOGGER.debug(
                    "API: zone %s schedule fetch deferred, quota too "
                    "low (remaining=%s, reserve=%s)",
                    zone_id, remaining, reserve,
                )
                continue

            zone_name = zone_data.get("name", f"Zone {zone_id}")
            try:
                schedule_data = await self.get_zone_schedule(zone_id)
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

            merged[zone_id] = {
                "name": zone_name,
                "type": schedule_data.get("type", "ONE_DAY"),
                "blocks": schedule_data.get("blocks") or {},
                "failed_days": schedule_data.get("failed_days") or [],
            }
            any_fetched = True

            if self._hass is not None:
                self._hass.bus.async_fire(
                    f"{DOMAIN}_schedule_updated",
                    {"zone_id": zone_id, "zone_name": zone_name},
                )

        if any_fetched:
            await self._data_loader.async_update_store("schedules", merged)

    async def _sync_heating_circuits(self, zones_info: list[dict[str, Any]]) -> None:
        """Fetch the home's circuit list + each zone's current circuit (full-sync only).

        Rides the full-sync cadence, with no separate timer, because the
        circuit list rarely changes; per-zone control is re-read here so a
        Tado-app reassignment surfaces within one full-sync interval. Stores are
        overwrite-on-fetch, so a dropped circuit / zone self-evicts.
        """
        if self._data_loader is None:
            return
        circuits = await self.get_heating_circuits()
        self._data_loader.update_cache("heating_circuits", {"circuits": circuits})
        await self._data_loader.async_update_store("heating_circuits", {"circuits": circuits})

        control: dict[str, Any] = {}
        for zone in zones_info:
            if zone.get("type") != "HEATING":
                continue
            zone_id = str(zone.get("id"))
            zone_control = await self.get_zone_control(zone_id)
            control[zone_id] = {"heatingCircuit": zone_control.get("heatingCircuit")}
        self._data_loader.update_cache("heating_circuit_control", control)
        await self._data_loader.async_update_store("heating_circuit_control", control)

    async def async_sync(
        self,
        quick: bool = False,
        skip_zone_states: bool = False,
        zone_only: bool = False,
        weather_enabled: bool = True,
        mobile_devices_enabled: bool = True,
        mobile_devices_frequent_sync: bool = False,
        offset_enabled: bool = False,
        home_state_sync_enabled: bool = False,
        svc_schedule_zone_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Run one cycle of cloud data fetches, raising typed errors for the coordinator."""
        sync_type = "quick" if quick else "full"
        _LOGGER.debug("API: starting %s sync", sync_type)
        await self._ensure_home_id()

        try:
            if skip_zone_states:
                _LOGGER.debug(
                    "API: skipping zone-states fetch. HomeKit is providing "
                    "live data",
                )
            else:
                zones_data = await self.api_call("zoneStates")
                if zones_data is None:
                    _LOGGER.warning(
                        "API: zone-states fetch failed, sync abandoned, "
                        "coordinator will retry on next poll",
                    )
                    await self.save_ratelimit("error")
                    raise TadoSyncError("Failed to fetch zone states")

                await self._data_loader.async_update_store("zones", zones_data) if self._data_loader else None
                _LOGGER.debug(
                    "API: zone states saved for %s zone(s)",
                    len((zones_data.get("zoneStates") or {}).keys()),
                )

            if not zone_only:
                await self._sync_quick_extras(
                    weather_enabled=weather_enabled,
                    home_state_sync_enabled=home_state_sync_enabled,
                    mobile_devices_enabled=mobile_devices_enabled,
                    mobile_devices_frequent_sync=mobile_devices_frequent_sync,
                )

            if not quick and not zone_only:
                await self._sync_full_extras(
                    mobile_devices_enabled=mobile_devices_enabled,
                    offset_enabled=offset_enabled,
                    svc_schedule_zone_ids=svc_schedule_zone_ids,
                )

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

    async def async_resync_offsets(
        self, zones_info: list[Any], target_zone_id: str | None = None,
    ) -> int:
        """Public wrapper around `_sync_offsets` for the coordinator's drift-refresh path.

        Called on `OFFSET_DRIFT_REFRESH_SECONDS` cadence so the cached
        offsets stay close to Tado's stored values even when Tado's
        adaptive calibration walks them behind our back. `target_zone_id`
        scopes the refresh to one zone (the round-robin path).
        """
        return await self._sync_offsets(zones_info, target_zone_id=target_zone_id)

    async def _sync_offsets(
        self, zones_info: list[Any], target_zone_id: str | None = None,
    ) -> int:
        """Refresh the device-offset cache for heating / AC zones.

        When `target_zone_id` is given, only that zone is fetched (the
        round-robin path); otherwise every climate zone is fetched (boot /
        full-sync). The result is MERGED into the existing offsets store, not
        overwritten, so a single-zone refresh never wipes the other zones.
        """
        offsets: dict[str, float] = {}
        calls_made = 0

        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_type = zone.get("type")

            if not is_climate_zone(zone_type or ""):
                continue

            if target_zone_id is not None and zone_id != target_zone_id:
                continue

            devices = zone.get("devices") or []
            for device in devices:
                serial = device.get("shortSerialNo")
                if serial:
                    try:
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
                    except (KeyError, TypeError, ValueError) as e:
                        _LOGGER.warning(
                            "API: could not fetch offset for device %s "
                            "(%s), zone %s will keep its previous cached "
                            "value",
                            mask_serial(serial), e, zone_id,
                        )

        if offsets:
            if self._data_loader is not None:
                existing = self._data_loader.get_cached("offsets")
                merged: dict[str, float] = (
                    dict(existing) if isinstance(existing, dict) else {}
                )
                merged.update(offsets)
                await self._data_loader.async_update_store("offsets", merged)
            _LOGGER.debug("API: offsets merged for %s zone(s)", len(offsets))

        return calls_made

    async def _sync_ac_capabilities(
        self, zones_info: list[Any], force_zone_ids: set[str] | None = None,
    ) -> None:
        """Cache per-zone AC + hot-water capabilities, fetching only zones not cached or forced.

        Per-zone (not whole-store) skip: a zone absent from the cache is fetched
        (new zone), a zone in `force_zone_ids` is re-fetched (re-pair / hardware
        swap), and an already-cached zone is left alone. The result is merged
        into the existing store so a single-zone refresh never wipes the others.
        Covers AIR_CONDITIONING (mode / fan / swing / temperature discovery) and
        HOT_WATER (`canSetTemperature`: a temperature-capable DHW zone's ON
        overlay must carry `temperature`).
        """
        cached_raw = (
            self._data_loader.get_cached("ac_capabilities")
            if self._data_loader is not None else None
        )
        merged: dict[str, Any] = dict(cached_raw) if isinstance(cached_raw, dict) else {}
        force = force_zone_ids or set()
        fetched_any = False

        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_type = zone.get("type")

            if zone_type not in ("AIR_CONDITIONING", "HOT_WATER"):
                continue

            if zone_id in merged and zone_id not in force:
                continue

            try:
                caps = await self.api_call(f"zones/{zone_id}/capabilities")
                if caps:
                    merged[zone_id] = caps
                    fetched_any = True
                    modes = [m for m in ["COOL", "HEAT", "DRY", "FAN", "AUTO"] if m in caps]
                    _LOGGER.debug(
                        "API: zone %s AC capabilities, modes=%s",
                        zone_id, modes,
                    )
            except (KeyError, TypeError, ValueError) as e:
                _LOGGER.warning(
                    "API: could not fetch AC capabilities for zone %s "
                    "(%s), zone will keep its previous cached value",
                    zone_id, e,
                )

        if fetched_any and self._data_loader is not None:
            await self._data_loader.async_update_store("ac_capabilities", merged)
            _LOGGER.debug(
                "API: AC capabilities cached for %s zone(s)",
                len(merged),
            )

    async def add_meter_reading(self, reading: int, date: str | None = None) -> bool:
        """POST a meter reading to Tado (non-idempotent: no retry on failure)."""
        if not date:
            try:
                date = dt_util.now().strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date = dt_util.utcnow().strftime("%Y-%m-%d")

        result = await self.api_call(
            "meterReadings",
            method="POST",
            data={"date": date, "reading": reading},
        )
        if result is not None:
            _LOGGER.info(
                "API: meter reading %s saved for %s", reading, date,
            )
            return True
        return False

    async def identify_device(self, device_serial: str) -> bool:
        """POST an identify command to a device (it flashes its LED)."""
        url = f"{API_ENDPOINT_DEVICES}/{device_serial}/identify"
        result = await self.api_call(
            f"devices/{device_serial}/identify",
            method="POST",
            full_url=url,
        )
        if result is not None:
            _LOGGER.info(
                "API: identify command sent to device %s",
                mask_serial(device_serial),
            )
            return True
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
