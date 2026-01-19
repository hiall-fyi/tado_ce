"""Tado CE Sensors."""
import json
import logging
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorEntityDescription
from homeassistant.const import UnitOfTemperature, PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, ZONES_FILE, ZONES_INFO_FILE, RATELIMIT_FILE, WEATHER_FILE, MOBILE_DEVICES_FILE, DEFAULT_ZONE_NAMES, CONFIG_FILE

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
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
            return config.get('home_id', 'unknown')
    except Exception:
        return 'unknown'

def get_home_id():
    """Get cached home ID."""
    global _CACHED_HOME_ID
    if _CACHED_HOME_ID is None:
        _CACHED_HOME_ID = 'unknown'
    return _CACHED_HOME_ID

def get_hub_device_info():
    """Get device info for Tado CE Hub."""
    home_id = get_home_id()
    return DeviceInfo(
        identifiers={(DOMAIN, f"tado_ce_hub_{home_id}")},
        name="Tado CE Hub",
        manufacturer="Joe Yiu (@hiall-fyi)",
        model="Tado CE Integration",
        sw_version="1.1.0",
        configuration_url="https://buymeacoffee.com/hiallfyi",
    )

def get_zone_names():
    """Load zone names from API data."""
    try:
        with open(ZONES_INFO_FILE) as f:
            zones_info = json.load(f)
            return {str(z.get('id')): z.get('name', f"Zone {z.get('id')}") for z in zones_info}
    except Exception as e:
        _LOGGER.warning(f"Failed to load zone names: {e}")
        return DEFAULT_ZONE_NAMES

