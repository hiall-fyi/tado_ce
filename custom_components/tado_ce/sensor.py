"""Tado CE Sensors."""
import json
import logging
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorEntityDescription
from homeassistant.const import UnitOfTemperature, PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, ZONES_FILE, ZONES_INFO_FILE, RATELIMIT_FILE, WEATHER_FILE, MOBILE_DEVICES_FILE, API_CALL_HISTORY_FILE, DEFAULT_ZONE_NAMES, CONFIG_FILE, DATA_DIR
from .device_manager import get_hub_device_info, get_zone_device_info
from .auth_manager import get_auth_manager
from .const import TADO_AUTH_URL, CLIENT_ID
from .data_loader import (
    load_zones_file, load_zones_info_file, load_weather_file,
    load_config_file, load_ratelimit_file, load_api_call_history_file,
    get_zone_names as dl_get_zone_names
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)

# Weather state mapping
WEATHER_STATE_MAP = {
    "CLOUDY_MOSTLY": "Mostly Cloudy",
    "CLOUDY_PARTLY": "Partly Cloudy",
    "CLOUDY": "Cloudy",
    "DRIZZLE": "Drizzle",
    "FOGGY": "Foggy",
    "NIGHT_CLEAR": "Clear Night",
    "NIGHT_CLOUDY": "Cloudy Night",
    "RAIN": "Rain",
    "SCATTERED_RAIN": "Scattered Rain",
    "SNOW": "Snow",
    "SUN": "Sunny",
    "THUNDERSTORMS": "Thunderstorms",
    "WINDY": "Windy",
}

# Cached home_id to avoid blocking calls in event loop
_CACHED_HOME_ID = None

def _load_home_id():
    """Load home ID from config file (blocking, run in executor)."""
    config = load_config_file()
    return config.get('home_id', 'unknown') if config else 'unknown'

def get_zone_names():
    """Load zone names from API data."""
    return dl_get_zone_names()

