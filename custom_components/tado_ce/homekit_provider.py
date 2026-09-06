"""Tado CE HomeKit local provider: push-driven cache + write path through the bridge."""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CACHE_REFRESH_FAILURE_THRESHOLD,
    HOMEKIT_CACHE_REFRESH_SECONDS,
    HOMEKIT_WRITE_TIMEOUT_SECONDS,
    SIGNAL_HOMEKIT_UPDATE,
)
from .helpers import mask_home_id
from .homekit_client import (
    CHAR_CURRENT_HUMIDITY,
    CHAR_CURRENT_TEMPERATURE,
    CHAR_IDENTIFY,
    CHAR_SERIAL_NUMBER,
    CHAR_TARGET_HEATING_STATE,
    CHAR_TARGET_TEMPERATURE,
    HomeKitClient,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# HAP RESOURCE_NOT_EXIST: a subscribed characteristic iid no longer resolves,
# the signature of a stale iid cache after a bridge config-number bump.
_HAP_RESOURCE_NOT_EXIST = -70409

# aiohomekit fires an EMPTY event dict on every (re)connect. 5 of those
# inside 60s means it is spinning reconnects, not settling after one.
_SPIN_PULSE_THRESHOLD = 5
_SPIN_PULSE_WINDOW_SECONDS = 60


def _find_char_iid(
    accessories: list[dict[str, Any]],
    aid: int,
    char_type: str,
) -> int | None:
    """Return the characteristic iid for `(aid, char_type)`, or None when absent.

    aiohomekit normalises type UUIDs to the full UUID form, so we
    strip the suffix and compare as ints to handle leading-zero
    variants (e.g. "0F" vs "F").
    """
    for acc in accessories:
        if acc.get("aid") != aid:
            continue
        for svc in acc.get("services", []):
            for char in svc.get("characteristics", []):
                raw_type = char.get("type", "")
                if "-" in raw_type:
                    ctype = raw_type.split("-")[0].upper()
                else:
                    ctype = raw_type.upper()
                try:
                    if int(ctype, 16) == int(char_type, 16):
                        iid = char.get("iid")
                        return int(iid) if iid is not None else None
                except (ValueError, TypeError):
                    continue
    return None


class HomeKitLocalProvider:
    """Push-driven cache + write path against a paired Tado HomeKit bridge.

    Read-side: maintains a `(value, last_changed_at, last_observed_at)`
    tuple per `(zone_id, char_type)` so the state reconciler can
    distinguish "value hasn't changed" from "we haven't seen a
    fresh sample". Write-side: temperature and HVAC mode go via
    `pairing.put_characteristics` with a strict timeout so a stalled
    bridge doesn't hang the user's set-temperature service call.
    """

    def __init__(self, client: HomeKitClient, hass: HomeAssistant, home_id: str) -> None:
        """Initialise the provider, ready to subscribe once HomeKit is connected."""
        self._client = client
        self._hass = hass
        self._home_id = home_id
        self._cache: dict[str, dict[str, tuple[Any, datetime | None, datetime]]] = {}
        self._accessories: list[dict[str, Any]] = []
        self._unsub_dispatcher: Any | None = None
        self._event_map: dict[tuple[int, int], tuple[str, str]] = {}
        self._cache_refresh_task: asyncio.Task[None] | None = None
        self._subscribe_lock = asyncio.Lock()
        self._reconnect_pulse_times: deque[float] = deque(maxlen=_SPIN_PULSE_THRESHOLD)

    @property
    def is_connected(self) -> bool:
        """Return True when the underlying HomeKit pairing is connected."""
        return self._client.is_connected

    async def async_refresh_accessories(self) -> None:
        """Refresh the cached accessory list from the bridge."""
        self._accessories = await self._client.async_list_accessories()

    def update_cache(
        self,
        zone_id: str,
        char_type: str,
        value: Any,
        observed_at: datetime | None = None,
        *,
        from_poll: bool = False,
    ) -> None:
        """Update the cache, advancing `last_observed_at` even when the value is unchanged.

        `last_changed_at` only advances on a real value change so
        the state reconciler can tell "stable reading" from "no
        new data". Keep-alive writes (same value) should still call
        this to refresh `last_observed_at`.

        `from_poll=True` for a reading we fetched, False for one the bridge pushed. A fetched
        first reading may have been constant for weeks, so it is not an observed change.
        """
        now = observed_at or dt_util.utcnow()
        if zone_id not in self._cache:
            self._cache[zone_id] = {}
        prev = self._cache[zone_id].get(char_type)
        if prev is None:
            changed_at = None if from_poll else now
        elif prev[0] == value:
            # Same value: keep last_changed_at, advance last_observed_at only.
            changed_at = prev[1]
        else:
            changed_at = now
        self._cache[zone_id][char_type] = (value, changed_at, now)

    def get_temperature(
        self, zone_id: str,
    ) -> tuple[float | None, datetime | None, datetime | None]:
        """Return `(celsius, last_changed_at, last_observed_at)` for the zone."""
        entry = self._cache.get(zone_id, {}).get(CHAR_CURRENT_TEMPERATURE)
        if entry is None:
            return None, None, None
        return entry

    def get_humidity(
        self, zone_id: str,
    ) -> tuple[float | None, datetime | None, datetime | None]:
        """Return `(percentage, last_changed_at, last_observed_at)` for the zone."""
        entry = self._cache.get(zone_id, {}).get(CHAR_CURRENT_HUMIDITY)
        if entry is None:
            return None, None, None
        return entry

    def get_target_temperature(
        self, zone_id: str,
    ) -> tuple[float | None, datetime | None, datetime | None]:
        """Return `(target_celsius, last_changed_at, last_observed_at)` for the zone."""
        entry = self._cache.get(zone_id, {}).get(CHAR_TARGET_TEMPERATURE)
        if entry is None:
            return None, None, None
        return entry

    async def set_temperature(self, zone_id: str, temperature: float) -> bool:
        """Write `temperature` to the zone's TRV via HomeKit, returning success."""
        if not self._client.is_connected or not self._client.pairing:
            return False

        aids = self._client.get_aids_for_zone(zone_id)
        if not aids:
            _LOGGER.debug(
                "HomeKit: zone %s has no mapped accessory, cannot set "
                "temperature via local bridge",
                zone_id,
            )
            return False

        aid = aids[0]
        iid = _find_char_iid(self._accessories, aid, CHAR_TARGET_TEMPERATURE)
        if iid is None:
            _LOGGER.debug(
                "HomeKit: aid %d has no target-temperature characteristic, "
                "falling back to cloud write",
                aid,
            )
            return False

        try:
            async with asyncio.timeout(HOMEKIT_WRITE_TIMEOUT_SECONDS):
                result = await self._client.pairing.put_characteristics([(aid, iid, temperature)])
            if not result:
                # aiohomekit returns an empty dict on success.
                self.update_cache(zone_id, CHAR_TARGET_TEMPERATURE, temperature)
                _LOGGER.debug(
                    "HomeKit: zone %s target temperature set to %.1f°C "
                    "via local bridge",
                    zone_id, temperature,
                )
                return True
            _LOGGER.warning(
                "HomeKit: zone %s target-temperature write rejected by "
                "the bridge: %s",
                zone_id, result,
            )
            return False
        except TimeoutError:
            _LOGGER.warning(
                "HomeKit: zone %s target-temperature write timed out "
                "after %ds, falling back to cloud write",
                zone_id, HOMEKIT_WRITE_TIMEOUT_SECONDS,
            )
            return False
        except Exception:
            _LOGGER.debug(
                "HomeKit: zone %s target-temperature write raised an "
                "exception, falling back to cloud write",
                zone_id, exc_info=True,
            )
            return False

    async def set_hvac_mode(self, zone_id: str, mode: int) -> bool:
        """Write `mode` (0=Off, 1=Heat, 2=Cool, 3=Auto) to the zone, returning success."""
        if not self._client.is_connected or not self._client.pairing:
            return False

        aids = self._client.get_aids_for_zone(zone_id)
        if not aids:
            _LOGGER.debug(
                "HomeKit: zone %s has no mapped accessory, cannot set "
                "HVAC mode via local bridge",
                zone_id,
            )
            return False

        aid = aids[0]
        iid = _find_char_iid(self._accessories, aid, CHAR_TARGET_HEATING_STATE)
        if iid is None:
            _LOGGER.debug(
                "HomeKit: aid %d has no target-heating-state characteristic, "
                "falling back to cloud write",
                aid,
            )
            return False

        try:
            async with asyncio.timeout(HOMEKIT_WRITE_TIMEOUT_SECONDS):
                result = await self._client.pairing.put_characteristics([(aid, iid, mode)])
            if not result:
                _LOGGER.debug(
                    "HomeKit: zone %s HVAC mode set to %d via local bridge",
                    zone_id, mode,
                )
                return True
            _LOGGER.warning(
                "HomeKit: zone %s HVAC-mode write rejected by the bridge "
                ": %s",
                zone_id, result,
            )
            return False
        except TimeoutError:
            _LOGGER.warning(
                "HomeKit: zone %s HVAC-mode write timed out after %ds, "
                "falling back to cloud write",
                zone_id, HOMEKIT_WRITE_TIMEOUT_SECONDS,
            )
            return False
        except Exception:
            _LOGGER.debug(
                "HomeKit: zone %s HVAC-mode write raised an exception, "
                "falling back to cloud write",
                zone_id, exc_info=True,
            )
            return False

    def _find_aid_for_serial(self, serial: str) -> int | None:
        """Return the accessory aid whose serial-number characteristic matches `serial`."""
        for acc in self._accessories:
            for svc in acc.get("services", []):
                for char in svc.get("characteristics", []):
                    raw_type = char.get("type", "")
                    ctype = raw_type.split("-")[0].upper() if "-" in raw_type else raw_type.upper()
                    try:
                        if int(ctype, 16) == int(CHAR_SERIAL_NUMBER, 16) and char.get("value") == serial:
                            aid = acc.get("aid")
                            return int(aid) if aid is not None else None
                    except (ValueError, TypeError):
                        continue
        return None

    async def async_identify(self, serial: str) -> bool:
        """Flash a device's LED locally via its HomeKit Identify characteristic.

        Returns False (so the caller can fall back to the cloud identify call)
        when the serial isn't mapped, the accessory has no Identify
        characteristic, or the write times out / errors.
        """
        if not self._client.is_connected or not self._client.pairing:
            return False

        aid = self._find_aid_for_serial(serial)
        if aid is None:
            _LOGGER.debug(
                "HomeKit: serial %s has no mapped accessory, cannot "
                "identify via local bridge", serial,
            )
            return False

        iid = _find_char_iid(self._accessories, aid, CHAR_IDENTIFY)
        if iid is None:
            _LOGGER.debug(
                "HomeKit: aid %d has no Identify characteristic, "
                "falling back to cloud identify", aid,
            )
            return False

        try:
            async with asyncio.timeout(HOMEKIT_WRITE_TIMEOUT_SECONDS):
                result = await self._client.pairing.put_characteristics([(aid, iid, True)])
            if not result:
                _LOGGER.debug("HomeKit: identify flashed device %s via local bridge", serial)
                return True
            _LOGGER.warning(
                "HomeKit: identify write for %s rejected by the bridge: %s",
                serial, result,
            )
            return False
        except TimeoutError:
            _LOGGER.warning(
                "HomeKit: identify write for %s timed out after %ds, "
                "falling back to cloud identify", serial, HOMEKIT_WRITE_TIMEOUT_SECONDS,
            )
            return False
        except Exception:
            _LOGGER.debug(
                "HomeKit: identify write for %s raised an exception, "
                "falling back to cloud identify", serial, exc_info=True,
            )
            return False

    async def async_subscribe_events(self) -> None:
        """Subscribe to characteristic events for every mapped zone."""
        async with self._subscribe_lock:
            if not self._client.is_connected or not self._client.pairing:
                _LOGGER.debug(
                    "HomeKit: cannot subscribe, bridge is not connected, "
                    "will retry once the connection comes back",
                )
                return

            if not self._accessories:
                await self.async_refresh_accessories()

            if not self._accessories:
                # `async_list_accessories` swallows every bridge error and
                # returns [], so an empty list here means the bridge did not
                # answer, a transient condition and exactly the one a
                # config-number bump creates (the bridge is reconfiguring).
                # Keep the live event map and poll loop so the stale-iid
                # counter can still drive recovery, and escalate the way the
                # subscribe-failure branch does.
                _LOGGER.warning(
                    "HomeKit: bridge listed no accessories, keeping the "
                    "current local device map and triggering a reconnect, "
                    "local control will recover once the bridge answers again",
                )
                await self._client.async_reconnect()
                return

            subscribe_chars: list[tuple[int, int]] = []
            # Build into a local map and commit it only once the bridge has
            # accepted the subscription: a rebuild that fails partway must not
            # leave the poll loop and the event handler reading an emptied map.
            event_map: dict[tuple[int, int], tuple[str, str]] = {}
            char_types = (
                CHAR_CURRENT_TEMPERATURE,
                CHAR_CURRENT_HUMIDITY,
                CHAR_TARGET_TEMPERATURE,
            )

            for zone_id, aids in self._client.zone_aid_map.items():
                for aid in aids:
                    for char_type in char_types:
                        iid = _find_char_iid(self._accessories, aid, char_type)
                        if iid is not None:
                            subscribe_chars.append((aid, iid))
                            event_map[(aid, iid)] = (zone_id, char_type)

            if not subscribe_chars:
                # The bridge answered but exposes none of the tracked
                # characteristics. Unlike the empty-accessories case this is
                # persistent, so a reconnect could not change the outcome.
                _LOGGER.debug(
                    "HomeKit: no subscribable characteristics found, "
                    "skipping event subscription",
                )
                return

            try:
                await self._client.pairing.subscribe(subscribe_chars)
            except Exception:
                # A bridge that drops between connect and subscribe must not
                # propagate to the caller (setup, poll-retry, or reconnect
                # callback) and crash it. Self-protect like the other
                # bridge-touching methods: reconnect (its callback re-subscribes)
                # and return. Broad catch is deliberate; exc_info keeps a real
                # bug visible.
                _LOGGER.warning(
                    "HomeKit: subscribing to bridge events failed, triggering a "
                    "reconnect, local control will recover once the bridge is "
                    "reachable again",
                )
                _LOGGER.debug("HomeKit: subscribe error details", exc_info=True)
                await self._client.async_reconnect()
                return

            # The bridge accepted the subscription, so the rebuilt map is now
            # the live one.
            self._event_map = event_map
            # Tear down the previous dispatcher on reconnect; without
            # this, every reconnect leaks a live callback and each
            # bridge event triggers N duplicate state updates.
            if self._unsub_dispatcher is not None:
                self._unsub_dispatcher()
                self._unsub_dispatcher = None
            self._unsub_dispatcher = self._client.pairing.dispatcher_connect(
                self._on_event_callback,
            )
            _LOGGER.info(
                "HomeKit: subscribed to %d characteristic(s) across %d zone(s)",
                len(subscribe_chars),
                len(self._client.zone_aid_map),
            )

            # Start the periodic cache refresh so stable readings still
            # advance `last_observed_at`.
            if self._cache_refresh_task is not None:
                self._cache_refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._cache_refresh_task
            self._cache_refresh_task = asyncio.create_task(
                self._periodic_cache_refresh(),
            )

    def _on_event_callback(self, event_data: dict[tuple[int, int], dict[str, Any]]) -> None:
        """Apply pushed event values to the cache and fan out one signal per zone."""
        if not event_data:
            self._record_reconnect_pulse()
            return
        updated_zones: set[str] = set()
        for key, data in event_data.items():
            mapping = self._event_map.get(key)
            if mapping is None:
                continue
            zone_id, char_type = mapping
            value = data.get("value")
            if value is not None:
                old_entry = self._cache.get(zone_id, {}).get(char_type)
                old_value = old_entry[0] if old_entry else None
                self.update_cache(zone_id, char_type, value)
                updated_zones.add(zone_id)
                if value != old_value:
                    _LOGGER.debug(
                        "HomeKit: zone %s %s changed %s → %s (event push)",
                        zone_id, char_type, old_value, value,
                    )
        signal = SIGNAL_HOMEKIT_UPDATE.format(home_id=self._home_id)
        for zone_id in updated_zones:
            async_dispatcher_send(self._hass, signal, zone_id)

    def _record_reconnect_pulse(self) -> None:
        """Record a connect pulse and trigger spin recovery once the threshold trips.

        An EMPTY_EVENT pulse means the connection just (re)established. Several in a
        short window means aiohomekit is spinning, not reconnecting once.
        """
        now = time.monotonic()
        self._reconnect_pulse_times.append(now)
        _LOGGER.debug(
            "HomeKit: reconnect pulse for home %s (%d in window)",
            mask_home_id(self._home_id), len(self._reconnect_pulse_times),
        )
        if (
            len(self._reconnect_pulse_times) == self._reconnect_pulse_times.maxlen
            and now - self._reconnect_pulse_times[0] < _SPIN_PULSE_WINDOW_SECONDS
        ):
            self._reconnect_pulse_times.clear()
            self._client.async_schedule_spin_recovery()

    async def _periodic_cache_refresh(self) -> None:
        """Periodically poll subscribed characteristics so stable values still age fresh.

        The HomeKit bridge only pushes events on value change. Without
        this poll, stable readings would go stale (> 5 min) and the
        state reconciler would fall back to cloud. Triggers a reconnect
        after `CACHE_REFRESH_FAILURE_THRESHOLD` consecutive failures.
        """
        consecutive_failures = 0
        stale_warned = False
        first_refresh = True
        while True:
            await asyncio.sleep(HOMEKIT_CACHE_REFRESH_SECONDS)
            if not self._client.is_connected or not self._client.pairing:
                continue
            try:
                chars_to_read = list(self._event_map.keys())
                if not chars_to_read:
                    continue
                result = await self._client.pairing.get_characteristics(chars_to_read)
                updated_zones: set[str] = set()
                changes_found = 0
                stale_found = 0
                for key, data in result.items():
                    mapping = self._event_map.get(key)
                    if mapping is None:
                        continue
                    zone_id, char_type = mapping
                    value = data.get("value")
                    if value is None:
                        if data.get("status") == _HAP_RESOURCE_NOT_EXIST:
                            stale_found += 1
                        continue
                    old_entry = self._cache.get(zone_id, {}).get(char_type)
                    old_value = old_entry[0] if old_entry else None
                    self.update_cache(zone_id, char_type, value, from_poll=True)
                    updated_zones.add(zone_id)
                    if value != old_value:
                        changes_found += 1
                        _LOGGER.debug(
                            "HomeKit: zone %s %s changed %s → %s (poll refresh)",
                            zone_id, char_type, old_value, value,
                        )
                if stale_found:
                    consecutive_failures += 1
                    if not stale_warned:
                        _LOGGER.warning(
                            "HomeKit: %d characteristic(s) returning stale-resource "
                            "errors for home %s, bridge may have reconfigured. "
                            "Reconnecting.",
                            stale_found, mask_home_id(self._home_id),
                        )
                        stale_warned = True
                    if consecutive_failures >= CACHE_REFRESH_FAILURE_THRESHOLD:
                        await self._client.async_reconnect()
                        consecutive_failures = 0
                else:
                    consecutive_failures = 0
                    stale_warned = False
                signal = SIGNAL_HOMEKIT_UPDATE.format(home_id=self._home_id)
                for zone_id in updated_zones:
                    async_dispatcher_send(self._hass, signal, zone_id)
                if first_refresh:
                    _LOGGER.debug(
                        "HomeKit: first cache refresh complete, polled "
                        "%d characteristic(s)",
                        len(chars_to_read),
                    )
                    first_refresh = False
                elif changes_found > 0:
                    _LOGGER.debug(
                        "HomeKit: cache refresh: %d value(s) changed",
                        changes_found,
                    )
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= CACHE_REFRESH_FAILURE_THRESHOLD:
                    _LOGGER.warning(
                        "HomeKit: %d consecutive cache-refresh failures, "
                        "reconnecting to the bridge",
                        consecutive_failures,
                    )
                    await self._client.async_reconnect()
                    consecutive_failures = 0
                else:
                    _LOGGER.debug(
                        "HomeKit: cache refresh failed "
                        "(%d/%d before reconnect)",
                        consecutive_failures,
                        CACHE_REFRESH_FAILURE_THRESHOLD,
                    )

    def unsubscribe_events(self) -> None:
        """Cancel the periodic refresh and disconnect the dispatcher."""
        if self._cache_refresh_task is not None:
            self._cache_refresh_task.cancel()
            self._cache_refresh_task = None
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None
            _LOGGER.debug("HomeKit: unsubscribed from bridge events")
