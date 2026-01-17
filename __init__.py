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

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.CLIMATE, Platform.BINARY_SENSOR, Platform.WATER_HEATER, Platform.DEVICE_TRACKER, Platform.SWITCH]
SCRIPT_PATH = "/config/custom_components/tado_ce/tado_api.py"

# Service names
SERVICE_SET_CLIMATE_TIMER = "set_climate_timer"
SERVICE_SET_WATER_HEATER_TIMER = "set_water_heater_timer"
SERVICE_RESUME_SCHEDULE = "resume_schedule"
SERVICE_SET_TEMP_OFFSET = "set_temperature_offset"
SERVICE_ADD_METER_READING = "add_meter_reading"
SERVICE_IDENTIFY_DEVICE = "identify_device"
SERVICE_SET_AWAY_CONFIG = "set_away_configuration"

# Smart polling configuration
DAY_START_HOUR = 7
NIGHT_START_HOUR = 23

POLLING_INTERVALS = [
    (100, 30, 120),
    (1000, 15, 60),
    (5000, 10, 30),
    (20000, 5, 15),
]
DEFAULT_DAY_INTERVAL = 30
DEFAULT_NIGHT_INTERVAL = 120
FULL_SYNC_INTERVAL_HOURS = 6


def is_daytime() -> bool:
    """Check if current time is daytime (7am-11pm)."""
    hour = datetime.now().hour
    return DAY_START_HOUR <= hour < NIGHT_START_HOUR


def get_polling_interval() -> int:
    """Get polling interval based on API rate limit and time of day."""
    daytime = is_daytime()
    
    try:
        if RATELIMIT_FILE.exists():
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                limit = data.get("limit")
                if limit:
                    for threshold, day_interval, night_interval in POLLING_INTERVALS:
                        if limit <= threshold:
                            return day_interval if daytime else night_interval
                    # Use fastest for highest limits
                    _, day_interval, night_interval = POLLING_INTERVALS[-1]
                    return day_interval if daytime else night_interval
    except Exception:
        pass
    
    return DEFAULT_DAY_INTERVAL if daytime else DEFAULT_NIGHT_INTERVAL


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Tado CE component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tado CE from a config entry."""
    _LOGGER.warning("Tado CE: Integration loading...")
    
    # Ensure data directory exists
    DATA_DIR.mkdir(exist_ok=True)
    
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
        new_interval = get_polling_interval()
        
        if new_interval != current_interval[0]:
            time_period = "day" if is_daytime() else "night"
            _LOGGER.info(f"Tado CE: Polling interval set to {new_interval}m ({time_period})")
            current_interval[0] = new_interval
        
        # Cancel old interval
        if cancel_interval[0]:
            cancel_interval[0]()
        
        # Schedule new interval
        cancel_interval[0] = async_track_time_interval(
            hass,
            lambda now: hass.async_add_executor_job(sync_tado),
            timedelta(minutes=new_interval)
        )
    
    def sync_tado(now=None):
        """Run Tado sync script."""
        import subprocess
        
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
    if CONFIG_FILE.exists():
        await hass.async_add_executor_job(sync_tado)
    else:
        # Still schedule polling even without config
        schedule_next_sync()
    
    # Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    await _async_register_services(hass)
    
    _LOGGER.warning("Tado CE: Integration loaded successfully")
    return True


async def _async_register_services(hass: HomeAssistant):
    """Register Tado CE services."""
    
    async def handle_set_climate_timer(call: ServiceCall):
        """Handle set_climate_timer service call."""
        entity_ids = call.data.get("entity_id", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        
        temperature = call.data.get("temperature")
        duration = call.data.get("duration")
        overlay = call.data.get("overlay")
        
        for entity_id in entity_ids:
            entity = hass.states.get(entity_id)
            if entity:
                # Get the climate entity and call set_timer
                climate_entity = hass.data.get("entity_components", {}).get("climate")
                if climate_entity:
                    for ent in climate_entity.entities:
                        if ent.entity_id == entity_id and hasattr(ent, 'set_timer'):
                            await hass.async_add_executor_job(
                                ent.set_timer, temperature, duration, overlay
                            )
                            break
    
    async def handle_set_water_heater_timer(call: ServiceCall):
        """Handle set_water_heater_timer service call."""
        entity_ids = call.data.get("entity_id", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        
        duration = call.data.get("duration")
        
        for entity_id in entity_ids:
            water_heater_component = hass.data.get("entity_components", {}).get("water_heater")
            if water_heater_component:
                for ent in water_heater_component.entities:
                    if ent.entity_id == entity_id and hasattr(ent, 'set_timer'):
                        await hass.async_add_executor_job(ent.set_timer, duration)
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
            vol.Optional("duration"): vol.Coerce(int),
            vol.Optional("overlay"): cv.string,
        })
    )
    
    hass.services.async_register(
        DOMAIN, SERVICE_SET_WATER_HEATER_TIMER, handle_set_water_heater_timer,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_ids,
            vol.Required("duration"): vol.Coerce(int),
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
    """Get access token by refreshing from config."""
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        
        refresh_token = config.get("refresh_token")
        if not refresh_token:
            return None
        
        data = urlencode({
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }).encode()
        
        req = Request(f"{TADO_AUTH_URL}/token", data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        
        with urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode())
            
            new_refresh = token_data.get("refresh_token")
            if new_refresh and new_refresh != refresh_token:
                config["refresh_token"] = new_refresh
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(config, f, indent=2)
            
            return token_data.get("access_token")
    except Exception as e:
        _LOGGER.error(f"Failed to get access token: {e}")
        return None


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