async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE sensors from a config entry."""
    # Load home_id in executor to avoid blocking event loop
    global _CACHED_HOME_ID
    _CACHED_HOME_ID = await hass.async_add_executor_job(_load_home_id)
    
    # Get configuration manager from hass data
    from .config_manager import ConfigurationManager
    config_manager = ConfigurationManager(entry)
    
    zone_names = await hass.async_add_executor_job(get_zone_names)
    
    sensors = []
    
    # Hub sensors (API status, home info)
    sensors.append(TadoHomeIdSensor())
    sensors.append(TadoApiUsageSensor())
    sensors.append(TadoApiLimitSensor())
    sensors.append(TadoApiResetSensor())
    sensors.append(TadoApiStatusSensor())
    sensors.append(TadoTokenStatusSensor())
    sensors.append(TadoZoneCountSensor())
    sensors.append(TadoLastSyncSensor())
    
    # Boiler Flow Temperature sensor (Hub device - only if data available)
    # This requires OpenTherm connection between Tado and boiler
    if await hass.async_add_executor_job(_has_boiler_flow_temperature_data):
        _LOGGER.info("Boiler flow temperature data detected - creating sensor")
        sensors.append(TadoBoilerFlowTemperatureSensor())
    else:
        _LOGGER.debug("No boiler flow temperature data found - sensor not created (requires OpenTherm)")
    
    # Weather sensors (optional based on configuration)
    if config_manager.get_weather_enabled():
        sensors.append(TadoOutsideTemperatureSensor())
        sensors.append(TadoSolarIntensitySensor())
        sensors.append(TadoWeatherStateSensor())
    
    # Zone sensors
    try:
        zones_data = await hass.async_add_executor_job(load_zones_file)
        zones_info = await hass.async_add_executor_job(load_zones_info_file)
        
        # Build zone type map
        zone_types = {}
        if zones_info:
            zone_types = {str(z.get('id')): z.get('type', 'HEATING') for z in zones_info}
        
        if zones_data:
            # Use 'or {}' pattern for null safety
            zone_states = zones_data.get('zoneStates') or {}
            for zone_id, zone_data in zone_states.items():
                zone_type = zone_types.get(zone_id, 'HEATING')
                zone_name = zone_names.get(zone_id, f"Zone {zone_id}")
                
                # Check if zone has temperature sensor data
                # Use 'or {}' pattern for null safety
                sensor_data = zone_data.get('sensorDataPoints') or {}
                inside_temp = sensor_data.get('insideTemperature') or {}
                has_temperature = inside_temp.get('celsius') is not None
                
                if zone_type == 'HEATING':
                    sensors.extend([
                        TadoTemperatureSensor(zone_id, zone_name, zone_type),
                        TadoHumiditySensor(zone_id, zone_name, zone_type),
                        TadoHeatingPowerSensor(zone_id, zone_name, zone_type),
                        TadoTargetTempSensor(zone_id, zone_name, zone_type),
                        TadoOverlaySensor(zone_id, zone_name, zone_type),
                    ])
                elif zone_type == 'AIR_CONDITIONING':
                    sensors.extend([
                        TadoTemperatureSensor(zone_id, zone_name, zone_type),
                        TadoHumiditySensor(zone_id, zone_name, zone_type),
                        TadoACPowerSensor(zone_id, zone_name, zone_type),
                        TadoTargetTempSensor(zone_id, zone_name, zone_type),
                        TadoOverlaySensor(zone_id, zone_name, zone_type),
                    ])
                elif zone_type == 'HOT_WATER':
                    # Only create temperature sensor if zone has temperature data
                    # Many hot water zones (combi boilers) don't have temperature sensors
                    if has_temperature:
                        sensors.append(TadoTemperatureSensor(zone_id, zone_name, zone_type))
                    sensors.append(TadoOverlaySensor(zone_id, zone_name, zone_type))
                    sensors.append(TadoHotWaterPowerSensor(zone_id, zone_name, zone_type))
    except Exception as e:
        _LOGGER.error(f"Failed to load zones: {e}")
    
    # Device sensors (battery + connection) - track seen serials to avoid duplicates
    seen_serials = set()
    try:
        zones_info = await hass.async_add_executor_job(load_zones_info_file)
        if zones_info:
            for zone in zones_info:
                zone_id = str(zone.get('id'))
                zone_name = zone.get('name', f"Zone {zone_id}")
                zone_type = zone.get('type', 'HEATING')
                for device in zone.get('devices', []):
                    serial = device.get('shortSerialNo')
                    if serial and serial not in seen_serials:
                        # Battery sensor (if device has battery)
                        if 'batteryState' in device:
                            sensors.append(TadoBatterySensor(zone_id, zone_name, zone_type, device, zones_info))
                        # Connection sensor (all devices)
                        if 'connectionState' in device:
                            sensors.append(TadoDeviceConnectionSensor(zone_id, zone_name, zone_type, device, zones_info))
                        seen_serials.add(serial)
    except Exception as e:
        _LOGGER.debug(f"Failed to load device info: {e}")
    
    async_add_entities(sensors, True)
    _LOGGER.info(f"Tado CE sensors loaded: {len(sensors)}")


def _has_boiler_flow_temperature_data():
    """Check if any zone has boiler flow temperature data (requires OpenTherm).
    
    This is used during setup to determine if the boiler flow temperature
    sensor should be created. Only systems with OpenTherm connection between
    Tado and the boiler will have this data.
    """
    try:
        with open(ZONES_FILE) as f:
            data = json.load(f)
        
        # Use 'or {}' pattern for null safety
        zone_states = data.get('zoneStates') or {}
        for zone_id, zone_data in zone_states.items():
            # Use 'or {}' pattern for null safety
            activity_data = zone_data.get('activityDataPoints') or {}
            flow_temp = (activity_data.get('boilerFlowTemperature') or {}).get('celsius')
            if flow_temp is not None:
                _LOGGER.debug(f"Found boilerFlowTemperature in zone {zone_id}: {flow_temp}°C")
                return True
        
        return False
    except Exception as e:
        _LOGGER.debug(f"Error checking boiler flow temperature data: {e}")
        return False

# ============ Hub Sensors (Tado CE Hub Device) ============

class TadoHomeIdSensor(SensorEntity):
    """Sensor showing Tado Home ID."""
    
    def __init__(self):
        self._attr_name = "Tado CE Home ID"
        self._attr_unique_id = "tado_ce_home_id"
        self._attr_icon = "mdi:home"
        self._attr_device_info = get_hub_device_info()
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_available = False
        self._attr_native_value = None
    
    def update(self):
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
                self._attr_native_value = config.get("home_id")
                self._attr_available = self._attr_native_value is not None
        except Exception:
            self._attr_available = False


class TadoApiUsageSensor(SensorEntity):
    """Sensor for Tado API usage tracking."""
    
    def __init__(self):
        self._attr_name = "Tado CE API Usage"
        self._attr_unique_id = "tado_ce_api_usage"
        self._attr_native_unit_of_measurement = "calls"
        self._attr_state_class = "measurement"
        self._attr_device_info = get_hub_device_info()
        self._attr_available = False
        self._attr_native_value = None
        self._data = {}
        self._call_history = []
    
    @property
    def icon(self):
        status = self._data.get("status")
        if status == "rate_limited":
            return "mdi:api-off"
        elif status == "error":
            return "mdi:alert-circle"
        return "mdi:api"
    
    @property
    def extra_state_attributes(self):
        attrs = {
            "limit": self._data.get("limit"),
            "remaining": self._data.get("remaining"),
            "percentage_used": self._data.get("percentage_used"),
            "last_updated": self._data.get("last_updated"),
            "status": self._data.get("status"),
        }
        
        # Add Test Mode indicator if enabled
        try:
            from .config_manager import ConfigurationManager
            from homeassistant.config_entries import ConfigEntry
            
            # Try to get config entry (this is a bit hacky but works)
            hass = self.hass
            if hass:
                entries = hass.config_entries.async_entries(DOMAIN)
                if entries:
                    config_manager = ConfigurationManager(entries[0])
                    if config_manager.get_test_mode_enabled():
                        attrs["test_mode"] = "Test Mode: 100 call limit"
        except Exception as e:
            _LOGGER.debug(f"Failed to check Test Mode status: {e}")
        
        # Add call history if available
        if self._call_history:
            attrs["call_history"] = self._call_history
        
        return attrs
    
    def update(self):
        try:
            with open(RATELIMIT_FILE) as f:
                self._data = json.load(f)
                used = self._data.get("used")
                if used is not None:
                    self._attr_native_value = int(used)
                    self._attr_available = True
                else:
                    self._attr_available = False
            
            # Load call history from tracker
            try:
                from .api_call_tracker import APICallTracker
                from .config_manager import ConfigurationManager
                
                # Get retention days from config
                retention_days = 14  # default
                try:
                    config_manager = ConfigurationManager(None)
                    retention_days = config_manager.get_api_history_retention_days()
                except:
                    pass
                
                tracker = APICallTracker(DATA_DIR, retention_days=retention_days)
                self._call_history = tracker.get_recent_calls(limit=50)
            except Exception as e:
                _LOGGER.debug(f"Failed to load call history: {e}")
                self._call_history = []
                
        except Exception:
            self._attr_available = False

class TadoApiResetSensor(SensorEntity):
    """Sensor showing API rate limit reset time."""
    
    def __init__(self):
        self._attr_name = "Tado CE API Reset"
        self._attr_unique_id = "tado_ce_api_reset"
        self._attr_icon = "mdi:timer-refresh"
        self._attr_device_class = "timestamp"
        self._attr_device_info = get_hub_device_info()
        self._attr_available = False
        self._attr_native_value = None
        self._reset_human = None
        self._reset_seconds = None
        self._status = None
        self._next_poll = None
        self._current_interval = None
    
    @property
    def extra_state_attributes(self):
        return {
            "time_until_reset": self._reset_human,
            "reset_seconds": self._reset_seconds,
            "status": self._status,
            "next_poll": self._next_poll,
            "current_interval_minutes": self._current_interval,
        }
    
    def update(self):
        try:
            from datetime import datetime, timezone, timedelta
            
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                
            self._reset_human = data.get("reset_human")
            self._reset_seconds = data.get("reset_seconds")
            self._status = data.get("status", "unknown")
            
            reset_at = data.get("reset_at")
            if reset_at and reset_at != "unknown":
                try:
                    reset_time = datetime.fromisoformat(reset_at.replace('Z', '+00:00'))
                    self._attr_native_value = reset_time
                    self._attr_available = True
                except Exception as e:
                    _LOGGER.debug(f"Failed to parse reset_at: {e}")
                    self._attr_native_value = None
                    self._attr_available = False
            else:
                self._attr_native_value = None
                self._attr_available = False
            
            # Calculate next poll time
            try:
                last_updated = data.get("last_updated")
                if last_updated:
                    last_sync = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                    
                    # Get current polling interval from config
                    from homeassistant.config_entries import ConfigEntry
                    entries = self.hass.config_entries.async_entries(DOMAIN) if self.hass else []
                    if entries:
                        from .config_manager import ConfigurationManager
                        from . import get_polling_interval
                        config_manager = ConfigurationManager(entries[0])
                        self._current_interval = get_polling_interval(config_manager)
                        
                        # Calculate next poll time
                        next_poll_time = last_sync + timedelta(minutes=self._current_interval)
                        self._next_poll = next_poll_time.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        self._next_poll = None
                        self._current_interval = None
                else:
                    self._next_poll = None
                    self._current_interval = None
            except Exception as e:
                _LOGGER.debug(f"Failed to calculate next poll time: {e}")
                self._next_poll = None
                self._current_interval = None
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update API reset sensor: {e}")
            self._attr_available = False
            self._attr_native_value = None

class TadoApiLimitSensor(SensorEntity):
    """Sensor showing Tado API daily limit."""
    
    def __init__(self):
        self._attr_name = "Tado CE API Limit"
        self._attr_unique_id = "tado_ce_api_limit"
        self._attr_icon = "mdi:speedometer"
        self._attr_native_unit_of_measurement = "calls"
        self._attr_device_info = get_hub_device_info()
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_available = False
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
    
    def update(self):
        try:
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                self._attr_native_value = data.get("limit")
                self._attr_available = self._attr_native_value is not None
            
            # Load recent API calls from history (last 100 calls only to avoid DB size issues)
            try:
                from datetime import datetime, timedelta
                with open(API_CALL_HISTORY_FILE) as f:
                    history = json.load(f)
                    
                    # Flatten all calls from all dates
                    all_calls = []
                    for date_key, calls in history.items():
                        all_calls.extend(calls)
                    
                    # Sort by timestamp (newest first) and take last 100
                    all_calls.sort(key=lambda x: x["timestamp"], reverse=True)
                    recent_calls = all_calls[:100]
                    
                    # Count calls from last 24 hours for statistics
                    now = datetime.now()
                    cutoff = now - timedelta(hours=24)
                    last_24h_count = sum(
                        1 for call in all_calls
                        if datetime.fromisoformat(call["timestamp"]) > cutoff
                    )
                    
                    self._attr_extra_state_attributes = {
                        "recent_calls": recent_calls,
                        "recent_calls_count": len(recent_calls),
                        "last_24h_count": last_24h_count,
                        "total_calls_tracked": len(all_calls)
                    }
            except Exception as e:
                _LOGGER.debug(f"Failed to load API call history: {e}")
                self._attr_extra_state_attributes = {
                    "recent_calls": [],
                    "recent_calls_count": 0,
                    "last_24h_count": 0,
                    "total_calls_tracked": 0
                }
        except Exception:
            self._attr_available = False

class TadoApiStatusSensor(SensorEntity):
    """Sensor showing Tado API status."""
    
    def __init__(self):
        self._attr_name = "Tado CE API Status"
        self._attr_unique_id = "tado_ce_api_status"
        self._attr_device_info = get_hub_device_info()
        self._attr_available = False
        self._attr_native_value = None
    
    @property
    def icon(self):
        if self._attr_native_value == "ok":
            return "mdi:check-circle"
        elif self._attr_native_value == "rate_limited":
            return "mdi:alert-circle"
        return "mdi:help-circle"
    
    def update(self):
        try:
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                self._attr_native_value = data.get("status", "unknown")
                self._attr_available = True
        except Exception:
            self._attr_native_value = "error"
            self._attr_available = True

class TadoTokenStatusSensor(SensorEntity):
    """Sensor showing Tado token status."""
    
    def __init__(self):
        self._attr_name = "Tado CE Token Status"
        self._attr_unique_id = "tado_ce_token_status"
        self._attr_device_info = get_hub_device_info()
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_available = False
        self._attr_native_value = None
    
    @property
    def icon(self):
        if self._attr_native_value == "valid":
            return "mdi:key-check"
        return "mdi:key-alert"
    
    def update(self):
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
                if config.get("refresh_token"):
                    self._attr_native_value = "valid"
                else:
                    self._attr_native_value = "missing"
                self._attr_available = True
        except Exception:
            self._attr_native_value = "error"
            self._attr_available = True

class TadoZoneCountSensor(SensorEntity):
    """Sensor showing number of Tado zones."""
    
    def __init__(self):
        self._attr_name = "Tado CE Zone Count"
        self._attr_unique_id = "tado_ce_zone_count"
        self._attr_icon = "mdi:home-thermometer"
        self._attr_native_unit_of_measurement = "zones"
        self._attr_device_info = get_hub_device_info()
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_available = False
        self._attr_native_value = None
        self._heating_zones = 0
        self._hot_water_zones = 0
        self._ac_zones = 0
    
    @property
    def extra_state_attributes(self):
        return {
            "heating_zones": self._heating_zones,
            "hot_water_zones": self._hot_water_zones,
            "ac_zones": self._ac_zones,
        }
    
    def update(self):
        try:
            with open(ZONES_INFO_FILE) as f:
                zones = json.load(f)
                self._attr_native_value = len(zones)
                self._heating_zones = len([z for z in zones if z.get('type') == 'HEATING'])
                self._hot_water_zones = len([z for z in zones if z.get('type') == 'HOT_WATER'])
                self._ac_zones = len([z for z in zones if z.get('type') == 'AIR_CONDITIONING'])
                self._attr_available = True
        except Exception:
            self._attr_available = False

class TadoLastSyncSensor(SensorEntity):
    """Sensor showing last sync time."""
    
    def __init__(self):
        self._attr_name = "Tado CE Last Sync"
        self._attr_unique_id = "tado_ce_last_sync"
        self._attr_icon = "mdi:sync"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_device_info = get_hub_device_info()
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_available = False
        self._attr_native_value = None
    
    def update(self):
        try:
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                last_updated = data.get("last_updated")
                if last_updated:
                    from datetime import datetime
                    self._attr_native_value = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                    self._attr_available = True
                else:
                    self._attr_available = False
        except Exception:
            self._attr_available = False

# ============ Weather Sensors ============

class TadoOutsideTemperatureSensor(SensorEntity):
    """Outside temperature from Tado weather data."""
    
    def __init__(self):
        self._attr_name = "Tado CE Outside Temperature"
        self._attr_unique_id = "tado_ce_outside_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = "measurement"
        self._attr_available = False
        self._attr_native_value = None
        self._timestamp = None
    
    @property
    def extra_state_attributes(self):
        return {"timestamp": self._timestamp}
    
    def update(self):
        try:
            with open(WEATHER_FILE) as f:
                data = json.load(f)
                # Use 'or {}' pattern for null safety
                temp_data = data.get('outsideTemperature') or {}
                self._attr_native_value = temp_data.get('celsius')
                self._timestamp = temp_data.get('timestamp')
                self._attr_available = self._attr_native_value is not None
        except Exception:
            self._attr_available = False

class TadoSolarIntensitySensor(SensorEntity):
    """Solar intensity from Tado weather data."""
    
    def __init__(self):
        self._attr_name = "Tado CE Solar Intensity"
        self._attr_unique_id = "tado_ce_solar_intensity"
        self._attr_icon = "mdi:white-balance-sunny"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = "measurement"
        self._attr_available = False
        self._attr_native_value = None
        self._timestamp = None
    
    @property
    def extra_state_attributes(self):
        return {"timestamp": self._timestamp}
    
    def update(self):
        try:
            with open(WEATHER_FILE) as f:
                data = json.load(f)
                # Use 'or {}' pattern for null safety
                solar_data = data.get('solarIntensity') or {}
                self._attr_native_value = solar_data.get('percentage')
                self._timestamp = solar_data.get('timestamp')
                self._attr_available = self._attr_native_value is not None
        except Exception:
            self._attr_available = False

class TadoWeatherStateSensor(SensorEntity):
    """Weather state from Tado weather data."""
    
    def __init__(self):
        self._attr_name = "Tado CE Weather"
        self._attr_unique_id = "tado_ce_weather_state"
        self._attr_icon = "mdi:weather-partly-cloudy"
        self._attr_available = False
        self._attr_native_value = None
        self._raw_state = None
        self._timestamp = None
    
    @property
    def icon(self):
        icons = {
            "SUN": "mdi:weather-sunny",
            "CLOUDY": "mdi:weather-cloudy",
            "CLOUDY_MOSTLY": "mdi:weather-cloudy",
            "CLOUDY_PARTLY": "mdi:weather-partly-cloudy",
            "RAIN": "mdi:weather-rainy",
            "SCATTERED_RAIN": "mdi:weather-partly-rainy",
            "DRIZZLE": "mdi:weather-rainy",
            "SNOW": "mdi:weather-snowy",
            "FOGGY": "mdi:weather-fog",
            "NIGHT_CLEAR": "mdi:weather-night",
            "NIGHT_CLOUDY": "mdi:weather-night-partly-cloudy",
            "THUNDERSTORMS": "mdi:weather-lightning",
            "WINDY": "mdi:weather-windy",
        }
        return icons.get(self._raw_state, "mdi:weather-partly-cloudy")
    
    @property
    def extra_state_attributes(self):
        return {
            "raw_state": self._raw_state,
            "timestamp": self._timestamp,
        }
    
    def update(self):
        try:
            with open(WEATHER_FILE) as f:
                data = json.load(f)
                # Use 'or {}' pattern for null safety
                weather_data = data.get('weatherState') or {}
                self._raw_state = weather_data.get('value')
                self._timestamp = weather_data.get('timestamp')
                self._attr_native_value = WEATHER_STATE_MAP.get(self._raw_state, self._raw_state)
                self._attr_available = self._attr_native_value is not None
        except Exception:
            self._attr_available = False

# ============ Zone Sensors ============

class TadoBaseSensor(SensorEntity):
    """Base class for Tado zone sensors."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HEATING"):
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._attr_available = False
        self._attr_native_value = None
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type)
    
    def _get_zone_data(self):
        """Get zone data from file."""
        try:
            with open(ZONES_FILE) as f:
                data = json.load(f)
                # Use 'or {}' pattern for null safety
                zone_states = data.get('zoneStates') or {}
                return zone_states.get(self._zone_id)
        except Exception:
            return None
    
    def update(self):
        zone_data = self._get_zone_data()
        if zone_data:
            self._update_from_zone_data(zone_data)
            self._attr_available = True
        else:
            self._attr_available = False
    
    def _update_from_zone_data(self, zone_data):
        pass

