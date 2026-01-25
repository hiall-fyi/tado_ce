"""API Call Tracker for Tado CE integration.

v1.6.2: Made file I/O async-safe using run_in_executor to avoid blocking the event loop.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

_LOGGER = logging.getLogger(__name__)

# Call type codes
CALL_TYPE_ZONE_STATES = 1
CALL_TYPE_WEATHER = 2
CALL_TYPE_ZONES = 3
CALL_TYPE_MOBILE_DEVICES = 4
CALL_TYPE_OVERLAY = 5
CALL_TYPE_PRESENCE_LOCK = 6
CALL_TYPE_HOME_STATE = 7

CALL_TYPE_NAMES = {
    CALL_TYPE_ZONE_STATES: "zoneStates",
    CALL_TYPE_WEATHER: "weather",
    CALL_TYPE_ZONES: "zones",
    CALL_TYPE_MOBILE_DEVICES: "mobileDevices",
    CALL_TYPE_OVERLAY: "overlay",
    CALL_TYPE_PRESENCE_LOCK: "presenceLock",
    CALL_TYPE_HOME_STATE: "homeState",
}

# Shared executor for file I/O (avoids creating new threads for each operation)
_io_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tado_ce_io")


class APICallTracker:
    """Track API calls with persistent storage.
    
    All file I/O is performed in a thread pool to avoid blocking the event loop.
    """
    
    def __init__(self, data_dir: Path, retention_days: int = 14):
        """Initialize API call tracker.
        
        Args:
            data_dir: Directory for storing call history
            retention_days: Number of days to retain history (0 = forever)
        """
        self.data_dir = data_dir
        self.retention_days = retention_days
        self.history_file = data_dir / "api_call_history.json"
        self._lock = Lock()
        self._call_history: Dict[str, List[Dict]] = {}
        self._last_cleanup_date = None
        self._initialized = False
        self._pending_writes: List[Dict] = []  # Buffer for async writes
        
        # Ensure data directory exists (sync, but fast)
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_history_sync(self) -> Dict:
        """Load call history from disk (sync, for executor)."""
        try:
            if self.history_file.exists():
                with open(self.history_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            _LOGGER.error(f"Failed to load API call history: {e}")
        return {}
    
    def _save_history_sync(self, data: Dict):
        """Save call history to disk (sync, for executor)."""
        try:
            with open(self.history_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            _LOGGER.error(f"Failed to save API call history: {e}")
    
    async def async_init(self):
        """Initialize tracker asynchronously (load history from disk)."""
        if self._initialized:
            return
        
        loop = asyncio.get_event_loop()
        self._call_history = await loop.run_in_executor(
            _io_executor, self._load_history_sync
        )
        self._initialized = True
        _LOGGER.debug(f"Loaded API call history: {len(self._call_history)} dates")
        
        # Cleanup old records
        await self.async_cleanup_old_records()
        self._last_cleanup_date = datetime.now().date()
    
    def _ensure_initialized_sync(self):
        """Ensure tracker is initialized (sync fallback for non-async contexts).
        
        This loads history synchronously if not already loaded.
        Should only be used when async_init() cannot be called.
        """
        if not self._initialized:
            self._call_history = self._load_history_sync()
            self._initialized = True
            _LOGGER.debug(f"Loaded API call history (sync): {len(self._call_history)} dates")
    
    async def async_record_call(self, call_type: int, status_code: int, 
                                 timestamp: Optional[datetime] = None):
        """Record an API call asynchronously.
        
        Args:
            call_type: Type of API call (1-7)
            status_code: HTTP status code
            timestamp: Call timestamp (defaults to now in UTC)
        """
        if not self._initialized:
            await self.async_init()
        
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        
        date_key = timestamp.strftime("%Y-%m-%d")
        today = timestamp.date()
        should_cleanup = False
        
        call_record = {
            "type": call_type,
            "type_name": CALL_TYPE_NAMES.get(call_type, "unknown"),
            "status": status_code,
            "timestamp": timestamp.isoformat()
        }
        
        with self._lock:
            if date_key not in self._call_history:
                self._call_history[date_key] = []
            self._call_history[date_key].append(call_record)
            
            if self._last_cleanup_date is None or self._last_cleanup_date < today:
                self._last_cleanup_date = today
                should_cleanup = True
        
        # Save asynchronously
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            _io_executor, self._save_history_sync, dict(self._call_history)
        )
        
        if should_cleanup:
            await self.async_cleanup_old_records()
        
        _LOGGER.debug(f"Recorded API call: {CALL_TYPE_NAMES.get(call_type)} (status {status_code})")
    
    def record_call(self, call_type: int, status_code: int, 
                    timestamp: Optional[datetime] = None):
        """Record an API call (sync version, schedules async save).
        
        This method is sync-compatible but schedules the file write asynchronously.
        Use async_record_call() when in an async context for better performance.
        """
        self._ensure_initialized_sync()
        
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        
        date_key = timestamp.strftime("%Y-%m-%d")
        
        call_record = {
            "type": call_type,
            "type_name": CALL_TYPE_NAMES.get(call_type, "unknown"),
            "status": status_code,
            "timestamp": timestamp.isoformat()
        }
        
        with self._lock:
            if date_key not in self._call_history:
                self._call_history[date_key] = []
            self._call_history[date_key].append(call_record)
        
        # Schedule async save if we're in an event loop
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                _io_executor, self._save_history_sync, dict(self._call_history)
            )
        except RuntimeError:
            # No event loop, save synchronously (fallback)
            self._save_history_sync(dict(self._call_history))
        
        _LOGGER.debug(f"Recorded API call: {CALL_TYPE_NAMES.get(call_type)} (status {status_code})")
    
    def get_call_history(self, days: int = 1) -> List[Dict]:
        """Get list of API calls from the last N days.
        
        Args:
            days: Number of days to retrieve
            
        Returns:
            List of call records sorted by timestamp (newest first)
        """
        self._ensure_initialized_sync()
        
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        calls = []
        
        with self._lock:
            for date_key, date_calls in self._call_history.items():
                if date_key >= cutoff_date:
                    calls.extend(date_calls)
        
        calls.sort(key=lambda x: x["timestamp"], reverse=True)
        return calls
    
    def get_recent_calls(self, limit: int = 50) -> List[Dict]:
        """Get the most recent N calls for sensor attributes."""
        self._ensure_initialized_sync()
        
        all_calls = []
        with self._lock:
            for date_calls in self._call_history.values():
                all_calls.extend(date_calls)
        
        all_calls.sort(key=lambda x: x["timestamp"], reverse=True)
        return all_calls[:limit]
    
    def get_call_counts(self, days: int = 1) -> Dict[str, int]:
        """Get counts by call type for the last N days."""
        calls = self.get_call_history(days)
        counts = {}
        for call in calls:
            type_name = call.get("type_name", "unknown")
            counts[type_name] = counts.get(type_name, 0) + 1
        return counts
    
    async def async_cleanup_old_records(self):
        """Remove records older than retention period (async)."""
        if self.retention_days == 0:
            return
        
        cutoff_str = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        removed = 0
        
        with self._lock:
            dates_to_remove = [k for k in self._call_history.keys() if k < cutoff_str]
            for date_key in dates_to_remove:
                del self._call_history[date_key]
                removed += 1
        
        if removed > 0:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                _io_executor, self._save_history_sync, dict(self._call_history)
            )
            _LOGGER.info(f"Cleaned up {removed} days of old API call records")
    
    def cleanup_old_records(self):
        """Remove records older than retention period (sync)."""
        if self.retention_days == 0:
            return
        
        self._ensure_initialized_sync()
        cutoff_str = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        
        with self._lock:
            dates_to_remove = [k for k in self._call_history.keys() if k < cutoff_str]
            for date_key in dates_to_remove:
                del self._call_history[date_key]
            
            if dates_to_remove:
                try:
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(
                        _io_executor, self._save_history_sync, dict(self._call_history)
                    )
                except RuntimeError:
                    self._save_history_sync(dict(self._call_history))
                _LOGGER.info(f"Cleaned up {len(dates_to_remove)} days of old API call records")
    
    def get_daily_usage(self, date) -> Dict:
        """Get API usage statistics for a specific date."""
        self._ensure_initialized_sync()
        date_key = date.strftime("%Y-%m-%d")
        
        with self._lock:
            date_calls = self._call_history.get(date_key, [])
        
        by_type = {}
        for call in date_calls:
            type_name = call.get("type_name", "unknown")
            by_type[type_name] = by_type.get(type_name, 0) + 1
        
        return {"date": date_key, "total_calls": len(date_calls), "by_type": by_type}
    
    def extrapolate_reset_time(self, current_used: int) -> Optional[datetime]:
        """Extrapolate when the API reset happened by looking at usage rate.
        
        Uses a hybrid approach:
        1. If call history has enough data, use actual call rate (more accurate)
        2. Otherwise, fall back to config-based rate estimation
        
        Args:
            current_used: Current number of API calls used today (from Tado API)
            
        Returns:
            Estimated reset time (datetime in UTC), or None if not enough data
        """
        if current_used <= 0:
            return None
        
        now_utc = datetime.now(timezone.utc)
        calls_per_hour = None
        rate_source = "unknown"
        
        # Strategy A: Try to use actual call history rate (more accurate)
        self._ensure_initialized_sync()
        calls = self.get_call_history(days=1)
        
        if len(calls) >= 20:  # Need enough calls for reliable rate
            # Parse timestamps
            call_times = []
            for call in calls:
                try:
                    ts = call["timestamp"]
                    call_time = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    if call_time.tzinfo is None:
                        call_time = call_time.replace(tzinfo=timezone.utc)
                    call_times.append(call_time)
                except Exception:
                    continue
            
            if len(call_times) >= 20:
                call_times.sort()
                oldest_call = call_times[0]
                newest_call = call_times[-1]
                time_span_hours = (newest_call - oldest_call).total_seconds() / 3600
                
                if time_span_hours >= 1.0:  # Need at least 1 hour span
                    # Calculate actual rate from history
                    actual_calls_per_hour = len(call_times) / time_span_hours
                    
                    # Sanity check: rate should be reasonable (1-100 calls/hour)
                    if 1 <= actual_calls_per_hour <= 100:
                        calls_per_hour = actual_calls_per_hour
                        rate_source = f"history ({len(call_times)} calls / {time_span_hours:.1f}h)"
        
        # Strategy B: Fall back to config-based rate
        if calls_per_hour is None:
            try:
                from .config_manager import ConfigurationManager
                config_manager = ConfigurationManager(None)
                
                # Get custom intervals or use defaults
                custom_day = config_manager.get_custom_day_interval()
                custom_night = config_manager.get_custom_night_interval()
                
                # Default intervals for 5000 limit
                day_interval = custom_day if custom_day else 10
                night_interval = custom_night if custom_night else 30
                
                # Use day rate (more conservative estimate)
                polls_per_hour = 60 / day_interval
                calls_per_poll = 2.5  # Average
                calls_per_hour = polls_per_hour * calls_per_poll
                rate_source = f"config (day={day_interval}min)"
                
            except Exception as e:
                _LOGGER.debug(f"Failed to get config rate: {e}")
                # Ultimate fallback: assume 15 calls/hour
                calls_per_hour = 15
                rate_source = "default"
        
        if calls_per_hour is None or calls_per_hour < 1:
            _LOGGER.debug(f"Calls per hour invalid: {calls_per_hour}")
            return None
        
        # Extrapolate backwards: how many hours ago was used = 0?
        hours_since_reset = current_used / calls_per_hour
        
        # Sanity check: reset should be within last 24 hours
        if hours_since_reset > 24 or hours_since_reset < 0:
            _LOGGER.debug(f"Extrapolated reset time out of range: {hours_since_reset:.2f}h ago")
            return None
        
        estimated_reset = now_utc - timedelta(hours=hours_since_reset)
        
        _LOGGER.debug(
            f"Extrapolated reset time: {estimated_reset.strftime('%H:%M')} UTC "
            f"(used={current_used}, rate={calls_per_hour:.1f}/h [{rate_source}], {hours_since_reset:.1f}h ago)"
        )
        
        return estimated_reset


def cleanup_executor():
    """Shutdown the I/O executor to prevent thread leaks on integration reload.
    
    MUST be called in async_unload_entry() to properly cleanup resources.
    """
    global _io_executor
    if _io_executor is not None:
        _io_executor.shutdown(wait=False)
        _io_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tado_ce_io")
        _LOGGER.debug("API call tracker executor reset")
