"""Device Manager for Tado CE Integration.

This module manages device creation and entity assignment for the Tado CE integration.
It provides functions to generate device info for both the hub device and individual zone devices.

CRITICAL: This module must be called from async context with proper executor handling.
The get_home_id() function performs blocking I/O and should be called via hass.async_add_executor_job().
"""
import json
import logging
from pathlib import Path
from typing import Optional

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CONFIG_FILE

_LOGGER = logging.getLogger(__name__)

# Cache for home_id - MUST be set via load_home_id() before use
_CACHED_HOME_ID: Optional[str] = None

# Cache for version from manifest
_CACHED_VERSION: Optional[str] = None


def _load_version() -> str:
    """Load version from manifest.json."""
    global _CACHED_VERSION
    if _CACHED_VERSION is not None:
        return _CACHED_VERSION
    
    try:
        manifest_path = Path(__file__).parent / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)
            _CACHED_VERSION = manifest.get("version", "unknown")
            return _CACHED_VERSION
    except Exception as e:
        _LOGGER.warning(f"Failed to load version from manifest: {e}")
        return "unknown"


def load_home_id() -> str:
    """Load home ID from config file (blocking I/O).
    
    This function performs blocking file I/O and MUST be called via
    hass.async_add_executor_job() from async context.
    
    Returns:
        str: The home ID, or "unknown" if not available.
    """
    global _CACHED_HOME_ID
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
            home_id = config.get("home_id", "unknown")
            _CACHED_HOME_ID = home_id
            return home_id
    except Exception as e:
        _LOGGER.warning(f"Failed to load home_id: {e}")
        _CACHED_HOME_ID = "unknown"
        return "unknown"


def get_home_id() -> str:
    """Get cached home ID.
    
    IMPORTANT: load_home_id() must be called first during integration setup.
    This function returns the cached value and does not perform I/O.
    
    Returns:
        str: The cached home ID, or "unknown" if not loaded.
    """
    global _CACHED_HOME_ID
    if _CACHED_HOME_ID is None:
        _LOGGER.warning("get_home_id() called before load_home_id() - returning 'unknown'")
        return "unknown"
    return _CACHED_HOME_ID


def get_hub_device_info() -> DeviceInfo:
    """Get device info for Tado CE Hub.
    
    The hub device contains global entities that apply to the entire Tado system,
    such as API usage sensors, weather sensors, and mobile device trackers.
    
    IMPORTANT: Hub identifier is fixed to ensure device stability across versions
    and configuration changes. This prevents duplicate hub devices when upgrading
    or re-authenticating.
    
    Returns:
        DeviceInfo: Device information for the Tado CE Hub.
    """
    # Use fixed identifier to prevent duplicate devices during upgrades
    # Previously used home_id which caused issues when config was missing
    return DeviceInfo(
        identifiers={(DOMAIN, "tado_ce_hub")},
        name="Tado CE Hub",
        manufacturer="Joe Yiu (@hiall-fyi)",
        model="Tado CE Integration",
        sw_version=_load_version(),
    )


def get_zone_device_info(zone_id: str, zone_name: str, zone_type: str) -> DeviceInfo:
    """Get device info for a specific Tado zone.
    
    Each zone device represents a physical zone (room) in the Tado system and contains
    all entities specific to that zone (climate, sensors, switches, etc.).
    
    Args:
        zone_id: The unique identifier for the zone (e.g., "1", "4", "9").
        zone_name: The human-readable name of the zone (e.g., "Living Room").
        zone_type: The type of zone - "HEATING", "AIR_CONDITIONING", or "HOT_WATER".
    
    Returns:
        DeviceInfo: Device information for the zone device.
    """
    model = get_zone_type_display(zone_type)
    
    # Use fixed hub identifier for via_device to match get_hub_device_info()
    return DeviceInfo(
        identifiers={(DOMAIN, f"tado_ce_zone_{zone_id}")},
        name=zone_name,
        manufacturer="Joe Yiu (@hiall-fyi)",
        model=model,
        via_device=(DOMAIN, "tado_ce_hub"),
    )


def get_zone_type_display(zone_type: str) -> str:
    """Convert zone type to display name for device model field.
    
    Args:
        zone_type: The zone type from Tado API - "HEATING", "AIR_CONDITIONING", or "HOT_WATER".
    
    Returns:
        str: Human-readable display name for the zone type.
    """
    zone_type_map = {
        "HEATING": "Heating Zone",
        "AIR_CONDITIONING": "AC Zone",
        "HOT_WATER": "Hot Water Zone",
    }
    return zone_type_map.get(zone_type, "Unknown Zone")


def get_device_name_suffix(zone_id: str, device_serial: str, device_type: str, zones_info: list) -> str:
    """Get device name suffix for zones with multiple devices.
    
    When a zone has multiple physical devices (e.g., 1 sensor + 2 valves), entity names
    need to be differentiated. This function generates an appropriate suffix.
    
    Args:
        zone_id: The zone ID (e.g., "1", "4").
        device_serial: The device serial number (e.g., "RU1234567").
        device_type: The device type (e.g., "VA02", "RU01").
        zones_info: The full zones_info data from zones_info.json.
    
    Returns:
        str: Empty string if zone has only 1 device, otherwise a suffix like " VA02 (1)" or " RU01".
    
    Examples:
        - Single device zone: "" (no suffix)
        - Multiple devices, different types: " VA02", " RU01"
        - Multiple devices, same type: " VA02 (1)", " VA02 (2)"
    """
    # Find the zone
    zone = next((z for z in zones_info if str(z.get('id')) == str(zone_id)), None)
    if not zone:
        return ""
    
    devices = zone.get('devices', [])
    if len(devices) <= 1:
        return ""  # Single device - no suffix needed
    
    # Multiple devices - check if there are multiple of the same type
    same_type_devices = [d for d in devices if d.get('deviceType') == device_type]
    
    if len(same_type_devices) > 1:
        # Multiple devices of same type - add index
        try:
            index = next(i + 1 for i, d in enumerate(same_type_devices) if d.get('shortSerialNo') == device_serial)
            return f" {device_type} ({index})"
        except StopIteration:
            return f" {device_type}"
    else:
        # Only one of this type - just add device type
        return f" {device_type}"