class TadoTemperatureSensor(TadoBaseSensor):
    """Current temperature sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HEATING"):
        super().__init__(zone_id, zone_name, zone_type)
        self._attr_name = f"{zone_name} Temperature"
        # Use zone_id for unique_id to maintain entity_id stability across zone name changes
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = "measurement"
    
    def update(self):
        """Update temperature sensor - mark unavailable if no temperature data."""
        zone_data = self._get_zone_data()
        if zone_data:
            self._update_from_zone_data(zone_data)
            # Only mark available if we actually have temperature data
            # HOT_WATER zones (combi boilers) often don't have temperature sensors
            self._attr_available = self._attr_native_value is not None
        else:
            self._attr_available = False
    
    def _update_from_zone_data(self, zone_data):
        # Use 'or {}' pattern for null safety (API may return null for these fields)
        sensor_data = zone_data.get('sensorDataPoints') or {}
        self._attr_native_value = (
            (sensor_data.get('insideTemperature') or {}).get('celsius')
        )

class TadoHumiditySensor(TadoBaseSensor):
    """Humidity sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HEATING"):
        super().__init__(zone_id, zone_name, zone_type)
        self._attr_name = f"{zone_name} Humidity"
        # Use zone_id for unique_id to maintain entity_id stability across zone name changes
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_humidity"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = "measurement"
    
    def update(self):
        """Update humidity sensor - mark unavailable if no humidity data."""
        zone_data = self._get_zone_data()
        if zone_data:
            self._update_from_zone_data(zone_data)
            # Only mark available if we actually have humidity data
            # Some zones may not have humidity sensors
            self._attr_available = self._attr_native_value is not None
        else:
            self._attr_available = False
    
    def _update_from_zone_data(self, zone_data):
        # Use 'or {}' pattern for null safety (API may return null for these fields)
        sensor_data = zone_data.get('sensorDataPoints') or {}
        self._attr_native_value = (
            (sensor_data.get('humidity') or {}).get('percentage')
        )

