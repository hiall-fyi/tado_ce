"""Tado CE Water Heater Platform."""
import asyncio
import json
import logging
from datetime import timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant

from .const import (
    ZONES_FILE, ZONES_INFO_FILE, CONFIG_FILE,
    TADO_API_BASE, TADO_AUTH_URL, CLIENT_ID
)
from .device_manager import get_zone_device_info
from .auth_manager import get_auth_manager

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)

# Operation modes for hot water
STATE_AUTO = "auto"  # Follow schedule (no overlay)
STATE_HEAT = "heat"  # Timer or manual heating
OPERATION_MODES = [STATE_AUTO, STATE_HEAT, STATE_OFF]


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


def _get_access_token():
    """Get access token using centralized AuthManager."""
    from .auth_manager import get_auth_manager
    auth_manager = get_auth_manager(CONFIG_FILE, CLIENT_ID, TADO_AUTH_URL)
    return auth_manager.get_access_token()


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tado CE water heater from a config entry."""
    _LOGGER.debug("Tado CE water_heater: Setting up...")
    zones_info = await hass.async_add_executor_job(_load_zones_info_file)
    
    water_heaters = []
    
    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_name = zone.get('name', f"Zone {zone_id}")
            zone_type = zone.get('type')
            
            if zone_type == 'HOT_WATER':
                water_heaters.append(TadoWaterHeater(hass, zone_id, zone_name))
    
    if water_heaters:
        async_add_entities(water_heaters, True)
        _LOGGER.info(f"Tado CE water heaters loaded: {len(water_heaters)}")
    else:
        _LOGGER.info("Tado CE: No hot water zones found")


class TadoWaterHeater(WaterHeaterEntity):
    """Tado CE Water Heater Entity."""
    
    def __init__(self, hass: HomeAssistant, zone_id: str, zone_name: str):
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = None
        
        self._attr_name = zone_name
        # Use zone_id for unique_id to maintain entity_id stability across zone name changes
        self._attr_unique_id = f"tado_ce_zone_{zone_id}_water_heater"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = WaterHeaterEntityFeature.OPERATION_MODE
        self._attr_operation_list = OPERATION_MODES
        self._attr_min_temp = 30
        self._attr_max_temp = 65
        # Use zone device info instead of hub device info
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HOT_WATER")
        
        self._attr_current_operation = None
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_available = False
        
        self._overlay_type = None

    @property
    def extra_state_attributes(self):
        return {
            "overlay_type": self._overlay_type,
            "zone_id": self._zone_id,
        }

    def update(self):
        """Update water heater state from JSON file."""
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
                self._home_id = config.get("home_id")
            
            with open(ZONES_FILE) as f:
                data = json.load(f)
                zone_data = data.get('zoneStates', {}).get(self._zone_id)
                
                if not zone_data:
                    self._attr_available = False
                    return
                
                setting = zone_data.get('setting', {})
                power = setting.get('power')
                overlay = zone_data.get('overlay')
                self._overlay_type = zone_data.get('overlayType')
                
                # Detect current operation mode based on overlay state
                if not overlay or self._overlay_type is None:
                    # No overlay = following schedule
                    self._attr_current_operation = STATE_AUTO
                elif self._overlay_type == 'TIMER':
                    # Timer overlay = HEAT mode
                    self._attr_current_operation = STATE_HEAT
                elif self._overlay_type == 'MANUAL':
                    if power == 'OFF':
                        # Manual OFF = OFF mode
                        self._attr_current_operation = STATE_OFF
                    else:
                        # Manual ON = HEAT mode
                        self._attr_current_operation = STATE_HEAT
                else:
                    # Unknown overlay type, default to AUTO
                    _LOGGER.debug(f"Unknown overlay type: {self._overlay_type}, defaulting to AUTO")
                    self._attr_current_operation = STATE_AUTO
                
                self._attr_available = True
                
        except Exception as e:
            _LOGGER.debug(f"Failed to update {self.name}: {e}")
            self._attr_available = False

    async def async_set_operation_mode(self, operation_mode: str):
        """Set new operation mode with retry logic (async).
        
        CRITICAL FIX: Converted to async to prevent event loop blocking.
        Uses await asyncio.sleep() instead of time.sleep().
        All blocking I/O operations are wrapped with async_add_executor_job.
        """
        # Store previous operation mode for rollback on failure
        previous_mode = self._attr_current_operation
        success = False
        max_retries = 2  # Initial attempt + 1 retry
        
        for attempt in range(max_retries):
            if operation_mode == STATE_AUTO:
                # AUTO mode: Delete overlay to follow schedule
                success = await self.hass.async_add_executor_job(self._resume_schedule_blocking)
                if success:
                    self._attr_current_operation = STATE_AUTO
                    await self._async_trigger_immediate_refresh("hot_water_auto")
                    break
            elif operation_mode == STATE_HEAT:
                # HEAT mode: Turn on with timer
                duration = self._get_timer_duration()
                success = await self.hass.async_add_executor_job(self._set_timer_blocking, duration, None)
                if success:
                    self._attr_current_operation = STATE_HEAT
                    await self._async_trigger_immediate_refresh("hot_water_heat")
                    break
            elif operation_mode == STATE_OFF:
                # OFF mode: Turn off with manual overlay
                success = await self.hass.async_add_executor_job(self._turn_off_blocking)
                if success:
                    self._attr_current_operation = STATE_OFF
                    await self._async_trigger_immediate_refresh("hot_water_off")
                    break
            
            # If failed and not last attempt, wait and retry
            if not success and attempt < max_retries - 1:
                _LOGGER.warning(
                    f"Failed to set operation mode to {operation_mode} (attempt {attempt + 1}/{max_retries}), "
                    f"retrying in 5 seconds..."
                )
                # CRITICAL FIX: Use async sleep instead of blocking time.sleep()
                await asyncio.sleep(5)
        
        if not success:
            _LOGGER.error(
                f"Failed to set operation mode to {operation_mode} after {max_retries} attempts. "
                f"Keeping previous mode: {previous_mode}"
            )
            # Restore previous operation mode
            self._attr_current_operation = previous_mode
    
    def set_operation_mode(self, operation_mode: str):
        """Set new operation mode (sync wrapper for tests/backward compatibility).
        
        This is a blocking wrapper that waits for the async operation to complete.
        For production use, prefer async_set_operation_mode().
        """
        import asyncio
        # Run the async version and wait for completion
        asyncio.run(self.async_set_operation_mode(operation_mode))

    
    def _get_timer_duration(self) -> int:
        """Get configured timer duration in minutes (default 60)."""
        try:
            # Try to get from hass.data
            from .const import DOMAIN
            if DOMAIN in self.hass.data and 'config_manager' in self.hass.data[DOMAIN]:
                config_manager = self.hass.data[DOMAIN]['config_manager']
                return config_manager.get_hot_water_timer_duration()
        except Exception as e:
            _LOGGER.debug(f"Failed to get timer duration from config: {e}")
        
        # Default to 60 minutes
        return 60
    
    async def _async_trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change (async).
        
        CRITICAL FIX: Async version to work with async_set_operation_mode.
        """
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            await handler.trigger_refresh(self.entity_id, reason)
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")
    
    def _trigger_immediate_refresh(self, reason: str):
        """Trigger immediate refresh after state change (sync wrapper).
        
        DEPRECATED: Use _async_trigger_immediate_refresh() instead.
        """
        try:
            from .immediate_refresh_handler import get_handler
            handler = get_handler(self.hass)
            # Use call_soon_threadsafe to schedule the coroutine from sync context
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(
                    handler.trigger_refresh(self.entity_id, reason)
                )
            )
        except Exception as e:
            _LOGGER.debug(f"Failed to trigger immediate refresh: {e}")

    def _turn_on_blocking(self) -> bool:
        """Turn on hot water (blocking I/O - use with async_add_executor_job)."""
        if not self._home_id:
            _LOGGER.error("No home_id configured")
            return False
        
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "HOT_WATER",
                    "power": "ON"
                },
                "termination": {"type": "MANUAL"}
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Turned on {self._zone_name}")
                self._attr_current_operation = STATE_HEAT
                return True
                
        except Exception as e:
            _LOGGER.error(f"Failed to turn on hot water: {e}")
            return False

    def _turn_off_blocking(self) -> bool:
        """Turn off hot water (blocking I/O - use with async_add_executor_job)."""
        if not self._home_id:
            _LOGGER.error("No home_id configured for hot water zone")
            return False
        
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token - authentication may be required")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            payload = {
                "setting": {
                    "type": "HOT_WATER",
                    "power": "OFF"
                },
                "termination": {"type": "MANUAL"}
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Turned off {self._zone_name}")
                self._attr_current_operation = STATE_OFF
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while turning off hot water: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while turning off hot water: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while turning off hot water: {e}")
            return False
    
    def _turn_on(self) -> bool:
        """Turn on hot water (public API for backward compatibility).
        
        DEPRECATED: This is a wrapper for backward compatibility.
        Internal code should use _turn_on_blocking() with async_add_executor_job.
        """
        return self._turn_on_blocking()
    
    def _turn_off(self) -> bool:
        """Turn off hot water (public API for backward compatibility).
        
        DEPRECATED: This is a wrapper for backward compatibility.
        Internal code should use _turn_off_blocking() with async_add_executor_job.
        """
        return self._turn_off_blocking()

    def _set_timer_blocking(self, duration_minutes: int, temperature: float = None) -> bool:
        """Turn on hot water with timer (blocking I/O - use with async_add_executor_job)."""
        if not self._home_id:
            _LOGGER.error("No home_id configured for hot water zone")
            return False
        
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token - authentication may be required")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            
            # Build setting payload
            setting = {
                "type": "HOT_WATER",
                "power": "ON"
            }
            
            # Add temperature if provided (for solar water heater systems)
            if temperature is not None:
                setting["temperature"] = {
                    "celsius": temperature
                }
            
            payload = {
                "setting": setting,
                "termination": {
                    "type": "TIMER",
                    "durationInSeconds": duration_minutes * 60
                }
            }
            
            data = json.dumps(payload).encode()
            req = Request(url, data=data, method="PUT")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            
            with urlopen(req, timeout=10) as resp:
                temp_str = f" at {temperature}°C" if temperature is not None else ""
                _LOGGER.info(f"Turned on {self._zone_name} for {duration_minutes} minutes{temp_str}")
                self._attr_current_operation = STATE_HEAT
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while setting hot water timer: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while setting hot water timer: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while setting hot water timer: {e}")
            return False
    
    def set_timer(self, duration_minutes: int, temperature: float = None) -> bool:
        """Turn on hot water with timer (public API for backward compatibility).
        
        DEPRECATED: This is a wrapper for backward compatibility.
        Internal code should use _set_timer_blocking() with async_add_executor_job.
        """
        return self._set_timer_blocking(duration_minutes, temperature)

    def _resume_schedule_blocking(self) -> bool:
        """Resume hot water schedule (blocking I/O - use with async_add_executor_job)."""
        if not self._home_id:
            _LOGGER.error("No home_id configured for hot water zone")
            return False
        
        try:
            token = _get_access_token()
            if not token:
                _LOGGER.error("Failed to get access token - authentication may be required")
                return False
            
            url = f"{TADO_API_BASE}/homes/{self._home_id}/zones/{self._zone_id}/overlay"
            req = Request(url, method="DELETE")
            req.add_header("Authorization", f"Bearer {token}")
            
            with urlopen(req, timeout=10) as resp:
                _LOGGER.info(f"Resumed schedule for {self._zone_name}")
                return True
                
        except HTTPError as e:
            if e.code == 401:
                _LOGGER.error(f"Authentication failed (401) - please re-authenticate: {e}")
            elif e.code == 429:
                _LOGGER.error(f"Rate limit exceeded (429) - too many API calls: {e}")
            else:
                _LOGGER.error(f"HTTP error {e.code} while resuming schedule: {e}")
            return False
        except URLError as e:
            _LOGGER.error(f"Network error while resuming schedule: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error while resuming schedule: {e}")
            return False
    
    def resume_schedule(self) -> bool:
        """Resume hot water schedule (public API for backward compatibility).
        
        DEPRECATED: This is a wrapper for backward compatibility.
        Internal code should use _resume_schedule_blocking() with async_add_executor_job.
        """
        return self._resume_schedule_blocking()
