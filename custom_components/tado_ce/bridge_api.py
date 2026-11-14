"""Tado CE bridge API client: read boiler wiring state, set flow temperature."""

from __future__ import annotations

from http import HTTPStatus
import logging
from typing import TYPE_CHECKING

import aiohttp

from .const import MECH_BRIDGE_API_CALL
from .exceptions import TadoBridgeApiError
from .helpers import async_retry_with_backoff, mask_serial

if TYPE_CHECKING:
    from .mechanism_counters import MechanismCounters

_LOGGER = logging.getLogger(__name__)

_BRIDGE_API_BASE = "https://my.tado.com/api/v2/homeByBridge"

# HTTP timeout for Bridge API calls (10s: simple GET/PUT operations)
_BRIDGE_API_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Temperature constraints (matches Tado app)
MIN_FLOW_TEMP = 30.0
MAX_FLOW_TEMP = 80.0
FLOW_TEMP_STEP = 0.5


def validate_flow_temperature(celsius: float) -> float:
    """Snap a flow-temperature request to the nearest 0.5°C inside the bridge's range.

    Raises `TadoBridgeApiError` when the requested value is outside
    30.0-80.0°C: the bridge will reject values outside this range and
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
        *,
        counters: MechanismCounters | None = None,
    ) -> None:
        """Initialise the client bound to one bridge serial + auth key."""
        self._session = session
        self._bridge_serial = bridge_serial
        self._auth_key = auth_key
        self._counters = counters
        self._base_url = f"{_BRIDGE_API_BASE}/{bridge_serial}"

    def _record(self, resp: aiohttp.ClientResponse, method: str) -> None:
        """Count the call per day by method and status and debug-log the response's rate-limit headers."""
        limits = {k: v for k, v in resp.headers.items() if k.lower().startswith(("ratelimit", "x-quota"))}
        if limits:
            _LOGGER.debug("Bridge: response rate-limit headers %s", limits)
        if self._counters is not None:
            self._counters.count(MECH_BRIDGE_API_CALL, f"{method}:{int(resp.status)}", calls=1)

    def _network_error(self, err: BaseException) -> TadoBridgeApiError:
        """Wrap a transport error with the auth key and serial removed from its text."""
        text = str(err) or type(err).__name__
        # An empty value would make str.replace rewrite every character
        if self._auth_key:
            text = text.replace(self._auth_key, "[AUTH_KEY]")
        if self._bridge_serial:
            text = text.replace(self._bridge_serial, mask_serial(self._bridge_serial))
        msg = "Bridge API network error: %s"
        return TadoBridgeApiError(msg % text)

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
                self._record(resp, "GET")
                _LOGGER.debug("Bridge: wiring-state response status %s", resp.status)
                if resp.status != HTTPStatus.OK:
                    msg = "Bridge API GET wiring state failed: HTTP %s"
                    raise TadoBridgeApiError(msg % resp.status)
                try:
                    result = await resp.json()
                except (ValueError, aiohttp.ContentTypeError):
                    # A body that is not JSON is a bridge fault, not a retryable network error
                    msg = "Bridge API GET wiring state returned a body that is not JSON: HTTP %s"
                    raise TadoBridgeApiError(msg % resp.status) from None
                if not isinstance(result, dict):
                    msg = "Bridge API GET wiring state returned an unusable body: HTTP %s"
                    raise TadoBridgeApiError(msg % resp.status)
                _LOGGER.debug("Bridge: wiring-state response had %d field(s)", len(result))
                return result

        try:
            return await async_retry_with_backoff(
                _do_get,
                no_retry_exceptions=(TadoBridgeApiError,),
                retryable_exceptions=(aiohttp.ClientError, TimeoutError),
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise self._network_error(err) from None

    async def async_get_max_output_temperature(self) -> float:
        """Fetch the boiler max output temperature from the bridge's own endpoint."""
        url = f"{self._base_url}/boilerMaxOutputTemperature"
        params = {"authKey": self._auth_key}
        _LOGGER.debug(
            "Bridge: GET max output temperature from %s",
            url.replace(self._bridge_serial, mask_serial(self._bridge_serial))
               .replace(self._auth_key, "[AUTH_KEY]"),
        )

        async def _do_get() -> float:
            async with self._session.get(url, params=params, timeout=_BRIDGE_API_TIMEOUT) as resp:
                self._record(resp, "GET")
                if resp.status != HTTPStatus.OK:
                    msg = "Bridge API GET max temp failed: HTTP %s"
                    raise TadoBridgeApiError(msg % resp.status)
                try:
                    body = await resp.json()
                except (ValueError, aiohttp.ContentTypeError):
                    # A body that is not JSON is a bridge fault, not a retryable network error
                    msg = "Bridge API GET max temp returned a body that is not JSON: HTTP %s"
                    raise TadoBridgeApiError(msg % resp.status) from None
                value = body.get("boilerMaxOutputTemperatureInCelsius") if isinstance(body, dict) else None
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    msg = "Bridge API GET max temp returned an unusable body"
                    raise TadoBridgeApiError(msg)
                return float(value)

        try:
            return await async_retry_with_backoff(
                _do_get,
                no_retry_exceptions=(TadoBridgeApiError,),
                retryable_exceptions=(aiohttp.ClientError, TimeoutError),
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise self._network_error(err) from None

    async def async_set_max_output_temperature(self, celsius: float) -> bool:
        """Set the boiler max output temperature on the bridge (idempotent PUT)."""
        snapped = validate_flow_temperature(celsius)
        url = f"{self._base_url}/boilerMaxOutputTemperature"
        params = {"authKey": self._auth_key}
        payload = {"boilerMaxOutputTemperatureInCelsius": snapped}

        async def _do_put() -> bool:
            async with self._session.put(url, params=params, json=payload, timeout=_BRIDGE_API_TIMEOUT) as resp:
                self._record(resp, "PUT")
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
            wrapped = self._network_error(err)
            _LOGGER.debug("Bridge: PUT max-temp network error: %s", wrapped)
            raise wrapped from None

    async def async_validate_credentials(self) -> bool:
        """Probe the bridge with a wiring-state read to confirm the auth key works."""
        try:
            await self.async_get_wiring_state()
        except TadoBridgeApiError:
            return False
        return True
