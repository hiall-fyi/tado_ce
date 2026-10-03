"""Tado CE bridge API client: read boiler wiring state, set flow temperature."""

from __future__ import annotations

from http import HTTPStatus
import logging

import aiohttp

from .exceptions import TadoBridgeApiError
from .helpers import async_retry_with_backoff, mask_serial

_LOGGER = logging.getLogger(__name__)

_BRIDGE_API_BASE = "https://my.tado.com/api/v2/homeByBridge"

# HTTP timeout for Bridge API calls (10s: simple GET/PUT operations)
_BRIDGE_API_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Temperature constraints (matches Tado app)
MIN_FLOW_TEMP = 25.0
MAX_FLOW_TEMP = 80.0
FLOW_TEMP_STEP = 0.5


def validate_flow_temperature(celsius: float) -> float:
    """Snap a flow-temperature request to the nearest 0.5°C inside the bridge's range.

    Raises `TadoBridgeApiError` when the requested value is outside
    25.0-80.0°C: the bridge will reject values outside this range and
    catching it here gives the caller a clearer message.
    """
    if celsius < MIN_FLOW_TEMP or celsius > MAX_FLOW_TEMP:
        msg = "Flow temperature %s°C outside valid range %s-%s°C"
        raise TadoBridgeApiError(msg % (celsius, MIN_FLOW_TEMP, MAX_FLOW_TEMP))
    return round(celsius * 2) / 2


class TadoBridgeApiClient:
    """Talk to one Tado bridge for boiler wiring state and flow-temperature control."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        bridge_serial: str,
        auth_key: str,
    ) -> None:
        """Initialise the client bound to one bridge serial + auth key."""
        self._session = session
        self._bridge_serial = bridge_serial
        self._auth_key = auth_key
        self._base_url = f"{_BRIDGE_API_BASE}/{bridge_serial}"

    async def async_get_wiring_state(self) -> dict[str, object]:
        """Fetch the boiler wiring-installation state from the bridge."""
        url = f"{self._base_url}/boilerWiringInstallationState"
        params = {"authKey": self._auth_key}
        _LOGGER.debug(
            "Bridge: GET wiring state from %s",
            url.replace(self._bridge_serial, mask_serial(self._bridge_serial))
               .replace(self._auth_key, "[AUTH_KEY]"),
        )

        async def _do_get() -> dict[str, object]:
            async with self._session.get(url, params=params, timeout=_BRIDGE_API_TIMEOUT) as resp:
                _LOGGER.debug("Bridge: wiring-state response status %s", resp.status)
                if resp.status != HTTPStatus.OK:
                    msg = "Bridge API GET wiring state failed: HTTP %s"
                    raise TadoBridgeApiError(msg % resp.status)
                result = await resp.json()
                _LOGGER.debug("Bridge: wiring-state response had %d field(s)", len(result))
                return result  # type: ignore[no-any-return]

        try:
            return await async_retry_with_backoff(
                _do_get,
                no_retry_exceptions=(TadoBridgeApiError,),
                retryable_exceptions=(aiohttp.ClientError, TimeoutError),
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = "Bridge API network error: %s"
            raise TadoBridgeApiError(msg % (str(err) or type(err).__name__)) from err

    async def async_set_max_output_temperature(self, celsius: float) -> bool:
        """Set the boiler max output temperature on the bridge (idempotent PUT)."""
        snapped = validate_flow_temperature(celsius)
        url = f"{self._base_url}/boilerMaxOutputTemperature"
        params = {"authKey": self._auth_key}
        payload = {"boilerMaxOutputTemperatureInCelsius": snapped}

        async def _do_put() -> bool:
            async with self._session.put(url, params=params, json=payload, timeout=_BRIDGE_API_TIMEOUT) as resp:
                if resp.status not in (HTTPStatus.OK, HTTPStatus.NO_CONTENT):
                    msg = "Bridge API PUT max temp failed: HTTP %s"
                    raise TadoBridgeApiError(msg % resp.status)
                return True

        try:
            return await async_retry_with_backoff(
                _do_put,
                no_retry_exceptions=(TadoBridgeApiError,),
                retryable_exceptions=(aiohttp.ClientError, TimeoutError),
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = "Bridge API network error: %s"
            _LOGGER.debug("Bridge: PUT max-temp network error: %s", err)
            raise TadoBridgeApiError(msg % (str(err) or type(err).__name__)) from err

    async def async_validate_credentials(self) -> bool:
        """Probe the bridge with a wiring-state read to confirm the auth key works."""
        try:
            await self.async_get_wiring_state()
        except TadoBridgeApiError:
            return False
        return True
