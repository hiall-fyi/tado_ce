"""Configuration Manager for Tado CE Integration.

Manages user configuration settings stored in Home Assistant config entry.
"""
import json
import logging
import tempfile
import shutil
import threading
from pathlib import Path
from typing import Optional, Dict, Tuple
from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Global lock for thread-safe config file writes
_config_write_lock = threading.Lock()

# Default configuration values
DEFAULT_WEATHER_ENABLED = False
DEFAULT_MOBILE_DEVICES_ENABLED = False
DEFAULT_TEST_MODE_ENABLED = False
DEFAULT_DAY_START_HOUR = 7
DEFAULT_NIGHT_START_HOUR = 23
DEFAULT_API_HISTORY_RETENTION_DAYS = 14  # 0 = keep forever
DEFAULT_HOT_WATER_TIMER_DURATION = 60  # minutes

# Validation constants
MIN_HOUR = 0
MAX_HOUR = 23
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440  # 24 hours
MIN_RETENTION_DAYS = 0  # 0 = forever
MAX_RETENTION_DAYS = 365
MIN_TIMER_DURATION = 5  # minutes
MAX_TIMER_DURATION = 1440  # 24 hours

# Config file path
CONFIG_FILE = Path("/config/custom_components/tado_ce/data/config.json")


class ConfigurationManager:
    """Manages configuration settings for Tado CE integration."""
    
    def __init__(self, config_entry: ConfigEntry, hass=None):
        """Initialize configuration manager with config entry.
        
        Args:
            config_entry: Home Assistant config entry containing user settings
            hass: Home Assistant instance (optional, for async file operations)
        """
        self._config_entry = config_entry
        self._options = config_entry.options if config_entry.options else {}
        self._hass = hass
        # Don't sync on init to avoid blocking - will be synced when needed
    
    @staticmethod
    def validate_hour(hour: int, field_name: str) -> Tuple[bool, Optional[str]]:
        """Validate hour value (0-23).
        
        Args:
            hour: Hour value to validate
            field_name: Name of the field for error messages
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not isinstance(hour, int):
            return False, f"{field_name} must be an integer"
        
        if hour < MIN_HOUR or hour > MAX_HOUR:
            return False, f"{field_name} must be between {MIN_HOUR} and {MAX_HOUR}"
        
        return True, None
    
    @staticmethod
    def validate_interval(interval: Optional[int], field_name: str) -> Tuple[bool, Optional[str]]:
        """Validate polling interval (1-1440 minutes or None).
        
        Args:
            interval: Interval value to validate
            field_name: Name of the field for error messages
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if interval is None:
            return True, None
        
        if not isinstance(interval, int):
            return False, f"{field_name} must be an integer or null"
        
        if interval < MIN_INTERVAL_MINUTES or interval > MAX_INTERVAL_MINUTES:
            return False, f"{field_name} must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes"
        
        return True, None
    
    @staticmethod
    def validate_retention_days(days: int) -> Tuple[bool, Optional[str]]:
        """Validate retention days (0-365).
        
        Args:
            days: Retention days to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not isinstance(days, int):
            return False, "api_history_retention_days must be an integer"
        
        if days < MIN_RETENTION_DAYS or days > MAX_RETENTION_DAYS:
            return False, f"api_history_retention_days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"
        
        return True, None
    
    @staticmethod
    def validate_day_night_hours(day_start: int, night_start: int) -> Tuple[bool, Optional[str]]:
        """Validate day/night hour combination.
        
        Args:
            day_start: Day start hour
            night_start: Night start hour
            
        Returns:
            Tuple of (is_valid, error_message)
            
        Note:
            day_start == night_start is valid (uniform polling mode)
        """
        # Validate individual hours first
        valid, error = ConfigurationManager.validate_hour(day_start, "day_start_hour")
        if not valid:
            return False, error
        
        valid, error = ConfigurationManager.validate_hour(night_start, "night_start_hour")
        if not valid:
            return False, error
        
        # Both hours are valid (same value = uniform mode, which is allowed)
        return True, None
    
    def validate_config_updates(self, updates: Dict) -> Tuple[bool, Optional[str]]:
        """Validate configuration updates before applying.
        
        Args:
            updates: Dictionary of configuration updates
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Get current values for validation
        current_day_start = self.get_day_start_hour()
        current_night_start = self.get_night_start_hour()
        
        # Check day_start_hour
        if 'day_start_hour' in updates:
            valid, error = self.validate_hour(updates['day_start_hour'], 'day_start_hour')
            if not valid:
                return False, error
            current_day_start = updates['day_start_hour']
        
        # Check night_start_hour
        if 'night_start_hour' in updates:
            valid, error = self.validate_hour(updates['night_start_hour'], 'night_start_hour')
            if not valid:
                return False, error
            current_night_start = updates['night_start_hour']
        
        # Validate day/night combination
        if 'day_start_hour' in updates or 'night_start_hour' in updates:
            valid, error = self.validate_day_night_hours(current_day_start, current_night_start)
            if not valid:
                return False, error
        
        # Check custom_day_interval
        if 'custom_day_interval' in updates:
            valid, error = self.validate_interval(updates['custom_day_interval'], 'custom_day_interval')
            if not valid:
                return False, error
        
        # Check custom_night_interval
        if 'custom_night_interval' in updates:
            valid, error = self.validate_interval(updates['custom_night_interval'], 'custom_night_interval')
            if not valid:
                return False, error
        
        # Check api_history_retention_days
        if 'api_history_retention_days' in updates:
            valid, error = self.validate_retention_days(updates['api_history_retention_days'])
            if not valid:
                return False, error
        
        # Check boolean fields
        for field in ['weather_enabled', 'mobile_devices_enabled', 'test_mode_enabled']:
            if field in updates and not isinstance(updates[field], bool):
                return False, f"{field} must be a boolean"
        
        return True, None
    
    def get_weather_enabled(self) -> bool:
        """Check if weather sensors are enabled.
        
        Returns:
            True if weather sensors should be created, False otherwise
        """
        return self._options.get('weather_enabled', DEFAULT_WEATHER_ENABLED)
    
    def get_mobile_devices_enabled(self) -> bool:
        """Check if mobile device tracking is enabled.
        
        Returns:
            True if mobile device tracking should be active, False otherwise
        """
        return self._options.get('mobile_devices_enabled', DEFAULT_MOBILE_DEVICES_ENABLED)
    
    def get_test_mode_enabled(self) -> bool:
        """Check if Test Mode is enabled (enforce 100 API limit).
        
        Returns:
            True if Test Mode is active, False otherwise
        """
        return self._options.get('test_mode_enabled', DEFAULT_TEST_MODE_ENABLED)
    
    def get_day_start_hour(self) -> int:
        """Get configured day start hour (default 7am).
        
        Returns:
            Hour (0-23) when day period starts
        """
        hour = self._options.get('day_start_hour', DEFAULT_DAY_START_HOUR)
        # Validate range
        if not isinstance(hour, int) or hour < 0 or hour > 23:
            _LOGGER.warning(f"Invalid day_start_hour: {hour}, using default {DEFAULT_DAY_START_HOUR}")
            return DEFAULT_DAY_START_HOUR
        return hour
    
    def get_night_start_hour(self) -> int:
        """Get configured night start hour (default 11pm).
        
        Returns:
            Hour (0-23) when night period starts
        """
        hour = self._options.get('night_start_hour', DEFAULT_NIGHT_START_HOUR)
        # Validate range
        if not isinstance(hour, int) or hour < 0 or hour > 23:
            _LOGGER.warning(f"Invalid night_start_hour: {hour}, using default {DEFAULT_NIGHT_START_HOUR}")
            return DEFAULT_NIGHT_START_HOUR
        return hour
    
    def get_custom_day_interval(self) -> Optional[int]:
        """Get custom day polling interval in minutes.
        
        Returns:
            Polling interval in minutes (1-1440), or None if not configured
        """
        interval = self._options.get('custom_day_interval')
        if interval is None:
            return None
        
        # Validate range
        if not isinstance(interval, int) or interval < 1 or interval > 1440:
            _LOGGER.warning(f"Invalid custom_day_interval: {interval}, ignoring")
            return None
        return interval
    
    def get_custom_night_interval(self) -> Optional[int]:
        """Get custom night polling interval in minutes.
        
        Returns:
            Polling interval in minutes (1-1440), or None if not configured
        """
        interval = self._options.get('custom_night_interval')
        if interval is None:
            return None
        
        # Validate range
        if not isinstance(interval, int) or interval < 1 or interval > 1440:
            _LOGGER.warning(f"Invalid custom_night_interval: {interval}, ignoring")
            return None
        return interval
    
    def get_api_history_retention_days(self) -> int:
        """Get API call history retention period in days.
        
        Returns:
            Number of days to retain history (0 = keep forever, default 14)
        """
        days = self._options.get('api_history_retention_days', DEFAULT_API_HISTORY_RETENTION_DAYS)
        # Validate range
        if not isinstance(days, int) or days < 0 or days > 365:
            _LOGGER.warning(f"Invalid api_history_retention_days: {days}, using default {DEFAULT_API_HISTORY_RETENTION_DAYS}")
            return DEFAULT_API_HISTORY_RETENTION_DAYS
        return days
    
    def get_hot_water_timer_duration(self) -> int:
        """Get hot water timer duration in minutes.
        
        Returns:
            Timer duration in minutes (5-1440, default 60)
        """
        duration = self._options.get('hot_water_timer_duration', DEFAULT_HOT_WATER_TIMER_DURATION)
        # Validate range
        if not isinstance(duration, int) or duration < MIN_TIMER_DURATION or duration > MAX_TIMER_DURATION:
            _LOGGER.warning(f"Invalid hot_water_timer_duration: {duration}, using default {DEFAULT_HOT_WATER_TIMER_DURATION}")
            return DEFAULT_HOT_WATER_TIMER_DURATION
        return duration
    
    def sync_all_to_config_json(self) -> None:
        """Sync all configuration values to config.json for tado_api.py to read.
        
        This is a synchronous method that should be called from executor job.
        Uses atomic write to prevent corruption.
        
        CRITICAL: This method NEVER overwrites refresh_token or home_id.
        These are managed by auth_manager and tado_api.py respectively.
        
        Thread-safe: Uses global lock to prevent concurrent write corruption.
        """
        # CRITICAL: Lock entire operation to prevent race conditions
        with _config_write_lock:
            config_data = {
                'weather_enabled': self.get_weather_enabled(),
                'mobile_devices_enabled': self.get_mobile_devices_enabled(),
                'test_mode_enabled': self.get_test_mode_enabled(),
                'day_start_hour': self.get_day_start_hour(),
                'night_start_hour': self.get_night_start_hour(),
                'custom_day_interval': self.get_custom_day_interval(),
                'custom_night_interval': self.get_custom_night_interval(),
                'api_history_retention_days': self.get_api_history_retention_days(),
                'hot_water_timer_duration': self.get_hot_water_timer_duration(),
            }
            
            temp_path = None
            
            try:
                # Load existing config
                if CONFIG_FILE.exists():
                    try:
                        with open(CONFIG_FILE, 'r') as f:
                            existing_config = json.load(f)
                        
                        # Validate structure
                        if not isinstance(existing_config, dict):
                            raise ValueError("Config must be a dictionary")
                            
                    except (json.JSONDecodeError, ValueError) as e:
                        _LOGGER.error(f"Corrupt config.json detected: {e}. Creating backup and resetting.")
                        # Backup corrupt file
                        backup_path = CONFIG_FILE.with_suffix('.json.corrupt')
                        shutil.copy(CONFIG_FILE, backup_path)
                        _LOGGER.info(f"Corrupt config backed up to {backup_path}")
                        existing_config = {}
                else:
                    existing_config = {}
                
                # CRITICAL: Preserve refresh_token and home_id
                # These are managed by auth_manager and tado_api.py, NOT by config_manager
                preserved_refresh_token = existing_config.get('refresh_token')
                preserved_home_id = existing_config.get('home_id')
                
                # Merge with existing config
                existing_config.update(config_data)
                
                # CRITICAL: Restore preserved values (never overwrite with None)
                if preserved_refresh_token is not None:
                    existing_config['refresh_token'] = preserved_refresh_token
                elif 'refresh_token' not in existing_config:
                    # Only set to None if it doesn't exist at all
                    existing_config['refresh_token'] = None
                
                if preserved_home_id is not None:
                    existing_config['home_id'] = preserved_home_id
                elif 'home_id' not in existing_config:
                    # Only set to None if it doesn't exist at all
                    existing_config['home_id'] = None
                
                # Atomic write: write to temp file first, then rename
                CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                
                with tempfile.NamedTemporaryFile(
                    mode='w',
                    dir=CONFIG_FILE.parent,
                    delete=False,
                    suffix='.tmp'
                ) as tmp_file:
                    json.dump(existing_config, tmp_file, indent=2)
                    tmp_file.flush()
                    # CRITICAL FIX: Store temp path before closing
                    temp_path = tmp_file.name
                
                # CRITICAL FIX: Verify temp file was created successfully
                if not Path(temp_path).exists():
                    raise IOError(f"Temp file was not created: {temp_path}")
                
                # Verify temp file size is reasonable (not empty, not too large)
                temp_size = Path(temp_path).stat().st_size
                if temp_size == 0:
                    raise IOError("Temp file is empty")
                if temp_size > 1024 * 1024:  # 1MB limit
                    raise IOError(f"Temp file too large: {temp_size} bytes")
                
                # Atomic rename (POSIX guarantees atomicity)
                shutil.move(temp_path, CONFIG_FILE)
                
                # CRITICAL FIX: Verify final file exists and is valid JSON
                if not CONFIG_FILE.exists():
                    raise IOError("Config file was not created after move")
                
                with open(CONFIG_FILE, 'r') as f:
                    json.load(f)  # Verify JSON is valid
                    
                _LOGGER.debug("Configuration synced to config.json (atomic write verified)")
                
            except Exception as e:
                _LOGGER.error(f"Failed to sync configuration to config.json: {e}")
                
                # CRITICAL FIX: Clean up temp file if it exists
                if temp_path and Path(temp_path).exists():
                    try:
                        Path(temp_path).unlink()
                        _LOGGER.debug(f"Cleaned up temp file: {temp_path}")
                    except OSError as cleanup_error:
                        _LOGGER.error(f"Failed to cleanup temp file {temp_path}: {cleanup_error}")
                
                # CRITICAL FIX: Re-raise to notify caller
                raise
    
    async def async_sync_all_to_config_json(self) -> None:
        """Async wrapper to sync configuration to config.json."""
        if self._hass:
            await self._hass.async_add_executor_job(self.sync_all_to_config_json)
        else:
            # Fallback to sync if no hass instance
            self.sync_all_to_config_json()
    
    async def async_update_config(self, updates: dict) -> Tuple[bool, Optional[str]]:
        """Update configuration with new values (async).
        
        Args:
            updates: Dictionary of configuration keys and values to update
            
        Returns:
            Tuple of (success, error_message)
        """
        # Validate updates first
        valid, error = self.validate_config_updates(updates)
        if not valid:
            _LOGGER.error(f"Configuration validation failed: {error}")
            return False, error
        
        try:
            # Merge updates with existing options
            new_options = {**self._options, **updates}
            
            # Update the config entry (this is async)
            self._hass.config_entries.async_update_entry(
                self._config_entry,
                options=new_options
            )
            self._options = new_options
            
            # Sync to config.json (async)
            await self.async_sync_all_to_config_json()
            
            _LOGGER.info(f"Configuration updated: {list(updates.keys())}")
            return True, None
        except Exception as e:
            error_msg = f"Failed to update configuration: {e}"
            _LOGGER.error(error_msg)
            return False, error_msg
    
    def update_config(self, updates: dict) -> Tuple[bool, Optional[str]]:
        """Update configuration with new values (sync - deprecated).
        
        DEPRECATED: Use async_update_config() instead.
        This method is kept for backward compatibility but should not be used.
        
        Args:
            updates: Dictionary of configuration keys and values to update
            
        Returns:
            Tuple of (success, error_message)
        """
        _LOGGER.warning("update_config() is deprecated, use async_update_config() instead")
        
        # Validate updates first
        valid, error = self.validate_config_updates(updates)
        if not valid:
            _LOGGER.error(f"Configuration validation failed: {error}")
            return False, error
        
        try:
            # Merge updates with existing options
            new_options = {**self._options, **updates}
            self._options = new_options
            
            # Sync to config.json only (can't update config entry synchronously)
            self.sync_all_to_config_json()
            
            _LOGGER.warning("Configuration synced to file only, config entry not updated (use async_update_config)")
            return True, None
        except Exception as e:
            error_msg = f"Failed to update configuration: {e}"
            _LOGGER.error(error_msg)
            return False, error_msg
    
    def get_all_config(self) -> dict:
        """Get all configuration values.
        
        Returns:
            Dictionary containing all configuration settings
        """
        return {
            'weather_enabled': self.get_weather_enabled(),
            'mobile_devices_enabled': self.get_mobile_devices_enabled(),
            'test_mode_enabled': self.get_test_mode_enabled(),
            'day_start_hour': self.get_day_start_hour(),
            'night_start_hour': self.get_night_start_hour(),
            'custom_day_interval': self.get_custom_day_interval(),
            'custom_night_interval': self.get_custom_night_interval(),
            'api_history_retention_days': self.get_api_history_retention_days(),
            'hot_water_timer_duration': self.get_hot_water_timer_duration(),
        }
