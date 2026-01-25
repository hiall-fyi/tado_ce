"""Tado CE Integration."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, DATA_DIR, CONFIG_FILE, RATELIMIT_FILE, TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID, API_ENDPOINT_DEVICES
from .config_manager import ConfigurationManager
from .auth_manager import get_auth_manager
from .async_api import get_async_client

_LOGGER = logging.getLogger(__name__)

# Platform.BUTTON was added in Home Assistant 2021.12
# For backward compatibility, check if it exists
try:
    BASE_PLATFORMS = [Platform.SENSOR, Platform.CLIMATE, Platform.BINARY_SENSOR, Platform.WATER_HEATER, Platform.DEVICE_TRACKER, Platform.SWITCH, Platform.BUTTON]
    CALENDAR_PLATFORM = Platform.CALENDAR
except AttributeError:
    # Older Home Assistant version without Platform.BUTTON
    BASE_PLATFORMS = [Platform.SENSOR, Platform.CLIMATE, Platform.BINARY_SENSOR, Platform.WATER_HEATER, Platform.DEVICE_TRACKER, Platform.SWITCH]
    CALENDAR_PLATFORM = None
    _LOGGER.debug("Platform.BUTTON not available - button entities will not be loaded")

# v1.6.0: Removed SCRIPT_PATH - no longer using subprocess for sync
# Legacy tado_api.py is deprecated but kept for reference

# Service names
SERVICE_SET_CLIMATE_TIMER = "set_climate_timer"
SERVICE_SET_WATER_HEATER_TIMER = "set_water_heater_timer"
SERVICE_RESUME_SCHEDULE = "resume_schedule"
SERVICE_SET_TEMP_OFFSET = "set_climate_temperature_offset"  # Match official Tado integration
SERVICE_GET_TEMP_OFFSET = "get_temperature_offset"  # New: on-demand offset fetch
SERVICE_ADD_METER_READING = "add_meter_reading"
SERVICE_IDENTIFY_DEVICE = "identify_device"
SERVICE_SET_AWAY_CONFIG = "set_away_configuration"

# Smart polling configuration
POLLING_INTERVALS = [
    (100, 30, 120),
    (1000, 15, 60),
    (5000, 10, 30),
    (20000, 5, 15),
]


async def async_detect_reset_from_history(hass: HomeAssistant) -> datetime | None:
    """Detect API reset time from Home Assistant sensor history.
    
    Queries the recorder for sensor.tado_ce_api_usage history and finds
    the time when the value dropped to its minimum (reset point).
    
    Args:
        hass: Home Assistant instance
        
    Returns:
        Estimated reset time (datetime in UTC), or None if not enough data
    """
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states
        from homeassistant.util import dt as dt_util
        
        # Query last 36 hours of history (to catch reset even if it was yesterday)
        end_time = dt_util.utcnow()
        start_time = end_time - timedelta(hours=36)
        
        entity_id = "sensor.tado_ce_api_usage"
        
        # Get history from recorder
        def _get_history():
            return get_significant_states(
                hass,
                start_time,
                end_time,
                [entity_id],
                significant_changes_only=False
            )
        
        states = await get_instance(hass).async_add_executor_job(_get_history)
        
        if not states or entity_id not in states:
            _LOGGER.debug("HA History Detection: No history found for sensor.tado_ce_api_usage")
            return None
        
        history = states[entity_id]
        if len(history) < 10:
            _LOGGER.debug(f"HA History Detection: Not enough history points ({len(history)})")
            return None
        
        # Parse states and find minimum value (reset point)
        # The reset is when value drops from high to low
        min_value = float('inf')
        min_time = None
        prev_value = None
        
        for state in history:
            try:
                value = int(state.state)
                state_time = state.last_changed
                
                # Detect reset: value dropped significantly (>50% drop or to <10)
                if prev_value is not None and prev_value > 50:
                    if value < prev_value * 0.2 or value < 10:
                        # This is likely the reset point
                        _LOGGER.debug(
                            f"HA History Detection: Reset detected! {prev_value} -> {value} at {state_time}"
                        )
                        return state_time.replace(tzinfo=timezone.utc) if state_time.tzinfo is None else state_time
                
                # Track minimum as fallback
                if value < min_value:
                    min_value = value
                    min_time = state_time
                
                prev_value = value
                
            except (ValueError, TypeError):
                continue
        
        # If no clear reset detected, use minimum value time
        if min_time and min_value < 20:
            _LOGGER.debug(f"HA History Detection: Using minimum value as reset: {min_value} at {min_time}")
            return min_time.replace(tzinfo=timezone.utc) if min_time.tzinfo is None else min_time
        
        _LOGGER.debug(f"HA History Detection: Could not detect reset (min_value={min_value})")
        return None
        
    except ImportError:
        _LOGGER.debug("Recorder component not available")
        return None
    except Exception as e:
        _LOGGER.debug(f"Failed to detect reset from history: {e}")
        return None


async def _update_ratelimit_reset_time(hass: HomeAssistant, detected_reset: datetime) -> None:
    """Update ratelimit.json with detected reset time from HA history.
    
    This is called after sync when we detect the actual reset time from
    sensor.tado_ce_api_usage history. It's more accurate than extrapolation.
    
    Args:
        hass: Home Assistant instance
        detected_reset: Detected reset time (datetime in UTC)
    """
    try:
        def _update_file():
            if not RATELIMIT_FILE.exists():
                return
            
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
            
            # Only update if detected time is different from stored time
            current_reset = data.get("last_reset_utc")
            new_reset = detected_reset.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            if current_reset != new_reset:
                data["last_reset_utc"] = new_reset
                
                # Recalculate reset_seconds, reset_at, reset_human
                now_utc = datetime.now(timezone.utc)
                next_reset = detected_reset + timedelta(hours=24)
                
                # If next_reset is in the past, add another 24h
                while next_reset <= now_utc:
                    next_reset += timedelta(hours=24)
                
                seconds_until_reset = int((next_reset - now_utc).total_seconds())
                
                if seconds_until_reset > 0:
                    hours = seconds_until_reset // 3600
                    minutes = (seconds_until_reset % 3600) // 60
                    data["reset_seconds"] = seconds_until_reset
                    data["reset_at"] = next_reset.isoformat()
                    data["reset_human"] = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
                
                # Write back
                import tempfile
                import shutil
                
                with tempfile.NamedTemporaryFile(
                    mode='w', dir=RATELIMIT_FILE.parent, delete=False, suffix='.tmp'
                ) as tmp:
                    json.dump(data, tmp, indent=2)
                    temp_path = tmp.name
                
                shutil.move(temp_path, RATELIMIT_FILE)
                _LOGGER.info(
                    f"Updated reset time from HA history: {detected_reset.strftime('%H:%M')} UTC"
                )
        
        await hass.async_add_executor_job(_update_file)
        
    except Exception as e:
        _LOGGER.debug(f"Failed to update ratelimit reset time: {e}")


DEFAULT_DAY_INTERVAL = 30
DEFAULT_NIGHT_INTERVAL = 120
FULL_SYNC_INTERVAL_HOURS = 6


def is_daytime(config_manager: ConfigurationManager) -> bool:
    """Check if current time is daytime based on configured hours.
    
    Args:
        config_manager: Configuration manager with day/night hour settings
        
    Returns:
        True if current time is within day hours, False otherwise
        
    Note:
        If day_start == night_start, returns True (uniform mode - always day polling)
        
    v1.6.1: Uses Home Assistant's timezone instead of system timezone
    """
    from homeassistant.util import dt as dt_util
    
    # Use HA's timezone-aware current time
    now = dt_util.now()
    hour = now.hour
    
    day_start = config_manager.get_day_start_hour()
    night_start = config_manager.get_night_start_hour()
    
    # Uniform mode: if day_start == night_start, always use day interval
    if day_start == night_start:
        return True
    
    return day_start <= hour < night_start


def get_polling_interval(config_manager: ConfigurationManager, cached_ratelimit: dict | None = None) -> int:
    """Get polling interval based on configuration and API rate limit.
    
    Args:
        config_manager: Configuration manager with polling settings
        cached_ratelimit: Pre-loaded ratelimit data (to avoid blocking I/O in async context)
        
    Returns:
        Polling interval in minutes
    """
    daytime = is_daytime(config_manager)
    
    # Check for custom intervals first
    if daytime:
        custom_interval = config_manager.get_custom_day_interval()
        if custom_interval is not None:
            _log_quota_warning_if_needed(custom_interval, daytime, config_manager)
            return custom_interval
    else:
        custom_interval = config_manager.get_custom_night_interval()
        if custom_interval is not None:
            _log_quota_warning_if_needed(custom_interval, daytime, config_manager)
            return custom_interval
    
    # Fall back to smart polling based on API quota (or Test Mode limit)
    try:
        # Get effective API limit (100 if Test Mode, otherwise actual limit)
        effective_limit = None
        if config_manager.get_test_mode_enabled():
            effective_limit = 100
            _LOGGER.info("Tado CE: Test Mode enabled - using 100 call limit")
        elif cached_ratelimit is not None:
            # Use pre-loaded data (async-safe)
            effective_limit = cached_ratelimit.get("limit")
        elif RATELIMIT_FILE.exists():
            # Fallback: sync read (only for non-async callers)
            # WARNING: This will trigger blocking I/O warning if called from async context
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                effective_limit = data.get("limit")
        
        if effective_limit:
            for threshold, day_interval, night_interval in POLLING_INTERVALS:
                if effective_limit <= threshold:
                    return day_interval if daytime else night_interval
            # Use fastest for highest limits
            _, day_interval, night_interval = POLLING_INTERVALS[-1]
            return day_interval if daytime else night_interval
    except Exception:
        pass
    
    return DEFAULT_DAY_INTERVAL if daytime else DEFAULT_NIGHT_INTERVAL


def _log_quota_warning_if_needed(interval: int, daytime: bool, config_manager: ConfigurationManager):
    """Log warning if custom interval would exceed API quota.
    
    Args:
        interval: Custom polling interval in minutes
        daytime: Whether it's currently daytime
        config_manager: Configuration manager
    """
    # Calculate calls per day with this interval
    # Assuming 2-3 API calls per sync (zoneStates + weather if enabled)
    weather_enabled = config_manager.get_weather_enabled()
    calls_per_sync = 2 if weather_enabled else 1
    
    # Get both intervals to calculate total daily calls
    day_interval = config_manager.get_custom_day_interval() or DEFAULT_DAY_INTERVAL
    night_interval = config_manager.get_custom_night_interval() or DEFAULT_NIGHT_INTERVAL
    
    # Assume 16 hours day, 8 hours night (based on default 7am-11pm)
    day_hours = 16
    night_hours = 8
    
    day_syncs = (day_hours * 60) / day_interval
    night_syncs = (night_hours * 60) / night_interval
    total_calls = (day_syncs + night_syncs) * calls_per_sync
    
    # Warn if exceeding low-tier quota (500 calls/day)
    low_tier_quota = 500
    if total_calls > low_tier_quota:
        _LOGGER.warning(
            f"Tado CE: Custom polling intervals may exceed API quota. "
            f"Estimated {total_calls:.0f} calls/day with day={day_interval}m, night={night_interval}m. "
            f"Consider increasing intervals to stay under {low_tier_quota} calls/day."
        )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Tado CE component."""
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry to new version.
    
    v1.5.3: Added comprehensive debug logging for upgrade troubleshooting.
    If upgrade fails, users can share logs to help diagnose issues.
    
    v1.6.0: Fixed cumulative migration - uses `< X` pattern instead of `== X`
    to ensure users jumping multiple versions (e.g., v1 -> v5) run ALL
    intermediate migrations correctly.
    """
    # Store initial version for logging (version may change during migration)
    initial_version = config_entry.version
    
    _LOGGER.info(
        "=== Tado CE Migration Start ===\n"
        f"  Current version: {initial_version}\n"
        f"  Target version: 5\n"
        f"  Entry ID: {config_entry.entry_id}\n"
        f"  Entry data: {config_entry.data}"
    )
    
    # Log file system state for debugging
    from .const import LEGACY_DATA_DIR, ZONES_INFO_FILE
    _LOGGER.info(
        "=== File System State ===\n"
        f"  DATA_DIR exists: {DATA_DIR.exists()}\n"
        f"  DATA_DIR path: {DATA_DIR}\n"
        f"  LEGACY_DATA_DIR exists: {LEGACY_DATA_DIR.exists()}\n"
        f"  LEGACY_DATA_DIR path: {LEGACY_DATA_DIR}\n"
        f"  CONFIG_FILE exists: {CONFIG_FILE.exists()}\n"
        f"  CONFIG_FILE path: {CONFIG_FILE}"
    )
    
    # List files in both directories for debugging
    if DATA_DIR.exists():
        try:
            files = list(DATA_DIR.glob("*.json"))
            _LOGGER.info(f"  DATA_DIR files: {[f.name for f in files]}")
        except Exception as e:
            _LOGGER.warning(f"  Could not list DATA_DIR files: {e}")
    
    if LEGACY_DATA_DIR.exists():
        try:
            files = list(LEGACY_DATA_DIR.glob("*.json"))
            _LOGGER.info(f"  LEGACY_DATA_DIR files: {[f.name for f in files]}")
        except Exception as e:
            _LOGGER.warning(f"  Could not list LEGACY_DATA_DIR files: {e}")
    
    # v1.5.2: Migrate data directory from custom_components/tado_ce/data/ to .storage/tado_ce/
    if LEGACY_DATA_DIR.exists() and not DATA_DIR.exists():
        _LOGGER.info("=== Data Directory Migration ===")
        _LOGGER.info("Migrating data directory from legacy location to .storage/tado_ce/")
        import shutil
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _LOGGER.info(f"  Created DATA_DIR: {DATA_DIR}")
        except Exception as e:
            _LOGGER.error(f"  Failed to create DATA_DIR: {e}")
            return False
        
        migrated_files = []
        failed_files = []
        for file in LEGACY_DATA_DIR.glob("*.json"):
            try:
                shutil.copy2(file, DATA_DIR / file.name)
                migrated_files.append(file.name)
                _LOGGER.info(f"  Migrated {file.name}")
            except Exception as e:
                failed_files.append((file.name, str(e)))
                _LOGGER.error(f"  Failed to migrate {file.name}: {e}")
        
        _LOGGER.info(f"  Migrated files: {migrated_files}")
        if failed_files:
            _LOGGER.error(f"  Failed files: {failed_files}")
        
        # Copy log file too if exists
        legacy_log = LEGACY_DATA_DIR / "api.log"
        if legacy_log.exists():
            try:
                shutil.copy2(legacy_log, DATA_DIR / "api.log")
                _LOGGER.info("  Migrated api.log")
            except Exception:
                pass  # Log file is not critical
        _LOGGER.info("Data directory migration complete")

    # v1.6.0: Cumulative migration using `< X` pattern
    # This ensures users jumping multiple versions (e.g., v1 -> v5) run ALL migrations
    # Previous `== X` pattern could miss migrations if config_entry.version wasn't
    # updated in-place after async_update_entry()
    
    if initial_version < 2:
        # Version 1 (v1.1.0) -> 2 (v1.2.0): Handle zone-based device migration
        _LOGGER.info("=== Migration: v1 -> v2 ===")
        _LOGGER.info("Migrating from v1.1.0 to v1.2.0 format")
        
        # Ensure data directory exists
        try:
            DATA_DIR.mkdir(exist_ok=True)
            _LOGGER.info(f"  DATA_DIR ensured: {DATA_DIR}")
        except Exception as e:
            _LOGGER.error(f"  Failed to create DATA_DIR: {e}")
        
        # Check if zones_info.json exists, if not, trigger a full sync
        if not ZONES_INFO_FILE.exists():
            _LOGGER.warning("  zones_info.json missing - will be created on first sync")
            # Don't fail migration - let the sync create it
        else:
            _LOGGER.info("  zones_info.json exists")
        
        _LOGGER.info("Migration step v1 -> v2 complete")

    if initial_version < 4:
        # Version 2/3 -> 4 (v1.4.0): New device authorization flow
        _LOGGER.info(f"=== Migration: v{initial_version} -> v4 ===")
        _LOGGER.info("Migrating to v1.4.0 format (device authorization)")
        
        # Ensure data directory exists
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _LOGGER.info(f"  DATA_DIR ensured: {DATA_DIR}")
        except Exception as e:
            _LOGGER.error(f"  Failed to create DATA_DIR: {e}")
        
        # Check if config.json exists with valid refresh_token
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                
                _LOGGER.info(f"  config.json keys: {list(config.keys())}")
                _LOGGER.info(f"  home_id present: {'home_id' in config}")
                _LOGGER.info(f"  refresh_token present: {'refresh_token' in config and bool(config.get('refresh_token'))}")
                
                if config.get("refresh_token"):
                    _LOGGER.info("  Existing refresh_token found - authentication should work")
                else:
                    _LOGGER.warning(
                        "  No refresh_token in config.json - re-authentication may be required. "
                        "If entities are unavailable, use Reconfigure option or delete and re-add the integration."
                    )
            except json.JSONDecodeError as e:
                _LOGGER.error(f"  config.json is invalid JSON: {e}")
            except Exception as e:
                _LOGGER.warning(f"  Could not read config.json: {e}")
        else:
            _LOGGER.warning(
                "  config.json not found - re-authentication required. "
                "Delete and re-add the integration to authenticate."
            )
        
        _LOGGER.info("Migration step -> v4 complete")

    if initial_version < 5:
        # Version 4 -> 5 (v1.5.2): Data directory moved to .storage/tado_ce/
        _LOGGER.info("=== Migration: -> v5 ===")
        _LOGGER.info("Migrating to v1.5.2 format (new data directory)")
        
        # Data migration already handled at the top of this function
        _LOGGER.info("Migration step -> v5 complete")

    if initial_version < 6:
        # Version 5 -> 6 (v1.7.0): Change unique_id from tado_ce_integration to tado_ce_{home_id}
        _LOGGER.info("=== Migration: -> v6 ===")
        _LOGGER.info("Migrating to v1.7.0 format (unique_id change for multi-home support)")
        
        # Get home_id from config entry data or config.json
        home_id = config_entry.data.get("home_id")
        
        if not home_id and CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                    home_id = config.get("home_id")
                    _LOGGER.info(f"  Got home_id from config.json: {home_id}")
            except Exception as e:
                _LOGGER.warning(f"  Could not read home_id from config.json: {e}")
        
        if home_id:
            new_unique_id = f"tado_ce_{home_id}"
            _LOGGER.info(f"  Updating unique_id: tado_ce_integration -> {new_unique_id}")
            
            # Update the config entry's unique_id
            # Note: This is done via async_update_entry with the new unique_id
            hass.config_entries.async_update_entry(
                config_entry,
                unique_id=new_unique_id
            )
            _LOGGER.info(f"  unique_id updated to: {new_unique_id}")
        else:
            _LOGGER.warning(
                "  Could not determine home_id for unique_id migration. "
                "unique_id will remain as tado_ce_integration until re-authentication."
            )
        
        _LOGGER.info("Migration step -> v6 complete")

    if initial_version < 7:
        # Version 6 -> 7 (v1.8.0): Per-home data files for multi-home support
        _LOGGER.info("=== Migration: -> v7 ===")
        _LOGGER.info("Migrating to v1.8.0 format (per-home data files)")
        
        # Get home_id from config entry data or config.json
        home_id = config_entry.data.get("home_id")
        
        if not home_id and CONFIG_FILE.exists():
            try:
                def _read_home_id():
                    with open(CONFIG_FILE) as f:
                        return json.load(f).get("home_id")
                home_id = await hass.async_add_executor_job(_read_home_id)
                _LOGGER.info(f"  Got home_id from config.json: {home_id}")
            except Exception as e:
                _LOGGER.warning(f"  Could not read home_id from config.json: {e}")
        
        if home_id:
            from .const import PER_HOME_FILES, get_data_file, get_legacy_file
            import shutil
            
            def _migrate_files():
                """Migrate files in executor to avoid blocking I/O."""
                migrated = []
                for base_name in PER_HOME_FILES:
                    legacy_path = get_legacy_file(base_name)
                    new_path = get_data_file(base_name, home_id)
                    
                    # Only migrate if legacy exists and new doesn't
                    if legacy_path.exists() and not new_path.exists():
                        try:
                            shutil.copy2(legacy_path, new_path)
                            migrated.append(f"{base_name}.json -> {base_name}_{home_id}.json")
                        except Exception as e:
                            _LOGGER.error(f"  Failed to migrate {base_name}.json: {e}")
                return migrated
            
            migrated_files = await hass.async_add_executor_job(_migrate_files)
            
            for f in migrated_files:
                _LOGGER.info(f"  Migrated {f}")
            
            if migrated_files:
                _LOGGER.info(f"  Migrated {len(migrated_files)} files for home_id {home_id}")
            else:
                _LOGGER.info("  No files needed migration (already migrated or new install)")
        else:
            _LOGGER.warning(
                "  Could not determine home_id for data file migration. "
                "Files will remain with legacy names until re-authentication."
            )
        
        _LOGGER.info("Migration step -> v7 complete")

    # Update to final version (only once, at the end)
    if initial_version < 7:
        hass.config_entries.async_update_entry(config_entry, version=7)
        _LOGGER.info(
            "=== Migration Complete ===\n"
            f"  Initial version: {initial_version}\n"
            f"  Final version: 7\n"
            f"  CONFIG_FILE exists: {CONFIG_FILE.exists()}\n"
            f"  DATA_DIR exists: {DATA_DIR.exists()}"
        )
    else:
        _LOGGER.info("Config entry already at version 7, no migration needed")
    
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tado CE from a config entry."""
    _LOGGER.info(
        "=== Tado CE Setup Start ===\n"
        f"  Entry ID: {entry.entry_id}\n"
        f"  Entry version: {entry.version}\n"
        f"  Entry data: {entry.data}"
    )
    
    # Log file system state for debugging
    from .const import LEGACY_DATA_DIR, ZONES_INFO_FILE, ZONES_FILE
    _LOGGER.info(
        "=== Setup File System State ===\n"
        f"  DATA_DIR: {DATA_DIR} (exists: {DATA_DIR.exists()})\n"
        f"  CONFIG_FILE: {CONFIG_FILE} (exists: {CONFIG_FILE.exists()})\n"
        f"  ZONES_FILE: {ZONES_FILE} (exists: {ZONES_FILE.exists()})\n"
        f"  ZONES_INFO_FILE: {ZONES_INFO_FILE} (exists: {ZONES_INFO_FILE.exists()})\n"
        f"  LEGACY_DATA_DIR: {LEGACY_DATA_DIR} (exists: {LEGACY_DATA_DIR.exists()})"
    )
    
    # CRITICAL: Check for duplicate entries and remove old ones (v1.1.0 leftovers)
    # This must be done BEFORE any setup to avoid race conditions
    all_entries = hass.config_entries.async_entries(DOMAIN)
    if len(all_entries) > 1:
        _LOGGER.warning(f"Found {len(all_entries)} Tado CE entries - checking for duplicates")
        _LOGGER.info(f"  All entries: {[(e.entry_id, e.version) for e in all_entries]}")
        
        # Initialize domain data if needed
        if DOMAIN not in hass.data:
            hass.data[DOMAIN] = {}
        
        # Sort by version (descending), then by entry_id for deterministic ordering
        entries_by_version = sorted(
            all_entries, 
            key=lambda e: (getattr(e, 'version', 0), e.entry_id), 
            reverse=True
        )
        
        keeper_entry_id = entries_by_version[0].entry_id
        _LOGGER.info(f"  Keeper entry: {keeper_entry_id}")
        
        # If current entry is NOT the one to keep, abort this setup
        if entry.entry_id != keeper_entry_id:
            _LOGGER.warning(
                f"Current entry {entry.entry_id} (version {entry.version}) is duplicate. "
                f"Aborting setup - will be removed by keeper entry."
            )
            return False
        
        # Current entry IS the keeper - remove all others
        # Use a flag specific to THIS cleanup session to prevent duplicate work
        cleanup_key = f'duplicate_cleanup_{keeper_entry_id}'
        if cleanup_key not in hass.data[DOMAIN]:
            hass.data[DOMAIN][cleanup_key] = True
            
            _LOGGER.info(f"Entry {keeper_entry_id} is keeper - removing {len(entries_by_version) - 1} duplicates")
            
            for old_entry in entries_by_version[1:]:
                _LOGGER.warning(
                    f"Removing duplicate entry {old_entry.entry_id} "
                    f"(version {getattr(old_entry, 'version', 'unknown')})"
                )
                # CRITICAL: Use await to ensure removal completes before continuing
                # This prevents race condition where old entries continue setup
                try:
                    await hass.config_entries.async_remove(old_entry.entry_id)
                    _LOGGER.info(f"Successfully removed duplicate entry {old_entry.entry_id}")
                except Exception as e:
                    _LOGGER.error(f"Failed to remove duplicate entry {old_entry.entry_id}: {e}")
            
            # Verify cleanup
            _LOGGER.info(f"Duplicate cleanup complete. Keeper: {keeper_entry_id}")
    
    # v1.5.2: Migrate data from legacy location if needed
    # This handles cases where migration didn't run (e.g., fresh install with old data)
    if LEGACY_DATA_DIR.exists() and not DATA_DIR.exists():
        _LOGGER.info("=== Setup-time Data Migration ===")
        _LOGGER.info("Migrating data directory from legacy location to .storage/tado_ce/")
        import shutil
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _LOGGER.info(f"  Created DATA_DIR: {DATA_DIR}")
        except Exception as e:
            _LOGGER.error(f"  Failed to create DATA_DIR: {e}")
        
        migrated_files = []
        for file in LEGACY_DATA_DIR.glob("*.json"):
            try:
                shutil.copy2(file, DATA_DIR / file.name)
                migrated_files.append(file.name)
            except Exception as e:
                _LOGGER.error(f"  Failed to migrate {file.name}: {e}")
        _LOGGER.info(f"  Migrated files: {migrated_files}")
    
    # Ensure data directory exists
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _LOGGER.error(f"Failed to create DATA_DIR: {e}")
    
    # Initialize configuration manager
    config_manager = ConfigurationManager(entry, hass)
    _LOGGER.info(f"Configuration loaded: {config_manager.get_all_config()}")
    
    # v1.8.0: Set current home_id for data_loader multi-home support
    home_id = entry.data.get("home_id")
    if home_id:
        from .data_loader import set_current_home_id
        set_current_home_id(home_id)
        _LOGGER.info(f"Data loader home_id set to: {home_id}")
    
    # Store config_manager in hass.data for access by other components
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    
    # CRITICAL: Check if already setup to prevent multiple polling timers
    if 'polling_cancel' in hass.data[DOMAIN]:
        _LOGGER.warning("Tado CE: Already setup, cancelling old polling timer")
        old_cancel = hass.data[DOMAIN]['polling_cancel']
        if old_cancel:
            old_cancel()
    
    hass.data[DOMAIN]['config_manager'] = config_manager
    
    # Sync configuration to config.json for tado_api.py
    await config_manager.async_sync_all_to_config_json()
    
    # Initialize immediate refresh handler
    from .immediate_refresh_handler import get_handler
    refresh_handler = get_handler(hass)
    _LOGGER.info("Immediate refresh handler initialized")
    
    # Load home_id and version early to avoid race conditions in device_manager
    # These perform blocking I/O so must be run in executor
    from .device_manager import load_home_id, load_version
    await hass.async_add_executor_job(load_home_id)
    await hass.async_add_executor_job(load_version)
    
    # Check if config file exists
    if not CONFIG_FILE.exists():
        _LOGGER.warning(
            "Tado CE config file not found. "
            "Use Settings > Devices & Services > Add Integration > Tado CE to authenticate."
        )
    
    # Track current interval and last full sync time
    current_interval = [0]
    cancel_interval = [None]
    last_full_sync = [None]
    
    # Cache for ratelimit data (loaded async to avoid blocking I/O)
    cached_ratelimit = [None]
    
    async def async_load_ratelimit():
        """Load ratelimit data asynchronously."""
        if RATELIMIT_FILE.exists():
            def read_file():
                with open(RATELIMIT_FILE) as f:
                    return json.load(f)
            try:
                cached_ratelimit[0] = await hass.async_add_executor_job(read_file)
            except Exception:
                cached_ratelimit[0] = None
        else:
            cached_ratelimit[0] = None
    
    async def async_schedule_next_sync():
        """Schedule next sync with dynamic interval (async-safe)."""
        # Load ratelimit data asynchronously
        await async_load_ratelimit()
        
        new_interval = get_polling_interval(config_manager, cached_ratelimit[0])
        
        if new_interval != current_interval[0]:
            time_period = "day" if is_daytime(config_manager) else "night"
            _LOGGER.info(f"Tado CE: Polling interval set to {new_interval}m ({time_period})")
            current_interval[0] = new_interval
        
        # Cancel old interval
        if cancel_interval[0]:
            cancel_interval[0]()
        
        # Schedule new interval
        async def async_sync_wrapper(now):
            """Async wrapper for sync."""
            await async_sync_tado()
        
        cancel_interval[0] = async_track_time_interval(
            hass,
            async_sync_wrapper,
            timedelta(minutes=new_interval)
        )
        
        # Store cancel function in hass.data so we can cancel on reload
        hass.data[DOMAIN]['polling_cancel'] = cancel_interval[0]
    
    async def async_sync_tado():
        """Run Tado sync using async API (v1.6.0+).
        
        Replaces subprocess-based sync with native async calls.
        """
        # Check if polling should be paused due to Test Mode limit
        if config_manager.get_test_mode_enabled():
            try:
                if RATELIMIT_FILE.exists():
                    def read_ratelimit():
                        with open(RATELIMIT_FILE) as f:
                            return json.load(f)
                    data = await hass.async_add_executor_job(read_ratelimit)
                    used = data.get("used", 0)
                    if used >= 100:
                        _LOGGER.warning(
                            f"Tado CE: Test Mode limit reached ({used}/100 calls). "
                            "Polling paused until quota resets."
                        )
                        # Re-schedule to check again later
                        await async_schedule_next_sync()
                        return
            except Exception as e:
                _LOGGER.error(f"Failed to check Test Mode limit: {e}")
        
        # Determine if this should be a full sync
        do_full_sync = False
        if last_full_sync[0] is None:
            do_full_sync = True
        else:
            hours_since_full = (datetime.now() - last_full_sync[0]).total_seconds() / 3600
            if hours_since_full >= FULL_SYNC_INTERVAL_HOURS:
                do_full_sync = True
        
        sync_type = "full" if do_full_sync else "quick"
        _LOGGER.debug(f"Tado CE: Executing {sync_type} sync")
        
        try:
            # Get async client and perform sync
            client = get_async_client(hass)
            
            # Get config options
            weather_enabled = config_manager.get_weather_enabled()
            mobile_devices_enabled = config_manager.get_mobile_devices_enabled()
            mobile_devices_frequent_sync = config_manager.get_mobile_devices_frequent_sync()
            offset_enabled = config_manager.get_offset_enabled()
            home_state_sync_enabled = config_manager.get_home_state_sync_enabled()
            
            success = await client.async_sync(
                quick=not do_full_sync,
                weather_enabled=weather_enabled,
                mobile_devices_enabled=mobile_devices_enabled,
                mobile_devices_frequent_sync=mobile_devices_frequent_sync,
                offset_enabled=offset_enabled,
                home_state_sync_enabled=home_state_sync_enabled
            )
            
            if success:
                if do_full_sync:
                    last_full_sync[0] = datetime.now()
                
                # v1.7.0: Detect API reset time from HA sensor history
                # This is more accurate than extrapolation because it uses actual recorded data
                detected_reset = await async_detect_reset_from_history(hass)
                if detected_reset:
                    _LOGGER.debug(f"Tado CE: HA history detected reset at {detected_reset.strftime('%H:%M')} UTC")
                    await _update_ratelimit_reset_time(hass, detected_reset)
            else:
                _LOGGER.warning("Tado CE sync returned failure status")
                
        except Exception as e:
            _LOGGER.error(f"Tado CE sync ERROR: {e}")
        
        # Re-schedule with potentially new interval (day/night change)
        await async_schedule_next_sync()
    
    # Initial sync (only if config exists)
    _LOGGER.info(f"Tado CE: Checking config file at {CONFIG_FILE}, exists={CONFIG_FILE.exists()}")
    if CONFIG_FILE.exists():
        _LOGGER.info("Tado CE: Starting initial sync...")
        await async_sync_tado()
        _LOGGER.info("Tado CE: Initial sync completed")
    else:
        # Still schedule polling even without config
        _LOGGER.warning(f"Tado CE: Config file not found at {CONFIG_FILE}, scheduling polling only")
        await async_schedule_next_sync()
        _LOGGER.info("Tado CE: Polling scheduled")
    
    # Forward setup to platforms
    # Build platform list based on config
    platforms_to_load = list(BASE_PLATFORMS)
    
    # v1.8.0: Add Calendar platform if enabled (opt-in)
    if CALENDAR_PLATFORM and config_manager.get_schedule_calendar_enabled():
        platforms_to_load.append(CALENDAR_PLATFORM)
        _LOGGER.info("Tado CE: Schedule Calendar enabled")
    
    await hass.config_entries.async_forward_entry_setups(entry, platforms_to_load)
    
    # Register services
    await _async_register_services(hass)
    
    # Register update listener for options changes
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    _LOGGER.info("Tado CE: Integration loaded successfully")
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    _LOGGER.info("Tado CE: Options changed, reloading integration...")
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_services(hass: HomeAssistant):
    """Register Tado CE services."""
    
    # Check if services are already registered (avoid duplicate registration)
    if hass.services.has_service(DOMAIN, SERVICE_SET_CLIMATE_TIMER):
        _LOGGER.debug("Tado CE services already registered, skipping")
        return
    
    async def handle_set_climate_timer(call: ServiceCall):
        """Handle set_climate_timer service call.
        
        Compatible with official Tado integration format:
        - entity_id (required)
        - temperature (required)
        - time_period (required) - Time Period format (e.g., "01:30:00")
        - overlay (optional)
        """
        entity_ids = call.data.get("entity_id", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        
        temperature = call.data.get("temperature")
        time_period = call.data.get("time_period")
        overlay = call.data.get("overlay")
        
        # CRITICAL FIX: Validate time_period (same as water heater)
        if not time_period:
            error_msg = "time_period is required for set_climate_timer service"
            _LOGGER.error(error_msg)
            raise vol.Invalid(error_msg)
        
        # Convert time_period to minutes with validation
        try:
            from datetime import timedelta
            
            # Home Assistant cv.time_period returns timedelta
            if isinstance(time_period, timedelta):
                duration_minutes = int(time_period.total_seconds() / 60)
            else:
                # Fallback: parse string format HH:MM:SS
                time_parts = str(time_period).split(":")
                if len(time_parts) != 3:
                    raise ValueError(f"Invalid time_period format: {time_period}. Expected HH:MM:SS")
                
                hours = int(time_parts[0])
                minutes = int(time_parts[1])
                seconds = int(time_parts[2])
                
                # Validate ranges
                if not (0 <= hours <= 24):
                    raise ValueError(f"Hours must be 0-24, got {hours}")
                if not (0 <= minutes <= 59):
                    raise ValueError(f"Minutes must be 0-59, got {minutes}")
                if not (0 <= seconds <= 59):
                    raise ValueError(f"Seconds must be 0-59, got {seconds}")
                
                duration_minutes = hours * 60 + minutes + (seconds // 60)
            
            # Validate final duration (5-1440 minutes)
            if duration_minutes < 5:
                raise ValueError(f"Duration must be at least 5 minutes, got {duration_minutes}")
            if duration_minutes > 1440:
                raise ValueError(f"Duration must be at most 1440 minutes (24 hours), got {duration_minutes}")
            
            _LOGGER.info(f"Parsed time_period {time_period} to {duration_minutes} minutes")
            
        except (ValueError, AttributeError, TypeError) as e:
            error_msg = f"Failed to parse time_period: {e}"
            _LOGGER.error(error_msg)
            raise vol.Invalid(error_msg)
        
        # Validate temperature if provided
        if temperature is None:
            error_msg = "temperature is required for set_climate_timer service"
            _LOGGER.error(error_msg)
            raise vol.Invalid(error_msg)
        
        for entity_id in entity_ids:
            entity = hass.states.get(entity_id)
            if entity:
                # Get the climate entity and call async_set_timer
                climate_entity = hass.data.get("entity_components", {}).get("climate")
                if climate_entity:
                    for ent in climate_entity.entities:
                        if ent.entity_id == entity_id and hasattr(ent, 'async_set_timer'):
                            try:
                                await ent.async_set_timer(temperature, duration_minutes, overlay)
                                _LOGGER.info(f"Set timer for {entity_id}: {temperature}°C for {duration_minutes}min")
                            except Exception as e:
                                error_msg = f"Failed to set timer for {entity_id}: {e}"
                                _LOGGER.error(error_msg)
                                # Continue to next entity instead of failing completely
                            break
    
    async def handle_set_water_heater_timer(call: ServiceCall):
        """Handle set_water_heater_timer service call."""
        entity_ids = call.data.get("entity_id", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        
        time_period = call.data.get("time_period")
        temperature = call.data.get("temperature")
        
        # CRITICAL FIX: Validate time_period
        if not time_period:
            error_msg = "time_period is required for set_water_heater_timer service"
            _LOGGER.error(error_msg)
            raise vol.Invalid(error_msg)
        
        # Convert time_period to minutes with validation
        try:
            from datetime import timedelta
            
            # Home Assistant cv.time_period returns timedelta
            if isinstance(time_period, timedelta):
                duration_minutes = int(time_period.total_seconds() / 60)
            else:
                # Fallback: parse string format HH:MM:SS
                time_parts = str(time_period).split(":")
                if len(time_parts) != 3:
                    raise ValueError(f"Invalid time_period format: {time_period}. Expected HH:MM:SS")
                
                hours = int(time_parts[0])
                minutes = int(time_parts[1])
                seconds = int(time_parts[2])
                
                # Validate ranges
                if not (0 <= hours <= 24):
                    raise ValueError(f"Hours must be 0-24, got {hours}")
                if not (0 <= minutes <= 59):
                    raise ValueError(f"Minutes must be 0-59, got {minutes}")
                if not (0 <= seconds <= 59):
                    raise ValueError(f"Seconds must be 0-59, got {seconds}")
                
                duration_minutes = hours * 60 + minutes + (seconds // 60)
            
            # Validate final duration (5-1440 minutes)
            if duration_minutes < 5:
                raise ValueError(f"Duration must be at least 5 minutes, got {duration_minutes}")
            if duration_minutes > 1440:
                raise ValueError(f"Duration must be at most 1440 minutes (24 hours), got {duration_minutes}")
            
            _LOGGER.info(f"Parsed time_period {time_period} to {duration_minutes} minutes")
            
        except (ValueError, AttributeError, TypeError) as e:
            error_msg = f"Failed to parse time_period: {e}"
            _LOGGER.error(error_msg)
            raise vol.Invalid(error_msg)
        
        # Validate temperature if provided
        if temperature is not None:
            if not (30 <= temperature <= 80):
                error_msg = f"Temperature must be 30-80°C, got {temperature}"
                _LOGGER.error(error_msg)
                raise vol.Invalid(error_msg)
        
        # Call water heater entities
        for entity_id in entity_ids:
            water_heater_component = hass.data.get("entity_components", {}).get("water_heater")
            if water_heater_component:
                for ent in water_heater_component.entities:
                    if ent.entity_id == entity_id and hasattr(ent, 'async_set_timer'):
                        try:
                            await ent.async_set_timer(duration_minutes, temperature)
                            _LOGGER.info(f"Set timer for {entity_id}: {duration_minutes}min")
                        except Exception as e:
                            error_msg = f"Failed to set timer for {entity_id}: {e}"
                            _LOGGER.error(error_msg)
                            # Continue to next entity instead of failing completely
                        break
    
    async def handle_resume_schedule(call: ServiceCall):
        """Handle resume_schedule service call."""
        from .async_api import get_async_client
        
        entity_ids = call.data.get("entity_id", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        
        client = get_async_client(hass)
        
        for entity_id in entity_ids:
            domain = entity_id.split(".")[0]
            component = hass.data.get("entity_components", {}).get(domain)
            if component:
                for ent in component.entities:
                    if ent.entity_id == entity_id:
                        zone_id = getattr(ent, '_zone_id', None)
                        if zone_id:
                            await client.delete_zone_overlay(zone_id)
                            _LOGGER.info(f"Resumed schedule for {entity_id}")
                        break
    
    async def handle_set_temp_offset(call: ServiceCall):
        """Handle set_temperature_offset service call."""
        from .async_api import get_async_client
        
        entity_id = call.data.get("entity_id")
        offset = call.data.get("offset")
        
        client = get_async_client(hass)
        
        # Get zone_id from entity and find device serial
        climate_component = hass.data.get("entity_components", {}).get("climate")
        if climate_component:
            for ent in climate_component.entities:
                if ent.entity_id == entity_id:
                    zone_id = getattr(ent, '_zone_id', None)
                    if zone_id:
                        # Find device serial for this zone
                        serial = await hass.async_add_executor_job(
                            _get_device_serial_for_zone, zone_id
                        )
                        if serial:
                            await client.set_device_offset(serial, offset)
                            _LOGGER.info(f"Set offset {offset}°C for {entity_id}")
                    break
    
    async def handle_add_meter_reading(call: ServiceCall):
        """Handle add_meter_reading service call (fully async)."""
        from .async_api import get_async_client
        
        reading = call.data.get("reading")
        date = call.data.get("date")
        
        client = get_async_client(hass)
        success = await client.add_meter_reading(reading, date)
        
        if not success:
            _LOGGER.error(f"Failed to add meter reading: {reading}")
    
    # Register services
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CLIMATE_TIMER, handle_set_climate_timer,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_ids,
            vol.Required("temperature"): vol.Coerce(float),
            vol.Required("time_period"): cv.time_period,
            vol.Optional("overlay"): cv.string,
        })
    )
    
    hass.services.async_register(
        DOMAIN, SERVICE_SET_WATER_HEATER_TIMER, handle_set_water_heater_timer,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_ids,
            vol.Required("time_period"): cv.time_period,
            vol.Optional("temperature"): vol.Coerce(float),
        })
    )
    
    hass.services.async_register(
        DOMAIN, SERVICE_RESUME_SCHEDULE, handle_resume_schedule,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_ids,
        })
    )
    
    hass.services.async_register(
        DOMAIN, SERVICE_SET_TEMP_OFFSET, handle_set_temp_offset,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
            vol.Required("offset"): vol.Coerce(float),
        })
    )
    
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_METER_READING, handle_add_meter_reading,
        schema=vol.Schema({
            vol.Required("reading"): vol.Coerce(int),
            vol.Optional("date"): cv.string,
        })
    )
    
    async def handle_identify_device(call: ServiceCall):
        """Handle identify_device service call (fully async)."""
        from .async_api import get_async_client
        
        device_serial = call.data.get("device_serial")
        
        client = get_async_client(hass)
        success = await client.identify_device(device_serial)
        
        if not success:
            _LOGGER.error(f"Failed to identify device: {device_serial}")
    
    async def handle_set_away_config(call: ServiceCall):
        """Handle set_away_configuration service call (fully async)."""
        from .async_api import get_async_client
        
        entity_id = call.data.get("entity_id")
        mode = call.data.get("mode")
        temperature = call.data.get("temperature")
        comfort_level = call.data.get("comfort_level", 50)
        
        client = get_async_client(hass)
        
        # Get zone_id from entity
        climate_component = hass.data.get("entity_components", {}).get("climate")
        if climate_component:
            for ent in climate_component.entities:
                if ent.entity_id == entity_id:
                    zone_id = getattr(ent, '_zone_id', None)
                    if zone_id:
                        success = await client.set_away_configuration(
                            zone_id, mode, temperature, comfort_level
                        )
                        if not success:
                            _LOGGER.error(f"Failed to set away config for {entity_id}")
                    break
    
    hass.services.async_register(
        DOMAIN, SERVICE_IDENTIFY_DEVICE, handle_identify_device,
        schema=vol.Schema({
            vol.Required("device_serial"): cv.string,
        })
    )
    
    hass.services.async_register(
        DOMAIN, SERVICE_SET_AWAY_CONFIG, handle_set_away_config,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
            vol.Required("mode"): cv.string,
            vol.Optional("temperature"): vol.Coerce(float),
            vol.Optional("comfort_level"): vol.Coerce(int),
        })
    )
    
    async def handle_get_temp_offset(call: ServiceCall):
        """Handle get_temperature_offset service call.
        
        Fetches the current temperature offset for a climate entity on-demand.
        Returns the offset value via service response for use in automations.
        """
        from .async_api import get_async_client
        
        entity_id = call.data.get("entity_id")
        client = get_async_client(hass)
        
        # Get zone_id from entity
        climate_component = hass.data.get("entity_components", {}).get("climate")
        if climate_component:
            for ent in climate_component.entities:
                if ent.entity_id == entity_id:
                    zone_id = getattr(ent, '_zone_id', None)
                    if zone_id:
                        # Find device serial for this zone
                        serial = await hass.async_add_executor_job(
                            _get_device_serial_for_zone, zone_id
                        )
                        if serial:
                            result = await client.get_device_offset(serial)
                            if result is not None:
                                return {"offset_celsius": result}
                    
                    _LOGGER.error(f"Failed to get offset for {entity_id}")
                    return {"offset_celsius": None, "error": "Failed to fetch offset"}
        
        _LOGGER.error(f"Entity not found: {entity_id}")
        return {"offset_celsius": None, "error": "Entity not found"}
    
    hass.services.async_register(
        DOMAIN, SERVICE_GET_TEMP_OFFSET, handle_get_temp_offset,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
        }),
        supports_response=True,
    )
    
    _LOGGER.info("Tado CE: Services registered")