class TadoHeatingPowerSensor(TadoBaseSensor):
    """Heating power sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HEATING"):
        super().__init__(zone_id, zone_name, zone_type)
        self._attr_name = f"{zone_name} Heating"
        # Use zone_name for unique_id to maintain entity_id stability
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_heating"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_icon = "mdi:radiator"
        self._attr_state_class = "measurement"
    
    def _update_from_zone_data(self, zone_data):
        # Use 'or {}' pattern for null safety (API may return null for these fields)
        activity_data = zone_data.get('activityDataPoints') or {}
        power = (activity_data.get('heatingPower') or {}).get('percentage')
        self._attr_native_value = power if power is not None else 0

class TadoACPowerSensor(TadoBaseSensor):
    """AC power sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "AIR_CONDITIONING"):
        super().__init__(zone_id, zone_name, zone_type)
        self._attr_name = f"{zone_name} AC Power"
        # Use zone_name for unique_id to maintain entity_id stability
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_ac_power"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_icon = "mdi:air-conditioner"
        self._attr_state_class = "measurement"
    
    def _update_from_zone_data(self, zone_data):
        # Use 'or {}' pattern for null safety (API may return null for these fields)
        activity_data = zone_data.get('activityDataPoints') or {}
        ac_power = activity_data.get('acPower') or {}
        # Try percentage first (older API), then value (newer API returns 'ON'/'OFF')
        power = ac_power.get('percentage')
        if power is None:
            value = ac_power.get('value')
            power = 100 if value == 'ON' else 0
        self._attr_native_value = power if power is not None else 0

