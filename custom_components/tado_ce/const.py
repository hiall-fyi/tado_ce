"""Constants for Tado CE integration."""
from pathlib import Path

DOMAIN = "tado_ce"

# Data directory (persistent storage)
DATA_DIR = Path("/config/custom_components/tado_ce/data")
CONFIG_FILE = DATA_DIR / "config.json"
ZONES_FILE = DATA_DIR / "zones.json"
ZONES_INFO_FILE = DATA_DIR / "zones_info.json"
RATELIMIT_FILE = DATA_DIR / "ratelimit.json"
WEATHER_FILE = DATA_DIR / "weather.json"
MOBILE_DEVICES_FILE = DATA_DIR / "mobile_devices.json"
HOME_STATE_FILE = DATA_DIR / "home_state.json"
API_CALL_HISTORY_FILE = DATA_DIR / "api_call_history.json"

# API
TADO_API_BASE = "https://my.tado.com/api/v2"
TADO_AUTH_URL = "https://login.tado.com/oauth2"
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

# Default zone names (fallback)
DEFAULT_ZONE_NAMES = {
    "0": "Hot Water", "1": "Dining", "4": "Guest", "5": "Study",
    "6": "Dressing", "9": "Lounge", "11": "Hallway", "13": "Bathroom",
    "16": "Ensuite", "18": "Master"
}