async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE sensors from a config entry."""
    # Load home_id in executor to avoid blocking event loop
    global _CACHED_HOME_ID
    _CACHED_HOME_ID = await hass.async_add_executor_job(_load_home_id)
    
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
    
    # Weather sensors
    sensors.append(TadoOutsideTemperatureSensor())
    sensors.append(TadoSolarIntensitySensor())
    sensors.append(TadoWeatherStateSensor())
    
    # Zone sensors
    try:
        zones_data = await hass.async_add_executor_job(_load_zones_file)
        zones_info = await hass.async_add_executor_job(_load_zones_info_file)
        
        # Build zone type map
        zone_types = {}
        if zones_info:
            zone_types = {str(z.get('id')): z.get('type', 'HEATING') for z in zones_info}
        
        if zones_data:
            for zone_id, zone_data in zones_data.get('zoneStates', {}).items():
                zone_type = zone_types.get(zone_id, 'HEATING')
                zone_name = zone_names.get(zone_id, f"Zone {zone_id}")
                
                if zone_type == 'HEATING':
                    sensors.extend([
                        TadoTemperatureSensor(zone_id, zone_name),
                        TadoHumiditySensor(zone_id, zone_name),
                        TadoHeatingPowerSensor(zone_id, zone_name),
                        TadoTargetTempSensor(zone_id, zone_name),
                        TadoOverlaySensor(zone_id, zone_name),
                    ])
                elif zone_type == 'AIR_CONDITIONING':
                    sensors.extend([
                        TadoTemperatureSensor(zone_id, zone_name),
                        TadoHumiditySensor(zone_id, zone_name),
                        TadoACPowerSensor(zone_id, zone_name),
                        TadoTargetTempSensor(zone_id, zone_name),
                        TadoOverlaySensor(zone_id, zone_name),
                    ])
    except Exception as e:
        _LOGGER.error(f"Failed to load zones: {e}")
    
    # Device sensors (battery + connection) - track seen serials to avoid duplicates
    seen_serials = set()
    try:
        zones_info = await hass.async_add_executor_job(_load_zones_info_file)
        if zones_info:
            for zone in zones_info:
                zone_id = str(zone.get('id'))
                zone_name = zone.get('name', f"Zone {zone_id}")
                for device in zone.get('devices', []):
                    serial = device.get('shortSerialNo')
                    if serial and serial not in seen_serials:
                        # Battery sensor (if device has battery)
                        if 'batteryState' in device:
                            sensors.append(TadoBatterySensor(zone_id, zone_name, device))
                        # Connection sensor (all devices)
                        if 'connectionState' in device:
                            sensors.append(TadoDeviceConnectionSensor(zone_name, device))
                        seen_serials.add(serial)
    except Exception as e:
        _LOGGER.debug(f"Failed to load device info: {e}")
    
    async_add_entities(sensors, True)
    _LOGGER.warning(f"Tado CE sensors loaded: {len(sensors)}")

def _load_zones_file():
    """Load zones file (blocking)."""
    try:
        with open(ZONES_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _load_zones_info_file():
    """Load zones info file (blocking)."""
    try:
        with open(ZONES_INFO_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _load_weather_file():
    """Load weather file (blocking)."""
    try:
        with open(WEATHER_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _load_mobile_devices_file():
    """Load mobile devices file (blocking)."""
    try:
        with open(MOBILE_DEVICES_FILE) as f:
            return json.load(f)
    except Exception:
        return None

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
        return {
            "limit": self._data.get("limit"),
            "remaining": self._data.get("remaining"),
            "reset_human": self._data.get("reset_human"),
            "reset_at": self._data.get("reset_at"),
            "percentage_used": self._data.get("percentage_used"),
            "last_updated": self._data.get("last_updated"),
            "status": self._data.get("status"),
        }
    
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
        except Exception:
            self._attr_available = False

class TadoApiResetSensor(SensorEntity):
    """Sensor showing time until API rate limit reset."""
    
    def __init__(self):
        self._attr_name = "Tado CE API Reset"
        self._attr_unique_id = "tado_ce_api_reset"
        self._attr_icon = "mdi:timer-refresh"
        self._attr_native_unit_of_measurement = "min"
        self._attr_state_class = "measurement"
        self._attr_device_info = get_hub_device_info()
        self._attr_available = False
        self._attr_native_value = None
        self._reset_at = None
        self._reset_human = None
        self._status = None
    
    @property
    def extra_state_attributes(self):
        return {
            "reset_at": self._reset_at,
            "reset_human": self._reset_human,
            "status": self._status,
        }
    
    def update(self):
        try:
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                self._reset_at = data.get("reset_at")
                self._reset_human = data.get("reset_human")
                self._status = data.get("status")
                
                reset_seconds = data.get("reset_seconds")
                if reset_seconds and reset_seconds != "unknown":
                    self._attr_native_value = int(reset_seconds) // 60
                else:
                    self._attr_native_value = 0
                self._attr_available = True
        except Exception:
            self._attr_available = False

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
    
    def update(self):
        try:
            with open(RATELIMIT_FILE) as f:
                data = json.load(f)
                self._attr_native_value = data.get("limit")
                self._attr_available = self._attr_native_value is not None
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
                temp_data = data.get('outsideTemperature', {})
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
                solar_data = data.get('solarIntensity', {})
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
                weather_data = data.get('weatherState', {})
                self._raw_state = weather_data.get('value')
                self._timestamp = weather_data.get('timestamp')
                self._attr_native_value = WEATHER_STATE_MAP.get(self._raw_state, self._raw_state)
                self._attr_available = self._attr_native_value is not None
        except Exception:
            self._attr_available = False

# ============ Zone Sensors ============

class TadoBaseSensor(SensorEntity):
    """Base class for Tado zone sensors."""
    
    def __init__(self, zone_id: str, zone_name: str):
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._attr_available = False
        self._attr_native_value = None
    
    def _get_zone_data(self):
        """Get zone data from file."""
        try:
            with open(ZONES_FILE) as f:
                data = json.load(f)
                return data.get('zoneStates', {}).get(self._zone_id)
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
    
    def __init__(self, zone_id: str, zone_name: str):
        super().__init__(zone_id, zone_name)
        self._attr_name = f"Tado CE {zone_name} Temperature"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = "measurement"
    
    def _update_from_zone_data(self, zone_data):
        self._attr_native_value = (
            zone_data.get('sensorDataPoints', {})
            .get('insideTemperature', {})
            .get('celsius')
        )

class TadoHumiditySensor(TadoBaseSensor):
    """Humidity sensor."""
    
    def __init__(self, zone_id: str, zone_name: str):
        super().__init__(zone_id, zone_name)
        self._attr_name = f"Tado CE {zone_name} Humidity"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_humidity"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = "measurement"
    
    def _update_from_zone_data(self, zone_data):
        self._attr_native_value = (
            zone_data.get('sensorDataPoints', {})
            .get('humidity', {})
            .get('percentage')
        )

class TadoHeatingPowerSensor(TadoBaseSensor):
    """Heating power sensor."""
    
    def __init__(self, zone_id: str, zone_name: str):
        super().__init__(zone_id, zone_name)
        self._attr_name = f"Tado CE {zone_name} Heating"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_heating"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_icon = "mdi:radiator"
        self._attr_state_class = "measurement"
    
    def _update_from_zone_data(self, zone_data):
        power = (
            zone_data.get('activityDataPoints', {})
            .get('heatingPower', {})
            .get('percentage')
        )
        self._attr_native_value = power if power is not None else 0

class TadoACPowerSensor(TadoBaseSensor):
    """AC power sensor."""
    
    def __init__(self, zone_id: str, zone_name: str):
        super().__init__(zone_id, zone_name)
        self._attr_name = f"Tado CE {zone_name} AC Power"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_ac_power"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_icon = "mdi:air-conditioner"
        self._attr_state_class = "measurement"
    
    def _update_from_zone_data(self, zone_data):
        power = (
            zone_data.get('activityDataPoints', {})
            .get('acPower', {})
            .get('percentage')
        )
        self._attr_native_value = power if power is not None else 0

class TadoTargetTempSensor(TadoBaseSensor):
    """Target temperature sensor."""
    
    def __init__(self, zone_id: str, zone_name: str):
        super().__init__(zone_id, zone_name)
        self._attr_name = f"Tado CE {zone_name} Target"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_target"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_icon = "mdi:thermometer-check"
    
    def _update_from_zone_data(self, zone_data):
        setting = zone_data.get('setting', {})
        if setting.get('power') == 'ON':
            self._attr_native_value = setting.get('temperature', {}).get('celsius')
        else:
            self._attr_native_value = None

class TadoOverlaySensor(TadoBaseSensor):
    """Overlay status sensor (Manual/Schedule)."""
    
    def __init__(self, zone_id: str, zone_name: str):
        super().__init__(zone_id, zone_name)
        self._attr_name = f"Tado CE {zone_name} Mode"
        self._attr_unique_id = f"tado_ce_{zone_name.lower().replace(' ', '_')}_mode"
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
        power = zone_data.get('setting', {}).get('power')
        
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
            setting = next_change.get('setting')
            if setting:
                temp = setting.get('temperature')
                self._next_temp = temp.get('celsius') if temp else None
            else:
                self._next_temp = None
        else:
            self._next_change = None
            self._next_temp = None

# ============ Device Sensors ============

class TadoBatterySensor(SensorEntity):
    """Battery status sensor."""
    
    def __init__(self, zone_id: str, zone_name: str, device: dict):
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._device_serial = device.get('shortSerialNo', 'unknown')
        self._device_type = device.get('deviceType', 'unknown')
        
        self._attr_name = f"Tado CE {zone_name} Battery"
        self._attr_unique_id = f"tado_ce_{self._device_serial}_battery"
        self._attr_icon = "mdi:battery"
        self._attr_available = True
        self._attr_native_value = device.get('batteryState', 'unknown')
        
        # Extra attributes
        self._firmware = device.get('currentFwVersion')
        self._connection_state = device.get('connectionState', {}).get('value')
        self._connection_timestamp = device.get('connectionState', {}).get('timestamp')
    
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
                            conn = device.get('connectionState', {})
                            self._connection_state = conn.get('value')
                            self._connection_timestamp = conn.get('timestamp')
                            self._attr_available = True
                            return
            self._attr_available = False
        except Exception:
            self._attr_available = False

class TadoDeviceConnectionSensor(SensorEntity):
    """Device connection state sensor."""
    
    def __init__(self, zone_name: str, device: dict):
        self._device_serial = device.get('shortSerialNo', 'unknown')
        self._device_type = device.get('deviceType', 'unknown')
        self._zone_name = zone_name
        
        self._attr_name = f"Tado CE {zone_name} Connection"
        self._attr_unique_id = f"tado_ce_{self._device_serial}_connection"
        self._attr_icon = "mdi:wifi"
        self._attr_available = True
        
        conn = device.get('connectionState', {})
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
                            conn = device.get('connectionState', {})
                            self._attr_native_value = "Online" if conn.get('value') else "Offline"
                            self._connection_timestamp = conn.get('timestamp')
                            self._firmware = device.get('currentFwVersion')
                            self._attr_available = True
                            return
            self._attr_available = False
        except Exception:
            self._attr_available = False
