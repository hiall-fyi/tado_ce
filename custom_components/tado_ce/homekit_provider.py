"""Tado CE HomeKit local provider: push-driven cache + write path through the bridge."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from enum import StrEnum
import logging
import time
from typing import TYPE_CHECKING, Any, Final

from aiohomekit.exceptions import AccessoryDisconnectedError, HttpErrorResponse
import aiohttp
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CACHE_REFRESH_FAILURE_THRESHOLD,
    HOMEKIT_BURST_GAP_SECONDS,
    HOMEKIT_CACHE_REFRESH_SECONDS,
    HOMEKIT_LOOP_RECONNECT_MIN_GAP_SECONDS,
    HOMEKIT_WRITE_TIMEOUT_SECONDS,
    SIGNAL_HOMEKIT_UPDATE,
)
from .helpers import mask_home_id, mask_serial
from .homekit_client import (
    CHAR_CURRENT_HEATING_STATE,
    CHAR_CURRENT_HUMIDITY,
    CHAR_CURRENT_TEMPERATURE,
    CHAR_IDENTIFY,
    CHAR_SERIAL_NUMBER,
    CHAR_TARGET_HEATING_STATE,
    CHAR_TARGET_TEMPERATURE,
    HomeKitClient,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_AccessoryKey = tuple[int, int]
_CacheEntry = tuple[Any, datetime | None, datetime]
_NO_KEY: _AccessoryKey = (-1, -1)

# HAP RESOURCE_NOT_EXIST: a subscribed characteristic iid no longer resolves,
# the signature of a stale iid cache after a bridge config-number bump.
_HAP_RESOURCE_NOT_EXIST = -70409
_NO_READ_ERROR = object()
_READ_ERROR_WARN_SECONDS: Final = 3600
# Target heating state 0x33: 0 is off, 1 is on; the auto and cool values give no power.
_HOMEKIT_POWER_NAMES: Final = {0: "OFF", 1: "ON"}
_CHAR_LABELS: Final = {
    CHAR_CURRENT_TEMPERATURE: "current temperature",
    CHAR_CURRENT_HUMIDITY: "humidity",
    CHAR_TARGET_TEMPERATURE: "target temperature",
    CHAR_CURRENT_HEATING_STATE: "heating state",
    CHAR_TARGET_HEATING_STATE: "target heating state",
}

# aiohomekit fires an EMPTY event dict on every (re)connect. 5 of those
# inside 60s means it is spinning reconnects, not settling after one.
_SPIN_PULSE_THRESHOLD = 5
_SPIN_PULSE_WINDOW_SECONDS = 60

# A write raising one of these may already have reached the bridge; aiohomekit's disconnect errors are not OSError.
HOMEKIT_UNKNOWN_OUTCOME_ERRORS = (TimeoutError, aiohttp.ClientError, OSError, AccessoryDisconnectedError)


class PutOutcome(StrEnum):
    """What one HomeKit put told us about the write."""

    ACCEPTED = "accepted"
    REFUSED = "refused"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


def _outcome_for_error(err: Exception) -> PutOutcome:
    """Classify a put that raised; a 4xx answer is a definite refusal though aiohomekit files it under the disconnect errors."""
    if isinstance(err, HttpErrorResponse) or not isinstance(err, HOMEKIT_UNKNOWN_OUTCOME_ERRORS):
        return PutOutcome.REFUSED
    return PutOutcome.AMBIGUOUS


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
    tuple per `(zone_id, char_type, accessory)` and folds them into one
    zone value on read, so the state reconciler can distinguish "value
    hasn't changed" from "we haven't seen a fresh sample". Write-side: temperature and HVAC mode go via
    `pairing.put_characteristics` with a strict timeout so a stalled
    bridge doesn't hang the user's set-temperature service call.
    """

    def __init__(self, client: HomeKitClient, hass: HomeAssistant, home_id: str) -> None:
        """Initialise the provider, ready to subscribe once HomeKit is connected."""
        self._client = client
        self._hass = hass
        self._home_id = home_id
        self._cache: dict[str, dict[str, dict[_AccessoryKey | None, _CacheEntry]]] = {}
        self._ambiguous_zones: set[str] = set()
        self._read_error_logged: dict[tuple[int, int], int | None] = {}
        # Next time a read error that keeps repeating warns, per characteristic (time.monotonic).
        self._read_error_warn_at: dict[tuple[int, int], float] = {}
        self._no_mapped_accessory_warned = False
        # Kept here because the read loop restarts after each reconnect and would forget its back-off.
        self._loop_backoff_level = 0
        self._failure_warned_level: int | None = None
        self._accessories: list[dict[str, Any]] = []
        self._unsub_dispatcher: Any | None = None
        self._event_map: dict[tuple[int, int], tuple[str, str]] = {}
        self._cache_refresh_task: asyncio.Task[None] | None = None
        self._subscribe_lock = asyncio.Lock()
        self._unsubscribe_count = 0
        self._reconnect_pulse_times: deque[float] = deque(maxlen=_SPIN_PULSE_THRESHOLD)
        self.put_rejection_listener: Callable[[str, int, int], None] | None = None
        self.change_listener: Callable[[str, datetime], None] | None = None
        self._last_put_at: float | None = None
        self._last_put_position = 0
        self._logged_subscribe_rejections: set[tuple[int, int, int]] = set()

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
        accessory_key: _AccessoryKey | None = None,
    ) -> None:
        """Update one accessory's entry; `last_changed_at` moves only when that accessory's own value changes."""
        # A fetched first reading (from_poll) may have been constant for weeks, so it is not a change.
        now = observed_at or dt_util.utcnow()
        entries = self._cache.setdefault(zone_id, {}).setdefault(char_type, {})
        prev = entries.get(accessory_key)
        if prev is None:
            changed_at = None if from_poll else now
        elif prev[0] == value:
            # Same value: keep last_changed_at, advance last_observed_at only.
            changed_at = prev[1]
        else:
            changed_at = now
        entries[accessory_key] = (value, changed_at, now)

    def _entry(self, zone_id: str, char_type: str, key: _AccessoryKey) -> _CacheEntry | None:
        """Return one accessory's cached entry, or None when it has none yet."""
        return self._cache.get(zone_id, {}).get(char_type, {}).get(key)

    @staticmethod
    def _latest_changes(entries: dict[_AccessoryKey | None, _CacheEntry]) -> list[_CacheEntry]:
        """Return the entries that share the newest genuine change time."""
        changed = [entry for entry in entries.values() if entry[1] is not None]
        if not changed:
            return []
        latest = max(entry[1] for entry in changed if entry[1] is not None)
        return [entry for entry in changed if entry[1] == latest]

    @staticmethod
    def _all_agree(entries: list[_CacheEntry]) -> bool:
        """Return True when every entry holds the same value."""
        return all(entry[0] == entries[0][0] for entry in entries)

    def _zone_value(
        self, zone_id: str, char_type: str,
    ) -> tuple[Any, datetime | None, datetime | None]:
        """Fold the per-accessory entries into the zone's `(value, changed, observed)`."""
        entries = self._cache.get(zone_id, {}).get(char_type)
        if not entries:
            return None, None, None
        if char_type != CHAR_TARGET_TEMPERATURE:
            # Latest sample wins; equal sample times fall to the greatest accessory key.
            return max(
                entries.items(),
                key=lambda item: (item[1][2], item[0] if item[0] is not None else _NO_KEY),
            )[1]
        observed = max(entry[2] for entry in entries.values())
        candidates = self._latest_changes(entries)
        if candidates:
            # Same-batch disagreement has no winner; None sends the reconciler to the cloud value.
            if self._all_agree(candidates):
                return candidates[0][0], candidates[0][1], observed
            return None, None, observed
        everything = list(entries.values())
        if self._all_agree(everything):
            return everything[0][0], None, observed
        return None, None, observed

    def _note_target_ambiguity(self, zone_id: str) -> None:
        """Log once when the zone's newest target changes start to disagree."""
        candidates = self._latest_changes(self._cache.get(zone_id, {}).get(CHAR_TARGET_TEMPERATURE, {}))
        if candidates and not self._all_agree(candidates):
            if zone_id not in self._ambiguous_zones:
                self._ambiguous_zones.add(zone_id)
                _LOGGER.debug(
                    "HomeKit: zone %s accessories disagree on a new target, using the cloud value",
                    zone_id,
                )
        else:
            self._ambiguous_zones.discard(zone_id)

    def _prune_cache(self, event_map: dict[_AccessoryKey, tuple[str, str]]) -> None:
        """Drop entries whose accessory is no longer mapped to the same zone and characteristic."""
        for zone_id in list(self._cache):
            chars = self._cache[zone_id]
            for char_type in list(chars):
                entries = chars[char_type]
                for key in list(entries):
                    if key is None or event_map.get(key) != (zone_id, char_type):
                        del entries[key]
                if not entries:
                    del chars[char_type]
            if not chars:
                del self._cache[zone_id]
                self._ambiguous_zones.discard(zone_id)

    def drop_zone(self, zone_id: str) -> None:
        """Forget a zone's event-map entries, cached values and ambiguity flag."""
        for key in [k for k, (zid, _char) in self._event_map.items() if zid == zone_id]:
            del self._event_map[key]
        self._cache.pop(zone_id, None)
        self._ambiguous_zones.discard(zone_id)

    def get_temperature(
        self, zone_id: str,
    ) -> tuple[float | None, datetime | None, datetime | None]:
        """Return `(celsius, last_changed_at, last_observed_at)` for the zone."""
        return self._zone_value(zone_id, CHAR_CURRENT_TEMPERATURE)

    def get_humidity(
        self, zone_id: str,
    ) -> tuple[float | None, datetime | None, datetime | None]:
        """Return `(percentage, last_changed_at, last_observed_at)` for the zone."""
        return self._zone_value(zone_id, CHAR_CURRENT_HUMIDITY)

    def get_target_temperature(
        self, zone_id: str,
    ) -> tuple[float | None, datetime | None, datetime | None]:
        """Return `(target_celsius, last_changed_at, last_observed_at)` for the zone."""
        return self._zone_value(zone_id, CHAR_TARGET_TEMPERATURE)

    def homekit_power(self, zone_id: str) -> tuple[str | None, datetime | None, datetime | None]:
        """Return `("ON"|"OFF", changed, observed)` when every mapped accessory has an entry and they agree, else all None."""
        keys = [k for k, (z, c) in self._event_map.items() if z == zone_id and c == CHAR_TARGET_HEATING_STATE]
        entries = [self._entry(zone_id, CHAR_TARGET_HEATING_STATE, key) for key in keys]
        present = [entry for entry in entries if entry is not None]
        if not present or len(present) != len(entries) or not self._all_agree(present):
            return None, None, None
        power = _HOMEKIT_POWER_NAMES.get(present[0][0]) if isinstance(present[0][0], int) else None
        if power is None:
            return None, None, None
        changes = [entry[1] for entry in present if entry[1] is not None]
        return power, max(changes) if changes else None, max(entry[2] for entry in present)

    def _next_burst_position(self) -> int:
        """Return this put's place in the chain of puts each within the burst gap of the previous one."""
        now = time.monotonic()
        in_chain = self._last_put_at is not None and now - self._last_put_at <= HOMEKIT_BURST_GAP_SECONDS
        self._last_put_position = self._last_put_position + 1 if in_chain else 1
        self._last_put_at = now
        return self._last_put_position

    def _report_rejection(self, zone_id: str, result: dict[Any, Any], position: int) -> None:
        """Tell the listener the status of every rejected characteristic in a put result."""
        if self.put_rejection_listener is None:
            return
        for entry in result.values():
            status = entry.get("status") if isinstance(entry, dict) else None
            if isinstance(status, int):
                self.put_rejection_listener(zone_id, status, position)

    def has_target_characteristic(self, zone_id: str) -> bool:
        """Return True when the zone has a mapped accessory exposing the target-temperature characteristic."""
        aids = self._client.get_aids_for_zone(zone_id)
        if not aids:
            return False
        return _find_char_iid(self._accessories, aids[0], CHAR_TARGET_TEMPERATURE) is not None

    async def set_temperature(self, zone_id: str, temperature: float) -> PutOutcome:
        """Write `temperature` to the zone's TRV via HomeKit and say whether it was accepted, refused, may have landed or could not be sent."""
        if not self._client.is_connected or not self._client.pairing:
            return PutOutcome.UNAVAILABLE

        aids = self._client.get_aids_for_zone(zone_id)
        if not aids:
            _LOGGER.debug(
                "HomeKit: zone %s has no mapped accessory, cannot set "
                "temperature via local bridge",
                zone_id,
            )
            return PutOutcome.UNAVAILABLE

        aid = aids[0]
        iid = _find_char_iid(self._accessories, aid, CHAR_TARGET_TEMPERATURE)
        if iid is None:
            _LOGGER.debug(
                "HomeKit: aid %d has no target-temperature characteristic, "
                "falling back to cloud write",
                aid,
            )
            return PutOutcome.UNAVAILABLE

        position = self._next_burst_position()
        try:
            async with asyncio.timeout(HOMEKIT_WRITE_TIMEOUT_SECONDS):
                result = await self._client.pairing.put_characteristics([(aid, iid, temperature)])
            if not result:
                # aiohomekit returns an empty dict on success.
                self.update_cache(
                    zone_id, CHAR_TARGET_TEMPERATURE, temperature, accessory_key=(aid, iid),
                )
                _LOGGER.debug(
                    "HomeKit: zone %s target temperature set to %.1f°C "
                    "via local bridge",
                    zone_id, temperature,
                )
                return PutOutcome.ACCEPTED
            _LOGGER.warning(
                "HomeKit: zone %s target-temperature write rejected by "
                "the bridge: %s",
                zone_id, result,
            )
            self._report_rejection(zone_id, result, position)
            return PutOutcome.REFUSED
        except TimeoutError:
            _LOGGER.warning(
                "HomeKit: zone %s target-temperature write timed out "
                "after %ds, falling back to cloud write",
                zone_id, HOMEKIT_WRITE_TIMEOUT_SECONDS,
            )
            return PutOutcome.AMBIGUOUS
        except Exception as err:
            _LOGGER.debug(
                "HomeKit: zone %s target-temperature write raised an "
                "exception, falling back to cloud write",
                zone_id, exc_info=True,
            )
            return _outcome_for_error(err)

    async def set_hvac_mode(self, zone_id: str, mode: int) -> PutOutcome:
        """Write `mode` (0=Off, 1=Heat, 2=Cool, 3=Auto) to the zone and say whether it was accepted, refused, may have landed or could not be sent."""
        if not self._client.is_connected or not self._client.pairing:
            return PutOutcome.UNAVAILABLE

        aids = self._client.get_aids_for_zone(zone_id)
        if not aids:
            _LOGGER.debug(
                "HomeKit: zone %s has no mapped accessory, cannot set "
                "HVAC mode via local bridge",
                zone_id,
            )
            return PutOutcome.UNAVAILABLE

        aid = aids[0]
        iid = _find_char_iid(self._accessories, aid, CHAR_TARGET_HEATING_STATE)
        if iid is None:
            _LOGGER.debug(
                "HomeKit: aid %d has no target-heating-state characteristic, "
                "falling back to cloud write",
                aid,
            )
            return PutOutcome.UNAVAILABLE

        position = self._next_burst_position()
        try:
            async with asyncio.timeout(HOMEKIT_WRITE_TIMEOUT_SECONDS):
                result = await self._client.pairing.put_characteristics([(aid, iid, mode)])
            if not result:
                self.update_cache(zone_id, CHAR_TARGET_HEATING_STATE, mode, accessory_key=(aid, iid))
                _LOGGER.debug(
                    "HomeKit: zone %s HVAC mode set to %d via local bridge",
                    zone_id, mode,
                )
                return PutOutcome.ACCEPTED
            _LOGGER.warning(
                "HomeKit: zone %s HVAC-mode write rejected by the bridge "
                ": %s",
                zone_id, result,
            )
            self._report_rejection(zone_id, result, position)
            return PutOutcome.REFUSED
        except TimeoutError:
            _LOGGER.warning(
                "HomeKit: zone %s HVAC-mode write timed out after %ds, "
                "falling back to cloud write",
                zone_id, HOMEKIT_WRITE_TIMEOUT_SECONDS,
            )
            return PutOutcome.AMBIGUOUS
        except Exception as err:
            _LOGGER.debug(
                "HomeKit: zone %s HVAC-mode write raised an exception, "
                "falling back to cloud write",
                zone_id, exc_info=True,
            )
            return _outcome_for_error(err)

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
                "identify via local bridge", mask_serial(serial),
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
                _LOGGER.debug("HomeKit: identify flashed device %s via local bridge", mask_serial(serial))
                return True
            _LOGGER.warning(
                "HomeKit: identify write for %s rejected by the bridge: %s",
                mask_serial(serial), result,
            )
            return False
        except TimeoutError:
            _LOGGER.warning(
                "HomeKit: identify write for %s timed out after %ds, "
                "falling back to cloud identify", mask_serial(serial), HOMEKIT_WRITE_TIMEOUT_SECONDS,
            )
            return False
        except Exception:
            _LOGGER.debug(
                "HomeKit: identify write for %s raised an exception, "
                "falling back to cloud identify", mask_serial(serial), exc_info=True,
            )
            return False

    async def set_power_and_target(self, zone_id: str, temperature: float) -> PutOutcome:
        """Switch the zone on (0x33 = 1) and write `temperature` (0x35) in one put, because a lone 0x35 on an OFF zone leaves the power at 0."""
        if not self._client.is_connected or not self._client.pairing:
            return PutOutcome.UNAVAILABLE

        aids = self._client.get_aids_for_zone(zone_id)
        if not aids:
            _LOGGER.debug("HomeKit: zone %s has no mapped accessory, cannot switch it on over the local bridge", zone_id)
            return PutOutcome.UNAVAILABLE

        aid = aids[0]
        mode_iid = _find_char_iid(self._accessories, aid, CHAR_TARGET_HEATING_STATE)
        target_iid = _find_char_iid(self._accessories, aid, CHAR_TARGET_TEMPERATURE)
        if mode_iid is None or target_iid is None:
            _LOGGER.debug("HomeKit: aid %d lacks the target-heating-state or target-temperature characteristic, no combined write", aid)
            return PutOutcome.UNAVAILABLE

        position = self._next_burst_position()
        try:
            async with asyncio.timeout(HOMEKIT_WRITE_TIMEOUT_SECONDS):
                # One put with the power first: two puts inside the burst gap risk a -70402 rejection.
                result = await self._client.pairing.put_characteristics([(aid, mode_iid, 1), (aid, target_iid, temperature)])
            if not result:
                self.update_cache(zone_id, CHAR_TARGET_HEATING_STATE, 1, accessory_key=(aid, mode_iid))
                self.update_cache(zone_id, CHAR_TARGET_TEMPERATURE, temperature, accessory_key=(aid, target_iid))
                _LOGGER.debug("HomeKit: zone %s switched on at %.1f°C via local bridge", zone_id, temperature)
                return PutOutcome.ACCEPTED
            _LOGGER.warning("HomeKit: zone %s power-and-target write rejected by the bridge: %s", zone_id, result)
            self._report_rejection(zone_id, result, position)
            if set(result) == {(aid, target_iid)}:
                # Only the target was rejected, so the power may be on: the next read tells, and the caller holds it as unsure.
                self.update_cache(zone_id, CHAR_TARGET_HEATING_STATE, 1, accessory_key=(aid, mode_iid))
                return PutOutcome.AMBIGUOUS
            return PutOutcome.REFUSED
        except TimeoutError:
            _LOGGER.warning(
                "HomeKit: zone %s power-and-target write timed out after %ds", zone_id, HOMEKIT_WRITE_TIMEOUT_SECONDS,
            )
            return PutOutcome.AMBIGUOUS
        except Exception as err:  # broad on purpose: aiohomekit can raise anything, and the error type sets the outcome
            _LOGGER.debug("HomeKit: zone %s power-and-target write raised an exception", zone_id, exc_info=True)
            return _outcome_for_error(err)

    def _log_rejected_subscriptions(
        self, result: Any, event_map: dict[tuple[int, int], tuple[str, str]],
    ) -> None:
        """Log once per characteristic and status when the bridge refuses to push it."""
        # aiohomekit returns {} on success and None when push is unsupported; only rejected rows carry a status.
        if not isinstance(result, dict):
            return
        for key, row in result.items():
            status = row.get("status") if isinstance(row, dict) else None
            mapping = event_map.get(key)
            if mapping is None or not isinstance(status, int) or status == 0:
                continue
            token = (key[0], key[1], status)
            if token in self._logged_subscribe_rejections:
                continue
            self._logged_subscribe_rejections.add(token)
            _LOGGER.info(
                "HomeKit: the bridge refused events for zone %s characteristic %s (status %s), "
                "reading it every %d s instead",
                mapping[0], mapping[1], status, HOMEKIT_CACHE_REFRESH_SECONDS,
            )

    def _subscribe_superseded(self, generation: int) -> bool:
        """Return True when the subscribe that read `generation` must stop: unsubscribed since, or the client is closing."""
        return self._unsubscribe_count != generation or self._client.is_closing

    async def async_subscribe_events(self) -> None:
        """Subscribe to characteristic events for every mapped zone."""
        generation = self._unsubscribe_count
        async with self._subscribe_lock:
            if self._subscribe_superseded(generation):
                return
            if not self._client.is_connected or not self._client.pairing:
                _LOGGER.debug(
                    "HomeKit: cannot subscribe, bridge is not connected, "
                    "will retry once the connection comes back",
                )
                return

            if not self._accessories:
                await self.async_refresh_accessories()
                if self._subscribe_superseded(generation):
                    return

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
                CHAR_CURRENT_HEATING_STATE,
                CHAR_TARGET_HEATING_STATE,
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
                if self._client.zone_aid_map:
                    if not self._no_mapped_accessory_warned:
                        self._no_mapped_accessory_warned = True
                        _LOGGER.warning(
                            "HomeKit: the bridge is connected but does not list any of the %d accessories "
                            "Tado CE has saved for your zones. Reload the Tado CE integration, or remove "
                            "and add the HomeKit pairing again, to rebuild them.",
                            len(self._client.zone_aid_map),
                        )
                else:
                    _LOGGER.debug(
                        "HomeKit: no subscribable characteristics found, "
                        "skipping event subscription",
                    )
                return

            self._no_mapped_accessory_warned = False
            try:
                subscribe_result = await self._client.pairing.subscribe(subscribe_chars)
            except Exception:
                if self._subscribe_superseded(generation):
                    return
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

            if self._subscribe_superseded(generation):
                return
            # The bridge accepted the subscription, so the rebuilt map is now
            # the live one.
            self._event_map = event_map
            self._log_rejected_subscriptions(subscribe_result, event_map)
            self._prune_cache(event_map)
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
            old, self._cache_refresh_task = self._cache_refresh_task, None
            if old is not None:
                old.cancel()
                # cleared first so the done callback sees a superseded task; wait() keeps a cancel of this task propagating
                await asyncio.wait({old})
            if self._subscribe_superseded(generation):
                return
            self._start_cache_refresh_task()

    def _start_cache_refresh_task(self) -> None:
        """Create the periodic read task with a done-callback that restarts it."""
        # eager_start=False keeps the first step on the next loop iteration, as asyncio.create_task does.
        self._cache_refresh_task = self._hass.async_create_background_task(
            self._periodic_cache_refresh(),
            name=f"tado_ce homekit cache refresh {mask_home_id(self._home_id)}",
            eager_start=False,
        )
        self._cache_refresh_task.add_done_callback(self._on_cache_refresh_task_done)

    def _on_cache_refresh_task_done(self, task: asyncio.Task[None]) -> None:
        """Restart the read loop when it ends while still subscribed and current."""
        # A cancelled, superseded or unsubscribed task ended on purpose.
        if task.cancelled() or task is not self._cache_refresh_task or self._unsub_dispatcher is None:
            return
        error = task.exception()
        if error is None:
            _LOGGER.warning(
                "HomeKit: the periodic read loop for home %s ended without an error, restarting it",
                mask_home_id(self._home_id),
            )
        else:
            _LOGGER.warning(
                "HomeKit: the periodic read loop for home %s ended with %r, restarting it",
                mask_home_id(self._home_id), error,
            )
        self._start_cache_refresh_task()

    def _on_event_callback(self, event_data: dict[tuple[int, int], dict[str, Any]]) -> None:
        """Apply pushed event values to the cache and fan out one signal per zone."""
        if not event_data:
            self._record_reconnect_pulse()
            return
        updated_zones: set[str] = set()
        # One timestamp for the whole batch so simultaneous changes tie.
        observed_at = dt_util.utcnow()
        for key, data in event_data.items():
            mapping = self._event_map.get(key)
            if mapping is None:
                continue
            zone_id, char_type = mapping
            value = data.get("value")
            if value is not None:
                if char_type == CHAR_CURRENT_HEATING_STATE:
                    # Log-only: the heating state feeds no cache, signal or entity.
                    _LOGGER.debug(
                        "HomeKit: zone %s accessory %d heating state %s (event push, not cached)",
                        zone_id, key[0], value,
                    )
                    continue
                old_entry = self._entry(zone_id, char_type, key)
                old_value = old_entry[0] if old_entry else None
                self.update_cache(zone_id, char_type, value, observed_at, accessory_key=key)
                updated_zones.add(zone_id)
                if value != old_value:
                    _LOGGER.debug(
                        "HomeKit: zone %s accessory %d %s changed %s → %s (event push)",
                        zone_id, key[0], char_type, old_value, value,
                    )
                    if self.change_listener is not None and char_type in (CHAR_TARGET_TEMPERATURE, CHAR_TARGET_HEATING_STATE):
                        self.change_listener(zone_id, observed_at)
                else:
                    # An echo of our own write arrives with the value already cached.
                    _LOGGER.debug(
                        "HomeKit: zone %s accessory %d %s reported %s (event push, unchanged)",
                        zone_id, key[0], char_type, value,
                    )
        signal = SIGNAL_HOMEKIT_UPDATE.format(home_id=self._home_id)
        for zone_id in updated_zones:
            self._note_target_ambiguity(zone_id)
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
        """Poll the subscribed characteristics each round so stable values stay fresh, and reconnect after repeated failed rounds."""
        consecutive_failures = 0
        first_refresh = True
        while True:
            await asyncio.sleep(HOMEKIT_CACHE_REFRESH_SECONDS)
            if not self._client.is_connected or not self._client.pairing:
                continue
            try:
                # 0F is event-only: a bad read of it must never count toward a reconnect.
                chars_to_read = [key for key, (_, char_type) in self._event_map.items() if char_type != CHAR_CURRENT_HEATING_STATE]
                if not chars_to_read:
                    continue
                result = await self._client.pairing.get_characteristics(chars_to_read)
                # One timestamp for the whole poll so simultaneous changes tie.
                observed_at = dt_util.utcnow()
                updated_zones: set[str] = set()
                changes_found = 0
                stale_found = 0
                values_read = 0
                for key, data in result.items():
                    mapping = self._event_map.get(key)
                    if mapping is None:
                        continue
                    zone_id, char_type = mapping
                    value = data.get("value")
                    if value is None:
                        status = data.get("status")
                        if status == _HAP_RESOURCE_NOT_EXIST:
                            stale_found += 1
                        else:
                            now = time.monotonic()
                            if self._read_error_logged.get(key, _NO_READ_ERROR) != status:
                                self._read_error_logged[key] = status
                                self._read_error_warn_at[key] = now + _READ_ERROR_WARN_SECONDS
                                _LOGGER.debug(
                                    "HomeKit: reading the %s of zone %s (accessory %d) failed, "
                                    "the bridge reported %s.",
                                    _CHAR_LABELS.get(char_type, char_type), zone_id, key[0],
                                    "no status" if status is None else f"error {status}",
                                )
                            elif now >= self._read_error_warn_at[key]:
                                self._read_error_warn_at[key] = now + _READ_ERROR_WARN_SECONDS
                                _LOGGER.warning(
                                    "HomeKit: reading the %s of zone %s (accessory %d) has been failing for at least an hour (%s)",
                                    _CHAR_LABELS.get(char_type, char_type), zone_id, key[0],
                                    "no status" if status is None else f"error {status}",
                                )
                        continue
                    values_read += 1
                    self._read_error_warn_at.pop(key, None)
                    if self._read_error_logged.pop(key, _NO_READ_ERROR) is not _NO_READ_ERROR:
                        _LOGGER.debug(
                            "HomeKit: zone %s accessory %d %s answers reads again",
                            zone_id, key[0], char_type,
                        )
                    old_entry = self._entry(zone_id, char_type, key)
                    old_value = old_entry[0] if old_entry else None
                    self.update_cache(
                        zone_id, char_type, value, observed_at, from_poll=True, accessory_key=key,
                    )
                    updated_zones.add(zone_id)
                    if value != old_value:
                        changes_found += 1
                        _LOGGER.debug(
                            "HomeKit: zone %s accessory %d %s changed %s → %s (poll refresh)",
                            zone_id, key[0], char_type, old_value, value,
                        )
                round_failed = stale_found > 0 or values_read == 0
                if round_failed:
                    consecutive_failures += 1
                    if self._failure_warned_level != self._loop_backoff_level:
                        if stale_found:
                            _LOGGER.warning(
                                "HomeKit: %d characteristic(s) returning stale-resource "
                                "errors for home %s, bridge may have reconfigured. "
                                "Reconnecting.",
                                stale_found, mask_home_id(self._home_id),
                            )
                        else:
                            _LOGGER.warning(
                                "HomeKit: the bridge returned no value for any characteristic "
                                "read for home %s, counting it as a failed read",
                                mask_home_id(self._home_id),
                            )
                        self._failure_warned_level = self._loop_backoff_level
                    consecutive_failures = await self._async_reconnect_after_failures(consecutive_failures)
                else:
                    consecutive_failures = 0
                    self._failure_warned_level = None
                    self._loop_backoff_level = 0
                signal = SIGNAL_HOMEKIT_UPDATE.format(home_id=self._home_id)
                for zone_id in updated_zones:
                    self._note_target_ambiguity(zone_id)
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
            except Exception as err:
                # Retry loop: a failed round is counted and the next round retries, so nothing here may end the loop.
                consecutive_failures += 1
                if self._failure_warned_level != self._loop_backoff_level:
                    _LOGGER.warning(
                        "HomeKit: the cache refresh failed for home %s with %s: %s, counting it as a failed read",
                        mask_home_id(self._home_id), type(err).__name__, err,
                    )
                    self._failure_warned_level = self._loop_backoff_level
                else:
                    _LOGGER.debug(
                        "HomeKit: cache refresh failed (%d/%d before reconnect)",
                        consecutive_failures, self._loop_failure_threshold(),
                    )
                consecutive_failures = await self._async_reconnect_after_failures(consecutive_failures)

    def _loop_failure_threshold(self) -> int:
        """Failed rounds that trigger the next loop reconnect: doubles per reconnect, capped near one hour of rounds."""
        most = max(CACHE_REFRESH_FAILURE_THRESHOLD, int(HOMEKIT_LOOP_RECONNECT_MIN_GAP_SECONDS // HOMEKIT_CACHE_REFRESH_SECONDS))
        return min(CACHE_REFRESH_FAILURE_THRESHOLD << min(self._loop_backoff_level, 16), most)

    async def _async_reconnect_after_failures(self, consecutive_failures: int) -> int:
        """Reconnect when the streak reaches the back-off threshold; return the streak to keep."""
        if consecutive_failures < self._loop_failure_threshold():
            return consecutive_failures
        # The first reconnect of a streak keeps the catch-up fetch (this arm is the only outage detector); repeats wait twice as long and skip it.
        catch_up = self._loop_backoff_level == 0
        self._loop_backoff_level += 1
        await self._client.async_reconnect(catch_up_fetch=catch_up)
        return 0

    def unsubscribe_events(self) -> None:
        """Cancel the periodic refresh and disconnect the dispatcher."""
        self._unsubscribe_count += 1
        self._read_error_logged.clear()
        self._read_error_warn_at.clear()
        self._no_mapped_accessory_warned = False
        if self._cache_refresh_task is not None:
            self._cache_refresh_task.cancel()
            self._cache_refresh_task = None
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None
            _LOGGER.debug("HomeKit: unsubscribed from bridge events")
