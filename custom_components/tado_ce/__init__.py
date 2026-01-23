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

from .const import DOMAIN, DATA_DIR, CONFIG_FILE, RATELIMIT_FILE, TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID
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
    _LOGGER.warning("Platform.BUTTON not available - button entities will not be loaded")

SCRIPT_PATH = "/config/custom_components/tado_ce/tado_api.py"

# Service names
SERVICE_SET_CLIMATE_TIMER = "set_climate_timer"
SERVICE_SET_WATER_HEATER_TIMER = "set_water_heater_timer"
SERVICE_RESUME_SCHEDULE = "resume_schedule"
SERVICE_SET_TEMP_OFFSET = "set_climate_temperature_offset"  # Match official Tado integration
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
    _LOGGER.info("Migrating Tado CE config entry from version %s", config_entry.version)

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
        return True

    _LOGGER.info("Config entry already at version %s, no migration needed", config_entry.version)
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
    
    # Ensure data directory exists
    DATA_DIR.mkdir(exist_ok=True)
    
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
    
    # Load home_id early to avoid race conditions in device_manager
    from .device_manager import load_home_id
    await hass.async_add_executor_job(load_home_id)
    
    # Check if config file exists
    if not CONFIG_FILE.exists():
        _LOGGER.warning(
            "Tado CE config file not found. "
            "Run 'python3 /config/custom_components/tado_ce/tado_api.py auth' first."
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
    config_file_path = Path("/config/custom_components/tado_ce/data/config.json")
    _LOGGER.info(f"Tado CE: Checking config file at {config_file_path}, exists={config_file_path.exists()}")
    if config_file_path.exists():
        _LOGGER.info("Tado CE: Starting initial sync...")
        await hass.async_add_executor_job(sync_tado)
        _LOGGER.info("Tado CE: Initial sync completed")
    else:
        # Still schedule polling even without config
        _LOGGER.warning(f"Tado CE: Config file not found at {config_file_path}, scheduling polling only")
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
                # Get the climate entity and call set_timer
                climate_entity = hass.data.get("entity_components", {}).get("climate")
                if climate_entity:
                    for ent in climate_entity.entities:
                        if ent.entity_id == entity_id and hasattr(ent, 'set_timer'):
                            try:
                                await hass.async_add_executor_job(
                                    ent.set_timer, temperature, duration_minutes, overlay
                                )
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
                    if ent.entity_id == entity_id and hasattr(ent, 'set_timer'):
                        try:
                            await hass.async_add_executor_job(ent.set_timer, duration_minutes, temperature)
                            _LOGGER.info(f"Set timer for {entity_id}: {duration_minutes}min")
                        except Exception as e:
                            error_msg = f"Failed to set timer for {entity_id}: {e}"
                            _LOGGER.error(error_msg)
                            # Continue to next entity instead of failing completely
                        break
    
    async def handle_resume_schedule(call: ServiceCall):
        """Handle resume_schedule service call."""
        entity_ids = call.data.get("entity_id", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        
        for entity_id in entity_ids:
            domain = entity_id.split(".")[0]
            component = hass.data.get("entity_components", {}).get(domain)
            if component:
                for ent in component.entities:
                    if ent.entity_id == entity_id:
                        if hasattr(ent, '_delete_overlay'):
                            await hass.async_add_executor_job(ent._delete_overlay)
                        elif hasattr(ent, 'resume_schedule'):
                            await hass.async_add_executor_job(ent.resume_schedule)
                        break
    
    async def handle_set_temp_offset(call: ServiceCall):
        """Handle set_temperature_offset service call."""
        entity_id = call.data.get("entity_id")
        offset = call.data.get("offset")
        
        # Get zone_id from entity
        climate_component = hass.data.get("entity_components", {}).get("climate")
        if climate_component:
            for ent in climate_component.entities:
                if ent.entity_id == entity_id:
                    # Find device serial for this zone
                    await hass.async_add_executor_job(
                        _set_temperature_offset, ent._zone_id, offset
                    )
                    break
    
    async def handle_add_meter_reading(call: ServiceCall):
        """Handle add_meter_reading service call."""
        reading = call.data.get("reading")
        date = call.data.get("date")
        
        await hass.async_add_executor_job(_add_meter_reading, reading, date)
    
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
        """Handle identify_device service call."""
        device_serial = call.data.get("device_serial")
        await hass.async_add_executor_job(_identify_device, device_serial)
    
    async def handle_set_away_config(call: ServiceCall):
        """Handle set_away_configuration service call."""
        entity_id = call.data.get("entity_id")
        mode = call.data.get("mode")
        temperature = call.data.get("temperature")
        comfort_level = call.data.get("comfort_level", 50)
        
        # Get zone_id from entity
        climate_component = hass.data.get("entity_components", {}).get("climate")
        if climate_component:
            for ent in climate_component.entities:
                if ent.entity_id == entity_id:
                    await hass.async_add_executor_job(
                        _set_away_configuration, ent._zone_id, mode, temperature, comfort_level
                    )
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
    
    _LOGGER.info("Tado CE: Services registered")


def _get_access_token():
    """Get access token using centralized AuthManager."""
    auth_manager = get_auth_manager(CONFIG_FILE, CLIENT_ID, TADO_AUTH_URL)
    return auth_manager.get_access_token()


def _set_temperature_offset(zone_id: str, offset: float):
    """Set temperature offset for devices in a zone."""
    from urllib.request import Request, urlopen
    from .const import ZONES_INFO_FILE
    
    try:
        # Find device serial for this zone
        with open(ZONES_INFO_FILE) as f:
            zones_info = json.load(f)
        
        for zone in zones_info:
            if str(zone.get('id')) == zone_id:
                for device in zone.get('devices', []):
                    serial = device.get('shortSerialNo')
                    if serial:
                        token = _get_access_token()
                        if not token:
                            return False
                        
                        url = f"https://my.tado.com/api/v2/devices/{serial}/temperatureOffset"
                        payload = {"celsius": offset}
                        
                        data = json.dumps(payload).encode()
                        req = Request(url, data=data, method="PUT")
                        req.add_header("Authorization", f"Bearer {token}")
                        req.add_header("Content-Type", "application/json")
                        
                        with urlopen(req, timeout=10) as resp:
                            _LOGGER.info(f"Set temperature offset {offset}°C for device {serial}")
                break
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set temperature offset: {e}")
        return False


def _add_meter_reading(reading: int, date: str = None):
    """Add energy meter reading."""
    from urllib.request import Request, urlopen
    
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        
        home_id = config.get("home_id")
        if not home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        token = _get_access_token()
        if not token:
            return False
        
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        
        url = f"{TADO_API_BASE}/homes/{home_id}/meterReadings"
        payload = {
            "date": date,
            "reading": reading
        }
        
        data = json.dumps(payload).encode()
        req = Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        
        with urlopen(req, timeout=10) as resp:
            _LOGGER.info(f"Added meter reading: {reading} on {date}")
            return True
            
    except Exception as e:
        _LOGGER.error(f"Failed to add meter reading: {e}")
        return False


def _identify_device(device_serial: str):
    """Make a device flash its LED to identify it."""
    from urllib.request import Request, urlopen
    
    try:
        token = _get_access_token()
        if not token:
            _LOGGER.error("Failed to get access token")
            return False
        
        url = f"https://my.tado.com/api/v2/devices/{device_serial}/identify"
        req = Request(url, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        
        with urlopen(req, timeout=10) as resp:
            _LOGGER.info(f"Identify command sent to device {device_serial}")
            return True
            
    except Exception as e:
        _LOGGER.error(f"Failed to identify device: {e}")
        return False


def _set_away_configuration(zone_id: str, mode: str, temperature: float = None, comfort_level: int = 50):
    """Set away configuration for a zone."""
    from urllib.request import Request, urlopen
    
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        
        home_id = config.get("home_id")
        if not home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        token = _get_access_token()
        if not token:
            return False
        
        url = f"{TADO_API_BASE}/homes/{home_id}/zones/{zone_id}/schedule/awayConfiguration"
        
        if mode == "auto":
            payload = {
                "type": "HEATING",
                "autoAdjust": True,
                "comfortLevel": comfort_level,
                "setting": {"type": "HEATING", "power": "OFF"}
            }
        elif mode == "manual" and temperature:
            payload = {
                "type": "HEATING",
                "autoAdjust": False,
                "setting": {
                    "type": "HEATING",
                    "power": "ON",
                    "temperature": {"celsius": temperature}
                }
            }
        else:  # off
            payload = {
                "type": "HEATING",
                "autoAdjust": False,
                "setting": {"type": "HEATING", "power": "OFF"}
            }
        
        data = json.dumps(payload).encode()
        req = Request(url, data=data, method="PUT")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        
        with urlopen(req, timeout=10) as resp:
            _LOGGER.info(f"Set away configuration for zone {zone_id}: {mode}")
            return True
            
    except Exception as e:
        _LOGGER.error(f"Failed to set away configuration: {e}")
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
