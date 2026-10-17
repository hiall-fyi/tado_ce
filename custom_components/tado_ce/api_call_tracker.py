"""Tado CE API call tracker: record every cloud call for quota and reset prediction.

Persists per-day call history through HA Store, derives a calls-per-
hour rate from recent history (or falls back to a config-based
estimate), and uses that rate to extrapolate when the daily quota
last reset. The coordinator combines this estimate with the live
`X-Quota-Remaining` header to keep the polling interval honest even
when the cloud doesn't tell us the exact reset time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from threading import Lock
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .helpers import parse_iso_datetime
from .storage import async_migrate_json_to_store

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.core import HomeAssistant

    from .config_manager import ConfigurationManager

_LOGGER = logging.getLogger(__name__)

# Call type codes
CALL_TYPE_ZONE_STATES = 1
CALL_TYPE_WEATHER = 2
CALL_TYPE_ZONES = 3
CALL_TYPE_MOBILE_DEVICES = 4
CALL_TYPE_OVERLAY = 5
CALL_TYPE_PRESENCE_LOCK = 6
CALL_TYPE_HOME_STATE = 7
CALL_TYPE_CAPABILITIES = 8

CALL_TYPE_NAMES = {
    CALL_TYPE_ZONE_STATES: "zoneStates",
    CALL_TYPE_WEATHER: "weather",
    CALL_TYPE_ZONES: "zones",
    CALL_TYPE_MOBILE_DEVICES: "mobileDevices",
    CALL_TYPE_OVERLAY: "overlay",
    CALL_TYPE_PRESENCE_LOCK: "presenceLock",
    CALL_TYPE_HOME_STATE: "homeState",
    CALL_TYPE_CAPABILITIES: "capabilities",
}

# Rate extrapolation thresholds
_MIN_CALLS_FOR_RATE = 20
_MAX_REASONABLE_RATE = 100
_HOURS_IN_DAY = 24
_DEFAULT_CALLS_PER_HOUR = 15
_MIN_TIME_SPAN_HOURS = 1.0


class APICallTracker:
    """Per-home record of every API call, persisted via HA Store.

    Async methods do their I/O off the event loop. Sync methods stay
    available for non-async callers (mostly tests). `data_dir` is
    only used to locate the legacy JSON path for one-time migration
    into HA Store.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        data_dir: Path,
        retention_days: int = 14,
        home_id: str | None = None,
        config_manager: ConfigurationManager | None = None,
    ) -> None:
        """Initialise the tracker for one home, with an optional retention cap."""
        self._hass = hass
        self.data_dir = data_dir
        self.retention_days = retention_days
        self.home_id = home_id
        self._config_manager = config_manager

        self._store: Store[dict[str, Any]] = Store(
            hass,
            1,
            f"tado_ce/api_call_tracker_{home_id or 'default'}",
        )

        from .const import get_data_file  # avoid circular import

        self._old_json_path = get_data_file("api_call_history", home_id)

        self._lock = Lock()
        self._async_lock = asyncio.Lock()
        self._call_history: dict[str, list[dict[str, Any]]] = {}
        self._last_cleanup_date = None
        self._initialized = False
        self._dirty = False

    async def _load_history_async(self) -> dict[str, Any]:
        """Load call history from HA Store, falling back to the legacy JSON file."""
        try:
            data = await self._store.async_load()
            if data is not None:
                return data

            migrated = await async_migrate_json_to_store(
                self._hass, self._old_json_path, self._store,
                label="api_call_tracker",
            )
            if migrated is not None and isinstance(migrated, dict):
                return migrated
        except (OSError, HomeAssistantError):
            _LOGGER.warning(
                "API Tracker: could not load call history, quota "
                "extrapolation will rely on config-based rate until the "
                "next save succeeds",
                exc_info=True,
            )
        return {}

    async def _save_history_async(self, data: dict[str, Any]) -> None:
        """Persist call history to HA Store, serialised through an asyncio lock."""
        async with self._async_lock:
            try:
                await self._store.async_save(data)
            except (OSError, TypeError):
                _LOGGER.warning(
                    "API Tracker: could not save call history, data is "
                    "kept in memory and will retry on the next poll",
                    exc_info=True,
                )

    async def async_init(self) -> None:
        """Load history from storage and run a first cleanup pass.

        Cleanup runs outside the load lock because
        `async_cleanup_old_records` calls `_save_history_async`, which
        re-acquires the same non-reentrant lock, and calling it from
        inside the lock would deadlock.
        """
        if self._initialized:
            return

        async with self._async_lock:
            if self._initialized:
                return

            self._call_history = await self._load_history_async()
            self._initialized = True
            _LOGGER.debug(
                "API Tracker: loaded call history covering %d day(s)",
                len(self._call_history),
            )

        await self.async_cleanup_old_records()
        self._last_cleanup_date = dt_util.utcnow().date()  # type: ignore[assignment]

    @property
    def needs_save(self) -> bool:
        """Return True when there are unsaved call records."""
        return self._dirty

    async def async_save_if_dirty(self) -> None:
        """Persist the dirty buffer to storage (called from poll cycle and unload)."""
        if not self._dirty:
            return
        await self._save_history_async(dict(self._call_history))
        self._dirty = False

    async def async_record_call(self, call_type: int, status_code: int, timestamp: datetime | None = None) -> None:
        """Record one API call, deferring the disk write to the next save tick."""
        if not self._initialized:
            await self.async_init()

        if timestamp is None:
            timestamp = dt_util.utcnow()
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        date_key = timestamp.strftime("%Y-%m-%d")
        today = timestamp.date()
        should_cleanup = False

        call_record = {
            "type": call_type,
            "type_name": CALL_TYPE_NAMES.get(call_type, "unknown"),
            "status": status_code,
            "timestamp": timestamp.isoformat(),
        }

        with self._lock:
            if date_key not in self._call_history:
                self._call_history[date_key] = []
            self._call_history[date_key].append(call_record)

            if self._last_cleanup_date is None or self._last_cleanup_date < today:
                self._last_cleanup_date = today  # type: ignore[assignment]
                should_cleanup = True

        # Mark dirty: actual save happens in coordinator poll cycle or unload
        self._dirty = True

        if should_cleanup:
            await self.async_cleanup_old_records()

        _LOGGER.debug(
            "API Tracker: recorded %s call (status %s)",
            CALL_TYPE_NAMES.get(call_type),
            status_code,
        )


    def get_call_history(self, days: int = 1) -> list[dict[str, Any]]:
        """Return API calls from the last `days` days, newest first.

        Returns an empty list when the tracker hasn't run `async_init`
        yet: synchronous callers can't trigger disk I/O without
        blocking the event loop.
        """
        if not self._initialized:
            _LOGGER.debug(
                "API Tracker: get_call_history called before async_init "
                "completed, returning empty history",
            )
            return []

        cutoff_date = (dt_util.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        calls = []

        with self._lock:
            for date_key, date_calls in self._call_history.items():
                if date_key >= cutoff_date:
                    calls.extend(date_calls)

        calls.sort(key=lambda x: x["timestamp"], reverse=True)
        return calls

    async def async_cleanup_old_records(self) -> None:
        """Drop records older than the configured retention window."""
        if self.retention_days == 0:
            return

        cutoff_str = (dt_util.utcnow() - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        removed = 0

        with self._lock:
            dates_to_remove = [k for k in self._call_history if k < cutoff_str]
            for date_key in dates_to_remove:
                del self._call_history[date_key]
                removed += 1

        if removed > 0:
            await self._save_history_async(dict(self._call_history))
            _LOGGER.debug(
                "API Tracker: pruned %d day(s) of expired call records",
                removed,
            )


    def _rate_from_history(self) -> tuple[float, str] | None:
        """Return (calls_per_hour, description) from recent calls, or None on too-thin data."""
        calls = self.get_call_history(days=1)
        if len(calls) < _MIN_CALLS_FOR_RATE:
            return None

        call_times: list[datetime] = []
        for call in calls:
            try:
                call_time = parse_iso_datetime(call["timestamp"])
                call_times.append(call_time)
            except (ValueError, TypeError, KeyError):
                continue

        if len(call_times) < _MIN_CALLS_FOR_RATE:
            return None

        call_times.sort()
        time_span_hours = (call_times[-1] - call_times[0]).total_seconds() / 3600
        if time_span_hours < _MIN_TIME_SPAN_HOURS:
            return None

        rate = len(call_times) / time_span_hours
        if not 1 <= rate <= _MAX_REASONABLE_RATE:
            return None

        return rate, f"history ({len(call_times)} calls / {time_span_hours:.1f}h)"

    def _rate_from_config(self) -> tuple[float, str]:
        """Return (calls_per_hour, description) estimated from polling config.

        Uses the actual calls-per-sync count from enabled features and
        blends day + night intervals weighted by their durations.
        Blending matters because the reset extrapolation covers the
        whole 24-hour window since reset, which spans both polling
        modes. Using only the day interval would overestimate the
        rate during the night.
        """
        try:
            config_manager = self._config_manager
            if not config_manager:
                return _DEFAULT_CALLS_PER_HOUR, "default"

            from .polling import _get_calls_per_sync

            calls_per_sync = _get_calls_per_sync(config_manager)

            from .const import DEFAULT_DAY_INTERVAL, DEFAULT_NIGHT_INTERVAL

            custom_day = config_manager.get_custom_day_interval()
            custom_night = config_manager.get_custom_night_interval()
            day_interval: float = custom_day or DEFAULT_DAY_INTERVAL
            night_interval: float = custom_night or DEFAULT_NIGHT_INTERVAL

            day_start = config_manager.get_day_start_hour()
            night_start = config_manager.get_night_start_hour()

            if day_start == night_start:
                blended_interval = day_interval
            else:
                day_hours = night_start - day_start if night_start > day_start else 24 - day_start + night_start
                night_hours = 24 - day_hours

                blended_interval = (
                    (day_hours * day_interval) + (night_hours * night_interval)
                ) / 24

            polls_per_hour = 60 / blended_interval
            rate = polls_per_hour * calls_per_sync
            desc = (
                f"config (day={day_interval:.0f}min, night={night_interval:.0f}min, "
                f"blend={blended_interval:.1f}min, cps={calls_per_sync})"
            )
            return rate, desc
        except (AttributeError, TypeError, ValueError) as e:
            _LOGGER.debug(
                "API Tracker: could not derive rate from config (%s), "
                "falling back to default %d calls/hour",
                e, _DEFAULT_CALLS_PER_HOUR,
            )
            return _DEFAULT_CALLS_PER_HOUR, "default"

    def extrapolate_reset_time(self, current_used: int) -> datetime | None:
        """Estimate when the daily quota last reset, in UTC, or None if uncertain.

        Hybrid strategy: history-derived rate when there are enough
        recent calls (more accurate), otherwise config-derived rate.
        Returns None when `current_used` is non-positive, the tracker
        hasn't initialised yet, or the result lands outside the 0-24h
        plausibility window.
        """
        if current_used <= 0:
            return None

        if not self._initialized:
            _LOGGER.debug(
                "API Tracker: extrapolate_reset_time called before "
                "async_init, returning None",
            )
            return None

        history_result = self._rate_from_history()
        if history_result is not None:
            calls_per_hour, rate_source = history_result
        else:
            calls_per_hour, rate_source = self._rate_from_config()

        if calls_per_hour < 1:
            _LOGGER.debug(
                "API Tracker: derived calls-per-hour %.2f is below 1, "
                "skipping reset extrapolation",
                calls_per_hour,
            )
            return None

        hours_since_reset = current_used / calls_per_hour

        if hours_since_reset > _HOURS_IN_DAY or hours_since_reset < 0:
            _LOGGER.debug(
                "API Tracker: extrapolated reset is %.2fh ago, outside "
                "the 0-24h plausibility window, skipping",
                hours_since_reset,
            )
            return None

        now_utc = dt_util.utcnow()
        estimated_reset = now_utc - timedelta(hours=hours_since_reset)

        _LOGGER.debug(
            "API Tracker: extrapolated quota reset at %s UTC "
            "(used=%s, rate=%.1f/h via %s, %.1fh ago)",
            estimated_reset.strftime("%H:%M"),
            current_used,
            calls_per_hour,
            rate_source,
            hours_since_reset,
        )

        return estimated_reset
