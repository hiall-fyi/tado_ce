"""Centralized Data Loader for Tado CE Integration.

This module provides thread-safe file loading helpers for all Tado CE components.
All file I/O is blocking and should be called via hass.async_add_executor_job().
"""
import json
import logging
from typing import Optional

from .const import (
    ZONES_FILE, ZONES_INFO_FILE, WEATHER_FILE, MOBILE_DEVICES_FILE,
    CONFIG_FILE, RATELIMIT_FILE, HOME_STATE_FILE, OFFSETS_FILE,
    AC_CAPABILITIES_FILE, API_CALL_HISTORY_FILE
)

_LOGGER = logging.getLogger(__name__)


def load_zones_file() -> Optional[dict]:
    """Load zones.json (zone states).
    
    Returns:
        Zone states dict, or None if file doesn't exist or is invalid.
    """
    try:
        with open(ZONES_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("zones.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in zones.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load zones.json: {e}")
        return None


def load_zones_info_file() -> Optional[list]:
    """Load zones_info.json (zone metadata).
    
    Returns:
        List of zone info dicts, or None if file doesn't exist or is invalid.
    """
    try:
        with open(ZONES_INFO_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("zones_info.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in zones_info.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load zones_info.json: {e}")
        return None


def load_weather_file() -> Optional[dict]:
    """Load weather.json.
    
    Returns:
        Weather data dict, or None if file doesn't exist or is invalid.
    """
    try:
        with open(WEATHER_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("weather.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in weather.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load weather.json: {e}")
        return None


def load_mobile_devices_file() -> Optional[list]:
    """Load mobile_devices.json.
    
    Returns:
        List of mobile device dicts, or None if file doesn't exist or is invalid.
    """
    try:
        with open(MOBILE_DEVICES_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("mobile_devices.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in mobile_devices.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load mobile_devices.json: {e}")
        return None


def load_config_file() -> Optional[dict]:
    """Load config.json.
    
    Returns:
        Config dict, or None if file doesn't exist or is invalid.
    """
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("config.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in config.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load config.json: {e}")
        return None


def load_home_state_file() -> Optional[dict]:
    """Load home_state.json.
    
    Returns:
        Home state dict, or None if file doesn't exist or is invalid.
    """
    try:
        with open(HOME_STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("home_state.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in home_state.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load home_state.json: {e}")
        return None


def load_ratelimit_file() -> Optional[dict]:
    """Load ratelimit.json.
    
    Returns:
        Rate limit data dict, or None if file doesn't exist or is invalid.
    """
    try:
        with open(RATELIMIT_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("ratelimit.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in ratelimit.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load ratelimit.json: {e}")
        return None


def load_offsets_file() -> Optional[dict]:
    """Load offsets.json.
    
    Returns:
        Offsets dict (zone_id -> offset_celsius), or None if file doesn't exist.
    """
    try:
        with open(OFFSETS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("offsets.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in offsets.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load offsets.json: {e}")
        return None


def load_ac_capabilities_file() -> Optional[dict]:
    """Load ac_capabilities.json.
    
    Returns:
        AC capabilities dict (zone_id -> capabilities), or None if file doesn't exist.
    """
    try:
        with open(AC_CAPABILITIES_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("ac_capabilities.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in ac_capabilities.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load ac_capabilities.json: {e}")
        return None


def load_api_call_history_file() -> Optional[dict]:
    """Load api_call_history.json.
    
    Returns:
        API call history dict, or None if file doesn't exist.
    """
    try:
        with open(API_CALL_HISTORY_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("api_call_history.json not found")
        return None
    except json.JSONDecodeError as e:
        _LOGGER.warning(f"Invalid JSON in api_call_history.json: {e}")
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to load api_call_history.json: {e}")
        return None


# Convenience functions for common data access patterns

def get_zone_names() -> dict:
    """Get zone ID to name mapping.
    
    Returns:
        Dict mapping zone_id (str) to zone_name (str).
    """
    from .const import DEFAULT_ZONE_NAMES
    
    zones_info = load_zones_info_file()
    if zones_info:
        return {str(z.get('id')): z.get('name', f"Zone {z.get('id')}") for z in zones_info}
    return DEFAULT_ZONE_NAMES


def get_zone_types() -> dict:
    """Get zone ID to type mapping.
    
    Returns:
        Dict mapping zone_id (str) to zone_type (str).
    """
    zones_info = load_zones_info_file()
    if zones_info:
        return {str(z.get('id')): z.get('type', 'HEATING') for z in zones_info}
    return {}


def get_zone_data(zone_id: str) -> Optional[dict]:
    """Get state data for a specific zone.
    
    Args:
        zone_id: Zone ID to look up.
        
    Returns:
        Zone state dict, or None if not found.
    """
    zones_data = load_zones_file()
    if zones_data:
        # Use 'or {}' pattern for null safety
        zone_states = zones_data.get('zoneStates') or {}
        return zone_states.get(zone_id)
    return None
