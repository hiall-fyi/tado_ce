"""Constants for Tado CE integration."""
from pathlib import Path
import os
from typing import Optional

DOMAIN = "tado_ce"
MANUFACTURER = "Joe Yiu (@hiall-fyi)"

# Data directory (persistent storage)
# v1.5.2: Moved from custom_components/tado_ce/data/ to .storage/tado_ce/
# This prevents HACS upgrades from overwriting credentials and data files
# Use environment variable if set (for testing), otherwise use standard HA path
_BASE_CONFIG_DIR = os.environ.get("TADO_CE_CONFIG_DIR", "/config")
DATA_DIR = Path(_BASE_CONFIG_DIR) / ".storage" / "tado_ce"

# Legacy data directory (for migration from v1.5.1 and earlier)
LEGACY_DATA_DIR = Path(_BASE_CONFIG_DIR) / "custom_components" / "tado_ce" / "data"

# v1.8.0: Multi-home support - per-home data files
# Files that are per-home (need home_id suffix)
PER_HOME_FILES = [
    "config", "zones", "zones_info", "ratelimit", "weather",
    "mobile_devices", "home_state", "api_call_history", "offsets",
    "ac_capabilities", "schedules"
]


def get_data_file(base_name: str, home_id: Optional[str] = None) -> Path:
    """Get data file path, with optional home_id suffix for multi-home support.
    
    v1.8.0: Supports per-home data files for multi-home setups.
    
    Args:
        base_name: Base filename without extension (e.g., "zones", "config")
        home_id: Optional home ID for per-home files
        
    Returns:
        Path to the data file
        
    Examples:
        get_data_file("zones") -> /config/.storage/tado_ce/zones.json
        get_data_file("zones", "12345") -> /config/.storage/tado_ce/zones_12345.json
    """
    if home_id and base_name in PER_HOME_FILES:
        return DATA_DIR / f"{base_name}_{home_id}.json"
    return DATA_DIR / f"{base_name}.json"


def get_legacy_file(base_name: str) -> Path:
    """Get legacy file path (without home_id suffix).
    
    Used for backwards compatibility and migration.
    
    Args:
        base_name: Base filename without extension
        
    Returns:
        Path to the legacy data file
    """
    return DATA_DIR / f"{base_name}.json"


# Legacy file paths (for backwards compatibility)
# These are kept for existing code that imports them directly
# New code should use get_data_file() with home_id
CONFIG_FILE = DATA_DIR / "config.json"
ZONES_FILE = DATA_DIR / "zones.json"
ZONES_INFO_FILE = DATA_DIR / "zones_info.json"
RATELIMIT_FILE = DATA_DIR / "ratelimit.json"
WEATHER_FILE = DATA_DIR / "weather.json"
MOBILE_DEVICES_FILE = DATA_DIR / "mobile_devices.json"
HOME_STATE_FILE = DATA_DIR / "home_state.json"
API_CALL_HISTORY_FILE = DATA_DIR / "api_call_history.json"
OFFSETS_FILE = DATA_DIR / "offsets.json"
AC_CAPABILITIES_FILE = DATA_DIR / "ac_capabilities.json"

# API Base URLs
TADO_API_BASE = "https://my.tado.com/api/v2"
TADO_AUTH_URL = "https://login.tado.com/oauth2"
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

# API Endpoints (relative to TADO_API_BASE)
API_ENDPOINT_ME = f"{TADO_API_BASE}/me"
API_ENDPOINT_HOMES = f"{TADO_API_BASE}/homes"  # + /{home_id}
API_ENDPOINT_DEVICES = f"{TADO_API_BASE}/devices"  # + /{serial}

# Auth Endpoints
AUTH_ENDPOINT_DEVICE = f"{TADO_AUTH_URL}/device_authorize"
AUTH_ENDPOINT_TOKEN = f"{TADO_AUTH_URL}/token"

# Default zone names (fallback)
DEFAULT_ZONE_NAMES = {
    "0": "Hot Water", "1": "Dining", "4": "Guest", "5": "Study",
    "6": "Dressing", "9": "Lounge", "11": "Hallway", "13": "Bathroom",
    "16": "Ensuite", "18": "Master"
}