def _get_device_serial_for_zone(zone_id: str) -> str | None:
    """Get the first device serial for a zone.
    
    Args:
        zone_id: Zone ID to look up
        
    Returns:
        Device serial number, or None if not found
    """
    from .const import ZONES_INFO_FILE
    
    try:
        with open(ZONES_INFO_FILE) as f:
            zones_info = json.load(f)
        
        for zone in zones_info:
            if str(zone.get('id')) == zone_id:
                for device in zone.get('devices', []):
                    serial = device.get('shortSerialNo')
                    if serial:
                        return serial
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to get device serial for zone {zone_id}: {e}")
        return None


# NOTE: The following blocking functions have been replaced by async methods
# in async_api.py (v1.5.0+) and removed to enforce proper async architecture:
# - _get_access_token -> Use async_api.get_async_client().get_access_token()
# - _get_temperature_offset -> client.get_device_offset()
# - _set_temperature_offset -> client.set_device_offset()
# - _add_meter_reading -> client.add_meter_reading()
# - _identify_device -> client.identify_device()
# - _set_away_configuration -> client.set_away_configuration()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.
    
    CRITICAL: Must clean up all resources to prevent memory leaks on reload.
    """
    _LOGGER.info("Tado CE: Unloading integration...")
    
    # Cancel polling timer if active
    if DOMAIN in hass.data and 'polling_cancel' in hass.data[DOMAIN]:
        cancel_func = hass.data[DOMAIN]['polling_cancel']
        if cancel_func:
            cancel_func()
            _LOGGER.debug("Cancelled polling timer")
    
    # Clean up async client to prevent memory leak
    from .async_api import cleanup_async_client
    cleanup_async_client(hass)
    
    # Clean up immediate refresh handler
    from .immediate_refresh_handler import cleanup_handler
    cleanup_handler()
    
    # Clean up API call tracker executor to prevent thread leaks
    from .api_call_tracker import cleanup_executor
    cleanup_executor()
    
    # Build platform list to unload (same logic as setup)
    config_manager = hass.data.get(DOMAIN, {}).get('config_manager')
    platforms_to_unload = list(BASE_PLATFORMS)
    
    # v1.8.0: Add Calendar platform if it was loaded
    if CALENDAR_PLATFORM and config_manager and config_manager.get_schedule_calendar_enabled():
        platforms_to_unload.append(CALENDAR_PLATFORM)
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms_to_unload)
    
    # Clean up hass.data
    if unload_ok and DOMAIN in hass.data:
        hass.data.pop(DOMAIN, None)
        _LOGGER.debug("Cleaned up hass.data")
    
    _LOGGER.info("Tado CE: Integration unloaded successfully")
    return unload_ok