class TadoBoilerFlowTemperatureSensor(SensorEntity):
    """Boiler flow temperature sensor - reads from HEATING zones.
    
    This is a Hub-level sensor that reads boilerFlowTemperature from
    any HEATING zone that has this data available.
    """
    
    def __init__(self):
        self._attr_name = "Tado CE Boiler Flow Temperature"
        self._attr_unique_id = "tado_ce_boiler_flow_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:water-boiler"
        self._attr_device_info = get_hub_device_info()
        self._attr_available = False
        self._attr_native_value = None
        self._source_zone = None
    
    @property
    def extra_state_attributes(self):
        return {
            "source_zone": self._source_zone,
        }
    
    def update(self):
        """Update boiler flow temperature from HEATING zones."""
        try:
            with open(ZONES_FILE) as f:
                data = json.load(f)
            
            # Look for boilerFlowTemperature in any zone
            # Use 'or {}' pattern for null safety
            zone_states = data.get('zoneStates') or {}
            for zone_id, zone_data in zone_states.items():
                # Use 'or {}' pattern for null safety
                activity_data = zone_data.get('activityDataPoints') or {}
                flow_temp = (activity_data.get('boilerFlowTemperature') or {}).get('celsius')
                if flow_temp is not None:
                    self._attr_native_value = flow_temp
                    self._source_zone = zone_id
                    self._attr_available = True
                    return
            
            # No boiler flow data found
            self._attr_native_value = None
            self._source_zone = None
            self._attr_available = False
            
        except Exception:
            self._attr_available = False

