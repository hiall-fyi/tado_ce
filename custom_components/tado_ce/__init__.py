"""Tado CE Integration."""
import json
import logging
from datetime import datetime, timedelta
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

_LOGGER = logging.getLogger(__name__)

# Platform.BUTTON was added in Home Assistant 2021.12
# For backward compatibility, check if it exists
try:
    PLATFORMS = [Platform.SENSOR, Platform.CLIMATE, Platform.BINARY_SENSOR, Platform.WATER_HEATER, Platform.DEVICE_TRACKER, Platform.SWITCH, Platform.BUTTON]
except AttributeError:
    # Older Home Assistant version without Platform.BUTTON
    PLATFORMS = [Platform.SENSOR, Platform.CLIMATE, Platform.BINARY_SENSOR, Platform.WATER_HEATER, Platform.DEVICE_TRACKER, Platform.SWITCH]
    _LOGGER.debug("Platform.BUTTON not available - button entities will not be loaded")

# Script path - use relative path from this file's location
SCRIPT_PATH = str(Path(__file__).parent / "tado_api.py")

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
    """
    hour = datetime.now().hour
    day_start = config_manager.get_day_start_hour()
    night_start = config_manager.get_night_start_hour()
    
    # Uniform mode: if day_start == night_start, always use day interval
    if day_start == night_start:
        return True
    
    return day_start <= hour < night_start


def get_polling_interval(config_manager: ConfigurationManager) -> int:
    """Get polling interval based on configuration and API rate limit.
    
    Args:
        config_manager: Configuration manager with polling settings
        
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
        elif RATELIMIT_FILE.exists():
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
    """Migrate old entry to new version."""
    _LOGGER.info("Migrating Tado CE config entry from version %s to version 5", config_entry.version)
    
    # v1.5.2: Migrate data directory from custom_components/tado_ce/data/ to .storage/tado_ce/
    from .const import LEGACY_DATA_DIR
    if LEGACY_DATA_DIR.exists() and not DATA_DIR.exists():
        _LOGGER.info("Migrating data directory from legacy location to .storage/tado_ce/")
        import shutil
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for file in LEGACY_DATA_DIR.glob("*.json"):
            try:
                shutil.copy2(file, DATA_DIR / file.name)
                _LOGGER.info(f"Migrated {file.name} to new location")
            except Exception as e:
                _LOGGER.error(f"Failed to migrate {file.name}: {e}")
        # Copy log file too if exists
        legacy_log = LEGACY_DATA_DIR / "api.log"
        if legacy_log.exists():
            try:
                shutil.copy2(legacy_log, DATA_DIR / "api.log")
            except Exception:
                pass  # Log file is not critical
        _LOGGER.info("Data directory migration complete")

    if config_entry.version == 1:
        # Version 1 (v1.1.0) -> 2 (v1.2.0): Handle zone-based device migration
        _LOGGER.info("Migrating from v1.1.0 to v1.2.0 format")
        
        # Ensure data directory exists
        DATA_DIR.mkdir(exist_ok=True)
        
        # Check if zones_info.json exists, if not, trigger a full sync
        from .const import ZONES_INFO_FILE
        if not ZONES_INFO_FILE.exists():
            _LOGGER.warning("zones_info.json missing - will be created on first sync")
            # Don't fail migration - let the sync create it
        
        # Update to version 2
        hass.config_entries.async_update_entry(config_entry, version=2)
        _LOGGER.info("Migration to version 2 successful")
        # Fall through to continue migration to version 4

    if config_entry.version in (2, 3):
        # Version 2/3 -> 4 (v1.4.0): New device authorization flow
        # The existing config.json with refresh_token is still valid
        # We just need to update the config entry version
        _LOGGER.info("Migrating from version %s to version 4 (v1.4.0)", config_entry.version)
        
        # Ensure data directory exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        # Check if config.json exists with valid refresh_token
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                
                if config.get("refresh_token"):
                    _LOGGER.info("Existing refresh_token found - authentication should work")
                else:
                    _LOGGER.warning(
                        "No refresh_token in config.json - re-authentication may be required. "
                        "If entities are unavailable, delete and re-add the integration."
                    )
            except Exception as e:
                _LOGGER.warning(f"Could not read config.json: {e}")
        else:
            _LOGGER.warning(
                "config.json not found - re-authentication required. "
                "Delete and re-add the integration to authenticate."
            )
        
        # Update to version 4, then fall through to version 5 migration
        hass.config_entries.async_update_entry(config_entry, version=4)
        _LOGGER.info("Migration to version 4 successful")
        # Fall through to version 5 migration

    if config_entry.version == 4:
        # Version 4 -> 5 (v1.5.2): Data directory moved to .storage/tado_ce/
        _LOGGER.info("Migrating from version 4 to version 5 (v1.5.2)")
        
        # Data migration already handled at the top of this function
        # Just update the version
        hass.config_entries.async_update_entry(config_entry, version=5)
        _LOGGER.info("Migration to version 5 successful")
        return True

    if config_entry.version == 5:
        _LOGGER.info("Config entry already at version 5, no migration needed")
        return True

    _LOGGER.warning("Unknown config entry version %s, attempting to continue", config_entry.version)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tado CE from a config entry."""
    _LOGGER.info("Tado CE: Integration loading...")
    
    # CRITICAL: Check for duplicate entries and remove old ones (v1.1.0 leftovers)
    # This must be done BEFORE any setup to avoid race conditions
    all_entries = hass.config_entries.async_entries(DOMAIN)
    if len(all_entries) > 1:
        _LOGGER.warning(f"Found {len(all_entries)} Tado CE entries - checking for duplicates")
        
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
                await hass.config_entries.async_remove(old_entry.entry_id)
                _LOGGER.info(f"Successfully removed duplicate entry {old_entry.entry_id}")
            
            # Verify cleanup
            _LOGGER.info(f"Duplicate cleanup complete. Keeper: {keeper_entry_id}")
    
    # v1.5.2: Migrate data from legacy location if needed
    # This handles cases where migration didn't run (e.g., fresh install with old data)
    from .const import LEGACY_DATA_DIR
    if LEGACY_DATA_DIR.exists() and not DATA_DIR.exists():
        _LOGGER.info("Migrating data directory from legacy location to .storage/tado_ce/")
        import shutil
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for file in LEGACY_DATA_DIR.glob("*.json"):
            try:
                shutil.copy2(file, DATA_DIR / file.name)
                _LOGGER.info(f"Migrated {file.name} to new location")
            except Exception as e:
                _LOGGER.error(f"Failed to migrate {file.name}: {e}")
        _LOGGER.info("Data directory migration complete")
    
    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    # Initialize configuration manager
    config_manager = ConfigurationManager(entry, hass)
    _LOGGER.info(f"Configuration loaded: {config_manager.get_all_config()}")
    
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
            f"Run 'python3 {SCRIPT_PATH} auth' first."
        )
    
    # Track current interval and last full sync time
    current_interval = [0]
    cancel_interval = [None]
    last_full_sync = [None]
    
    def schedule_next_sync():
        """Schedule next sync with dynamic interval."""
        new_interval = get_polling_interval(config_manager)
        
        if new_interval != current_interval[0]:
            time_period = "day" if is_daytime(config_manager) else "night"
            _LOGGER.info(f"Tado CE: Polling interval set to {new_interval}m ({time_period})")
            current_interval[0] = new_interval
        
        # Cancel old interval
        if cancel_interval[0]:
            cancel_interval[0]()
        
        # Schedule new interval
        async def async_sync_wrapper(now):
            """Async wrapper for sync_tado."""
            await hass.async_add_executor_job(sync_tado)
        
        cancel_interval[0] = async_track_time_interval(
            hass,
            async_sync_wrapper,
            timedelta(minutes=new_interval)
        )
        
        # Store cancel function in hass.data so we can cancel on reload
        hass.data[DOMAIN]['polling_cancel'] = cancel_interval[0]
    
    def sync_tado(now=None):
        """Run Tado sync script."""
        import subprocess
        
        # Check if polling should be paused due to Test Mode limit
        if config_manager.get_test_mode_enabled():
            try:
                if RATELIMIT_FILE.exists():
                    with open(RATELIMIT_FILE) as f:
                        data = json.load(f)
                        used = data.get("used", 0)
                        if used >= 100:
                            _LOGGER.warning(
                                f"Tado CE: Test Mode limit reached ({used}/100 calls). "
                                "Polling paused until quota resets."
                            )
                            # Re-schedule to check again later
                            schedule_next_sync()
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
        _LOGGER.info(f"Tado CE: Executing {sync_type} sync")
        
        try:
            cmd = ["python3", SCRIPT_PATH, "sync"]
            if not do_full_sync:
                cmd.append("--quick")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                _LOGGER.info(f"Tado CE {sync_type} sync SUCCESS")
                if do_full_sync:
                    last_full_sync[0] = datetime.now()
            else:
                _LOGGER.warning(f"Tado CE sync: {result.stdout} {result.stderr}")
        except Exception as e:
            _LOGGER.error(f"Tado CE sync ERROR: {e}")
        
        # Re-schedule with potentially new interval (day/night change)
        schedule_next_sync()
    
    # Initial sync (only if config exists)
    _LOGGER.info(f"Tado CE: Checking config file at {CONFIG_FILE}, exists={CONFIG_FILE.exists()}")
    if CONFIG_FILE.exists():
        _LOGGER.info("Tado CE: Starting initial sync...")
        await hass.async_add_executor_job(sync_tado)
        _LOGGER.info("Tado CE: Initial sync completed")
    else:
        # Still schedule polling even without config
        _LOGGER.warning(f"Tado CE: Config file not found at {CONFIG_FILE}, scheduling polling only")
        schedule_next_sync()
        _LOGGER.info("Tado CE: Polling scheduled")
    
    # Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
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
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # Clean up hass.data
    if unload_ok and DOMAIN in hass.data:
        hass.data.pop(DOMAIN, None)
        _LOGGER.debug("Cleaned up hass.data")
    
    _LOGGER.info("Tado CE: Integration unloaded successfully")
    return unload_ok
