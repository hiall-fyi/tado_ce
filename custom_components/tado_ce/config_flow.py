"""Config flow for Tado CE with device authorization."""
import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CLIENT_ID, DATA_DIR, CONFIG_FILE

_LOGGER = logging.getLogger(__name__)

# Tado OAuth endpoints
DEVICE_AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
ME_URL = "https://my.tado.com/api/v2/me"


class TadoCEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tado CE."""

    VERSION = 4

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
        self._poll_task = None

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
            DEVICE_AUTH_URL,
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
        """Show authorization URL for user to click."""
        if user_input is not None:
            # User clicked Submit, start polling
            return await self.async_step_poll()

        return self.async_show_form(
            step_id="authorize",
            data_schema=vol.Schema({}),
            description_placeholders={
                "url": self._verify_url,
                "code": self._user_code,
            },
        )

    async def async_step_poll(self, user_input: dict[str, Any] | None = None):
        """Poll for authorization completion."""
        # Start polling task if not already running
        if self._poll_task is None:
            self._poll_task = self.hass.async_create_task(
                self._poll_for_authorization()
            )
        
        # Check if task is already done
        if self._poll_task.done():
            return self.async_show_progress_done(next_step_id="poll_done")
        
        # Show progress spinner with task
        return self.async_show_progress(
            step_id="poll",
            progress_action="poll",
            progress_task=self._poll_task,
        )

    async def async_step_poll_done(self, user_input: dict[str, Any] | None = None):
        """Handle completion of polling task."""
        try:
            result = self._poll_task.result()
            if result == "success":
                return await self.async_step_select_home()
            elif result == "timeout":
                return self.async_abort(reason="timeout")
            else:
                return self.async_abort(reason="authorization_failed")
        except Exception as e:
            _LOGGER.error(f"Poll task error: {e}")
            return self.async_abort(reason="authorization_failed")

    async def _poll_for_authorization(self) -> str:
        """Background task to poll for authorization."""
        session = async_get_clientsession(self.hass)
        max_attempts = self._expires_in // self._interval
        
        _LOGGER.debug(f"Starting poll for authorization, max_attempts={max_attempts}, interval={self._interval}")
        
        for attempt in range(max_attempts):
            await asyncio.sleep(self._interval)
            
            _LOGGER.debug(f"Poll attempt {attempt + 1}/{max_attempts}")
            
            try:
                async with session.post(
                    TOKEN_URL,
                    data={
                        "client_id": CLIENT_ID,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": self._device_code
                    }
                ) as resp:
                    _LOGGER.debug(f"Poll response status: {resp.status}")
                    
                    if resp.status == 200:
                        data = await resp.json()
                        self._access_token = data.get("access_token")
                        self._refresh_token = data.get("refresh_token")
                        
                        if self._access_token and self._refresh_token:
                            _LOGGER.info("Authorization successful!")
                            await self._fetch_homes()
                            return "success"
                    
                    elif resp.status == 400:
                        data = await resp.json()
                        error = data.get("error", "")
                        _LOGGER.debug(f"Poll error response: {error}")
                        
                        if error == "authorization_pending":
                            continue
                        elif error == "slow_down":
                            self._interval += 5
                            _LOGGER.debug(f"Slowing down, new interval: {self._interval}")
                            continue
                        elif error == "expired_token":
                            _LOGGER.debug("Device code expired")
                            return "timeout"
                        else:
                            _LOGGER.error(f"Authorization error: {error}")
                            return "error"
                            
            except Exception as e:
                _LOGGER.error(f"Poll error: {e}")
                continue
        
        _LOGGER.debug("Authorization timed out")
        return "timeout"

    async def _fetch_homes(self):
        """Fetch available homes from Tado API."""
        session = async_get_clientsession(self.hass)
        
        async with session.get(
            ME_URL,
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
        
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        
        _LOGGER.info(f"Saved credentials for home: {home_name} (ID: {home_id})")
        
        return self.async_create_entry(
            title=f"Tado CE ({home_name})",
            data={"home_id": str(home_id)},
        )

    async def async_step_timeout(self, user_input: dict[str, Any] | None = None):
        """Handle authorization timeout."""
        return self.async_abort(reason="timeout")

    async def async_step_error(self, user_input: dict[str, Any] | None = None):
        """Handle authorization error."""
        return self.async_abort(reason="authorization_failed")


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