class TadoTargetTempSensor(TadoBaseSensor):
    """Target temperature sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HEATING"):
        super().__init__(zone_id, zone_name, zone_type)
        self._attr_name = f"{zone_name} Target"
        # Use zone_name for unique_id to maintain entity_id stability
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_target"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_icon = "mdi:thermometer-check"
    
    def _update_from_zone_data(self, zone_data):
        # Use 'or {}' pattern for null safety (API may return null for setting)
        setting = zone_data.get('setting') or {}
        if setting.get('power') == 'ON':
            self._attr_native_value = (setting.get('temperature') or {}).get('celsius')
        else:
            self._attr_native_value = None

class TadoOverlaySensor(TadoBaseSensor):
    """Overlay status sensor (Manual/Schedule)."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HEATING"):
        super().__init__(zone_id, zone_name, zone_type)
        self._attr_name = f"{zone_name} Mode"
        # Use zone_name for unique_id to maintain entity_id stability
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_mode"
        self._attr_icon = "mdi:calendar-clock"
        self._next_change = None
        self._next_temp = None
    
    @property
    def extra_state_attributes(self):
        return {
            "next_change": self._next_change,
            "next_temperature": self._next_temp,
        }
    
    def _update_from_zone_data(self, zone_data):
        overlay_type = zone_data.get('overlayType')
        # Use 'or {}' pattern for null safety
        setting = zone_data.get('setting') or {}
        power = setting.get('power')
        
        if power == 'OFF':
            self._attr_native_value = "Off"
        elif overlay_type == 'MANUAL':
            self._attr_native_value = "Manual"
        else:
            self._attr_native_value = "Schedule"
        
        # Next schedule change
        next_change = zone_data.get('nextScheduleChange')
        if next_change:
            self._next_change = next_change.get('start')
            next_setting = next_change.get('setting')
            if next_setting:
                temp = next_setting.get('temperature')
                self._next_temp = temp.get('celsius') if temp else None
            else:
                self._next_temp = None
        else:
            self._next_change = None
            self._next_temp = None


