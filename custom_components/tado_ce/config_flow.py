"""Config flow for Tado CE with device authorization."""
import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN, CLIENT_ID, DATA_DIR, CONFIG_FILE,
    API_ENDPOINT_ME, AUTH_ENDPOINT_DEVICE, AUTH_ENDPOINT_TOKEN
)

_LOGGER = logging.getLogger(__name__)


class TadoCEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tado CE."""

    VERSION = 5

    def __init__(self):
        """Initialize the config flow."""
        self._device_code: str | None = None
        self._user_code: str | None = None
        self._verify_url: str | None = None
        self._interval: int = 5
        self._expires_in: int = 300
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._homes: list[dict] = []
        self._check_count: int = 0

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return TadoCEOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step - start device authorization."""
        await self.async_set_unique_id("tado_ce_integration")
        self._abort_if_unique_id_configured()

        errors = {}

        if user_input is not None:
            try:
                await self._request_device_code()
                # Show URL for user to click
                return await self.async_step_authorize()
            except Exception as e:
                _LOGGER.error(f"Failed to start authorization: {e}")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def _request_device_code(self):
        """Request device code from Tado."""
        session = async_get_clientsession(self.hass)
        
        async with session.post(
            AUTH_ENDPOINT_DEVICE,
            data={
                "client_id": CLIENT_ID,
                "scope": "home.user offline_access"
            }
        ) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to get device code: {resp.status}")
            
            data = await resp.json()
            self._device_code = data.get("device_code")
            self._user_code = data.get("user_code")
            self._verify_url = data.get("verification_uri_complete")
            self._interval = data.get("interval", 5)
            self._expires_in = data.get("expires_in", 300)
            
            if not self._device_code:
                raise Exception("No device code in response")

    async def async_step_authorize(self, user_input: dict[str, Any] | None = None):
        """Show authorization URL and wait for user to authorize."""
        errors = {}
        
        if user_input is not None:
            # User clicked Submit - check if they've authorized
            self._check_count += 1
            _LOGGER.debug(f"Checking authorization status (attempt {self._check_count})")
            
            result = await self._check_authorization()
            
            if result == "success":
                _LOGGER.info("Authorization successful!")
                return await self.async_step_select_home()
            elif result == "pending":
                # Still waiting - show form again with hint
                errors["base"] = "auth_pending"
            elif result == "expired":
                return self.async_abort(reason="timeout")
            else:
                errors["base"] = "authorization_failed"

        return self.async_show_form(
            step_id="authorize",
            data_schema=vol.Schema({}),
            description_placeholders={
                "url": self._verify_url,
                "code": self._user_code,
            },
            errors=errors,
        )

    async def _check_authorization(self) -> str:
        """Check if user has completed authorization."""
        session = async_get_clientsession(self.hass)
        
        try:
            async with session.post(
                AUTH_ENDPOINT_TOKEN,
                data={
                    "client_id": CLIENT_ID,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": self._device_code
                }
            ) as resp:
                _LOGGER.debug(f"Authorization check response status: {resp.status}")
                
                if resp.status == 200:
                    data = await resp.json()
                    self._access_token = data.get("access_token")
                    self._refresh_token = data.get("refresh_token")
                    
                    if self._access_token and self._refresh_token:
                        await self._fetch_homes()
                        return "success"
                    return "error"
                
                elif resp.status == 400:
                    data = await resp.json()
                    error = data.get("error", "")
                    _LOGGER.debug(f"Authorization check error: {error}")
                    
                    if error == "authorization_pending":
                        return "pending"
                    elif error == "slow_down":
                        # Wait a bit before allowing next check
                        await asyncio.sleep(2)
                        return "pending"
                    elif error == "expired_token":
                        return "expired"
                    else:
                        _LOGGER.error(f"Authorization error: {error}")
                        return "error"
                else:
                    return "error"
                    
        except Exception as e:
            _LOGGER.error(f"Authorization check error: {e}")
            return "error"

    async def _fetch_homes(self):
        """Fetch available homes from Tado API."""
        session = async_get_clientsession(self.hass)
        
        async with session.get(
            API_ENDPOINT_ME,
            headers={"Authorization": f"Bearer {self._access_token}"}
        ) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to fetch homes: {resp.status}")
            
            data = await resp.json()
            self._homes = data.get("homes", [])

    async def async_step_select_home(self, user_input: dict[str, Any] | None = None):
        """Handle home selection (if multiple homes)."""
        if not self._homes:
            return self.async_abort(reason="no_homes")

        if len(self._homes) == 1:
            home = self._homes[0]
            return await self._create_entry(home["id"], home.get("name", "Tado Home"))

        if user_input is not None:
            home_id = user_input["home"]
            home_name = next(
                (h.get("name", "Tado Home") for h in self._homes if str(h["id"]) == home_id),
                "Tado Home"
            )
            return await self._create_entry(home_id, home_name)

        home_options = {
            str(home["id"]): home.get("name", f"Home {home['id']}")
            for home in self._homes
        }

        return self.async_show_form(
            step_id="select_home",
            data_schema=vol.Schema({
                vol.Required("home"): vol.In(home_options)
            }),
        )

    async def _create_entry(self, home_id: str, home_name: str):
        """Create the config entry and save credentials."""
        import json
        
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        config = {
            "home_id": str(home_id),
            "refresh_token": self._refresh_token
        }
        
        # Use executor to avoid blocking I/O in event loop
        await self.hass.async_add_executor_job(
            self._save_config_sync, config
        )
        
        _LOGGER.info(f"Saved credentials for home: {home_name} (ID: {home_id})")
        
        return self.async_create_entry(
            title=f"Tado CE ({home_name})",
            data={"home_id": str(home_id)},
        )
    
    def _save_config_sync(self, config: dict):
        """Save config synchronously (for executor)."""
        import json
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)

    # ========== Reconfigure Flow (Re-authenticate) ==========
    
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Handle reconfiguration - allows re-authentication."""
        errors = {}
        
        if user_input is not None:
            try:
                await self._request_device_code()
                return await self.async_step_reconfigure_authorize()
            except Exception as e:
                _LOGGER.error(f"Failed to start re-authorization: {e}")
                errors["base"] = "cannot_connect"
        
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_reconfigure_authorize(self, user_input: dict[str, Any] | None = None):
        """Show authorization URL for reconfigure flow."""
        errors = {}
        
        if user_input is not None:
            self._check_count += 1
            _LOGGER.debug(f"Checking re-authorization status (attempt {self._check_count})")
            
            result = await self._check_authorization()
            
            if result == "success":
                _LOGGER.info("Re-authorization successful!")
                return await self.async_step_reconfigure_confirm()
            elif result == "pending":
                errors["base"] = "auth_pending"
            elif result == "expired":
                return self.async_abort(reason="timeout")
            else:
                errors["base"] = "authorization_failed"
        
        return self.async_show_form(
            step_id="reconfigure_authorize",
            data_schema=vol.Schema({}),
            description_placeholders={
                "url": self._verify_url,
                "code": self._user_code,
            },
            errors=errors,
        )

    async def async_step_reconfigure_confirm(self, user_input: dict[str, Any] | None = None):
        """Save new credentials and finish reconfigure."""
        # Get the existing config entry
        reconfigure_entry = self._get_reconfigure_entry()
        home_id = reconfigure_entry.data.get("home_id")
        
        # If we have homes from the new auth, verify the home still exists
        if self._homes:
            home_exists = any(str(h["id"]) == str(home_id) for h in self._homes)
            if not home_exists:
                # Home no longer exists, let user select a new one
                return await self.async_step_reconfigure_select_home()
        
        # Save new credentials
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        config = {
            "home_id": str(home_id),
            "refresh_token": self._refresh_token
        }
        
        # Use executor to avoid blocking I/O in event loop
        await self.hass.async_add_executor_job(
            self._save_config_sync, config
        )
        
        _LOGGER.info(f"Re-authentication successful, saved new credentials for home ID: {home_id}")
        
        # Finish reconfigure - this updates the existing entry
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_reconfigure_select_home(self, user_input: dict[str, Any] | None = None):
        """Handle home selection during reconfigure (if original home no longer exists)."""
        if not self._homes:
            return self.async_abort(reason="no_homes")
        
        if user_input is not None:
            home_id = user_input["home"]
            
            # Save new credentials with new home
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            
            config = {
                "home_id": str(home_id),
                "refresh_token": self._refresh_token
            }
            
            # Use executor to avoid blocking I/O in event loop
            await self.hass.async_add_executor_job(
                self._save_config_sync, config
            )
            
            _LOGGER.info(f"Re-authentication successful with new home ID: {home_id}")
            
            return self.async_abort(reason="reconfigure_successful")
        
        home_options = {
            str(home["id"]): home.get("name", f"Home {home['id']}")
            for home in self._homes
        }
        
        return self.async_show_form(
            step_id="reconfigure_select_home",
            data_schema=vol.Schema({
                vol.Required("home"): vol.In(home_options)
            }),
        )


class TadoCEOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Tado CE."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        super().__init__()

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}
        
        if user_input is not None:
            processed_input = dict(user_input)
            
            # Handle custom day interval
            day_interval_str = user_input.get('custom_day_interval', '').strip()
            if day_interval_str:
                try:
                    day_interval = int(day_interval_str)
                    if day_interval < 1 or day_interval > 1440:
                        errors['custom_day_interval'] = 'interval_out_of_range'
                    else:
                        processed_input['custom_day_interval'] = day_interval
                except ValueError:
                    errors['custom_day_interval'] = 'invalid_number'
            else:
                processed_input['custom_day_interval'] = None
            
            # Handle custom night interval
            night_interval_str = user_input.get('custom_night_interval', '').strip()
            if night_interval_str:
                try:
                    night_interval = int(night_interval_str)
                    if night_interval < 1 or night_interval > 1440:
                        errors['custom_night_interval'] = 'interval_out_of_range'
                    else:
                        processed_input['custom_night_interval'] = night_interval
                except ValueError:
                    errors['custom_night_interval'] = 'invalid_number'
            else:
                processed_input['custom_night_interval'] = None
            
            if not errors:
                return self.async_create_entry(title="", data=processed_input)

        options = self.config_entry.options
        weather_enabled = options.get('weather_enabled', False)
        mobile_devices_enabled = options.get('mobile_devices_enabled', False)
        mobile_devices_frequent_sync = options.get('mobile_devices_frequent_sync', False)
        offset_enabled = options.get('offset_enabled', False)
        test_mode_enabled = options.get('test_mode_enabled', False)
        day_start_hour = options.get('day_start_hour', 7)
        night_start_hour = options.get('night_start_hour', 23)
        custom_day_interval = options.get('custom_day_interval')
        custom_night_interval = options.get('custom_night_interval')
        api_history_retention_days = options.get('api_history_retention_days', 14)
        hot_water_timer_duration = options.get('hot_water_timer_duration', 60)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional('weather_enabled', default=weather_enabled): bool,
                vol.Optional('mobile_devices_enabled', default=mobile_devices_enabled): bool,
                vol.Optional('mobile_devices_frequent_sync', default=mobile_devices_frequent_sync): bool,
                vol.Optional('offset_enabled', default=offset_enabled): bool,
                vol.Optional('test_mode_enabled', default=test_mode_enabled): bool,
                vol.Optional('api_history_retention_days', default=api_history_retention_days): vol.All(
                    int, vol.Range(min=0, max=365)
                ),
                vol.Required('day_start_hour', default=day_start_hour): vol.All(
                    int, vol.Range(min=0, max=23)
                ),
                vol.Required('night_start_hour', default=night_start_hour): vol.All(
                    int, vol.Range(min=0, max=23)
                ),
                vol.Optional('custom_day_interval', default=str(custom_day_interval) if custom_day_interval else ""): str,
                vol.Optional('custom_night_interval', default=str(custom_night_interval) if custom_night_interval else ""): str,
                vol.Optional('hot_water_timer_duration', default=hot_water_timer_duration): vol.All(
                    int, vol.Range(min=5, max=1440)
                ),
            }),
            errors=errors,
        )