class TadoHotWaterPowerSensor(TadoBaseSensor):
    """Hot water power sensor (ON/OFF)."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str = "HOT_WATER"):
        super().__init__(zone_id, zone_name, zone_type)
        self._attr_name = f"{zone_name} Power"
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_power"
        self._attr_icon = "mdi:power"
    
    def _update_from_zone_data(self, zone_data):
        setting = zone_data.get('setting') or {}
        power = setting.get('power')
        self._attr_native_value = power if power else "Unknown"


# ============ Device Sensors ============

class TadoBatterySensor(SensorEntity):
    """Battery status sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str, device: dict, zones_info: list):
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._device_serial = device.get('shortSerialNo', 'unknown')
        self._device_type = device.get('deviceType', 'unknown')
        
        # Import here to avoid circular dependency
        from .device_manager import get_device_name_suffix
        suffix = get_device_name_suffix(zone_id, self._device_serial, self._device_type, zones_info)
        
        self._attr_name = f"{zone_name}{suffix} Battery"
        self._attr_unique_id = f"tado_ce_{self._device_serial}_battery"
        self._attr_icon = "mdi:battery"
        self._attr_available = True
        self._attr_native_value = device.get('batteryState', 'unknown')
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type)
        
        # Extra attributes
        self._firmware = device.get('currentFwVersion')
        self._connection_state = (device.get('connectionState') or {}).get('value')
        self._connection_timestamp = (device.get('connectionState') or {}).get('timestamp')
    
    @property
    def icon(self):
        if self._attr_native_value == 'LOW':
            return "mdi:battery-low"
        return "mdi:battery"
    
    @property
    def extra_state_attributes(self):
        return {
            "device_serial": self._device_serial,
            "device_type": self._device_type,
            "firmware_version": self._firmware,
            "connection_state": "online" if self._connection_state else "offline",
            "connection_timestamp": self._connection_timestamp,
        }
    
    def update(self):
        try:
            with open(ZONES_INFO_FILE) as f:
                zones_info = json.load(f)
                for zone in zones_info:
                    for device in zone.get('devices', []):
                        if device.get('shortSerialNo') == self._device_serial:
                            self._attr_native_value = device.get('batteryState', 'unknown')
                            self._firmware = device.get('currentFwVersion')
                            # Use 'or {}' pattern for null safety
                            conn = device.get('connectionState') or {}
                            self._connection_state = conn.get('value')
                            self._connection_timestamp = conn.get('timestamp')
                            self._attr_available = True
                            return
            self._attr_available = False
        except Exception:
            self._attr_available = False

class TadoDeviceConnectionSensor(SensorEntity):
    """Device connection state sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, zone_type: str, device: dict, zones_info: list):
        self._zone_id = zone_id
        self._device_serial = device.get('shortSerialNo', 'unknown')
        self._device_type = device.get('deviceType', 'unknown')
        self._zone_name = zone_name
        self._zone_type = zone_type
        
        # Import here to avoid circular dependency
        from .device_manager import get_device_name_suffix
        suffix = get_device_name_suffix(zone_id, self._device_serial, self._device_type, zones_info)
        
        self._attr_name = f"{zone_name}{suffix} Connection"
        self._attr_unique_id = f"tado_ce_{self._device_serial}_connection"
        self._attr_icon = "mdi:wifi"
        self._attr_available = True
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type)
        
        # Use 'or {}' pattern for null safety
        conn = device.get('connectionState') or {}
        self._attr_native_value = "Online" if conn.get('value') else "Offline"
        self._connection_timestamp = conn.get('timestamp')
        self._firmware = device.get('currentFwVersion')
    
    @property
    def icon(self):
        if self._attr_native_value == "Online":
            return "mdi:wifi"
        return "mdi:wifi-off"
    
    @property
    def extra_state_attributes(self):
        return {
            "device_serial": self._device_serial,
            "device_type": self._device_type,
            "firmware_version": self._firmware,
            "last_seen": self._connection_timestamp,
        }
    
    def update(self):
        try:
            with open(ZONES_INFO_FILE) as f:
                zones_info = json.load(f)
                for zone in zones_info:
                    for device in zone.get('devices', []):
                        if device.get('shortSerialNo') == self._device_serial:
                            # Use 'or {}' pattern for null safety
                            conn = device.get('connectionState') or {}
                            self._attr_native_value = "Online" if conn.get('value') else "Offline"
                            self._connection_timestamp = conn.get('timestamp')
                            self._firmware = device.get('currentFwVersion')
                            self._attr_available = True
                            return
            self._attr_available = False
        except Exception:
            self._attr_available = False
