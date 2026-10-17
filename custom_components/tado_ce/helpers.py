"""Tado CE shared helpers: masking, retry delay, datetime parsing, overlay termination, refresh trigger.

Pure functions used across the integration. The masking
helpers (`mask_serial`, `mask_serial_dict`, `mask_home_id`)
exist to keep PII out of shipped logs, every emit referring
to a device serial / home_id should route through them.

Also hosts `PerEntityAvailabilityMixin`, the one shared entity mixin
that lets a `CoordinatorEntity` subclass surface its own data presence
through `available` instead of only the coordinator's poll status.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import logging
import random
import re
from typing import TYPE_CHECKING, Any, Final

from .const import (
    CLIMATE_ZONE_TYPES,
    CLOUD_READING_REPORT_SLACK,
    CLOUD_READING_STALE_AFTER,
    MAX_RETRY_ATTEMPTS,
    MAX_RETRY_DELAY,
    OVERLAY_MODE_DEFAULT,
    OVERLAY_MODE_MANUAL,
    RETRY_BASE_DELAY,
    TIMER_DURATION_DEFAULT,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from .coordinator import TadoDataUpdateCoordinator
    from .zone_config_manager import ZoneConfigManager

    # PerEntityAvailabilityMixin is only ever mixed into a CoordinatorEntity, whose
    # `available` property it composes with `super()`. Declaring that host shape
    # here (type-check only) lets mypy --strict resolve `super().available`; at
    # runtime the mixin stays a plain object so it adds nothing to the MRO but its
    # own `available`.
    _AvailabilityHost = CoordinatorEntity[Any]
else:
    _AvailabilityHost = object

_LOGGER = logging.getLogger(__name__)

# Serial number masking: keep prefix + first few chars for debugging, mask the rest.
# Bridge serials (IB...) are especially sensitive: combined with the 4-digit auth
# code printed on the bridge, a full serial in shared logs enables brute-force.
_SERIAL_VISIBLE_CHARS: int = 6


def mask_serial(serial: str) -> str:
    """Mask a device or bridge serial for safe logging.

    Keeps the first few characters visible for debugging (e.g. 'VA0315...',
    'IB0123...') and replaces the rest with '…'.
    """
    if not serial or len(serial) <= _SERIAL_VISIBLE_CHARS:
        return serial
    return serial[:_SERIAL_VISIBLE_CHARS] + "…"


def mask_serial_dict(d: dict[str, str]) -> dict[str, str]:
    """Mask all keys (serials) in a serial-to-zone mapping dict."""
    return {mask_serial(k): v for k, v in d.items()}


# Tado home_id is a numeric per-account identifier, not strictly secret
# (Tado's own logs already contain it) but identifies one user's home,
# so we mask it in shipped log output. Multi-home users still get a
# stable, distinguishable shape in their logs.
_HOME_ID_VISIBLE_CHARS: int = 3


def mask_home_id(home_id: str | int | None) -> str:
    """Mask a Tado home ID for safe logging.

    Keeps the first few characters visible so multi-home users can
    still distinguish entries (e.g. '123…' vs '987…') and replaces
    the rest with '…'. Returns '<unknown>' when home_id is missing.
    """
    if home_id is None or home_id == "":
        return "<unknown>"
    s = str(home_id)
    if len(s) <= _HOME_ID_VISIBLE_CHARS:
        return s
    return s[:_HOME_ID_VISIBLE_CHARS] + "…"


_API_URL_HOME_RE = re.compile(r"(/homes/)(\d+)")
_API_URL_DEVICE_RE = re.compile(r"((?<![A-Za-z0-9])devices/)([A-Za-z0-9]+)")


def mask_api_url(url: str) -> str:
    """Mask the home ID and device serial in a Tado API URL for safe logging."""
    url = _API_URL_HOME_RE.sub(lambda m: m.group(1) + mask_home_id(m.group(2)), url)
    return _API_URL_DEVICE_RE.sub(lambda m: m.group(1) + mask_serial(m.group(2)), url)


_BODY_SERIAL_RE = re.compile(r"\b[A-Z]{2}\d{8,12}\b")
_BODY_HOME_ID_RE = re.compile(r"(home_?id[\"']?\s*[:=]\s*[\"']?)(\d+)", re.IGNORECASE)


def mask_response_body(body: str, limit: int = 200) -> str:
    """Mask serials and home IDs in a Tado response body, then truncate to limit."""
    body = mask_api_url(body)
    body = _BODY_SERIAL_RE.sub(lambda m: mask_serial(m.group(0)), body)
    body = _BODY_HOME_ID_RE.sub(lambda m: m.group(1) + mask_home_id(m.group(2)), body)
    return body[:limit]


class PerEntityAvailabilityMixin(_AvailabilityHost):
    """Compose per-entity data presence with coordinator availability.

    ``CoordinatorEntity.available`` reports only ``coordinator.last_update_success``
    and shadows ``Entity._attr_available`` via the MRO, so a subclass that tracks
    its own data presence never surfaces it. Subclasses set ``self._data_present``
    in their update path; this mixin ANDs it with the coordinator's availability so
    a per-entity data gap reads as unavailable while a coordinator failure still
    wins. List this mixin FIRST in the bases so it owns ``available``.
    """

    _data_present: bool = False

    @property
    def available(self) -> bool:
        """Return True only if the coordinator is healthy and this entity has data."""
        return super().available and self._data_present


def live_zones(zones_info: list[Any], coord_data: dict[str, Any] | None) -> list[Any]:
    """Return the zones the live zone states still hold, or all of them when no states are loaded."""
    states = get_zone_states(coord_data)
    if not states:
        return zones_info
    return [z for z in zones_info if str(z.get("id")) in states]


def get_zone_states(coord_data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Extract zone states dict from coordinator data.

    Handles all null-safety: None coord_data, missing "zones" key,
    missing "zoneStates" key.

    Returns:
        Dict mapping zone_id -> zone_state_dict, or empty dict.
    """
    if not coord_data:
        return {}
    zones = coord_data.get("zones")
    if not isinstance(zones, dict):
        return {}
    return zones.get("zoneStates") or {}


def get_zone_state(coord_data: dict[str, Any] | None, zone_id: str) -> dict[str, Any] | None:
    """Get state for a single zone from coordinator data.

    Convenience wrapper: get_zone_states(data).get(zone_id).
    """
    return get_zone_states(coord_data).get(zone_id)


def get_zone_state_for_control(coordinator: TadoDataUpdateCoordinator, zone_id: str) -> dict[str, Any] | None:
    """Zone state a controller may act on: None until a real zone fetch has happened in this process."""
    if coordinator.last_zone_fetch_ts() is None:
        return None
    return get_zone_state(coordinator.data, zone_id)


def zone_confirmed_on(coordinator: TadoDataUpdateCoordinator, zone_id: str) -> bool:
    """Whether the last cloud poll has this zone's power ON.

    Not the entity's own `_attr_hvac_mode`, which flips optimistically
    before a write is confirmed.
    """
    zone_data = get_zone_state(coordinator.data, zone_id) or {}
    setting = zone_data.get("setting") or {}
    return setting.get("power") == "ON"


def resolve_boiler_flow_temperature(
    coordinator: TadoDataUpdateCoordinator,
) -> tuple[float | None, str | None]:
    """Boiler flow temp: cloud data first, external binding as fallback, so the sensor and insight can't disagree.

    Returns (value, source): "zone:<id>", "external", or (None, None).

    The cloud side is picked on presence alone, not age. If Tado kept serving a stale flow value
    while the bridge was offline, it would still beat a fresh external reading. No captured payload
    shows whether `boilerFlowTemperature` carries a timestamp the way `heatingPower` does, so there
    is nothing verified to check against yet.
    """
    zone_states = get_zone_states(coordinator.data)
    for zone_id, zone_data in zone_states.items():
        activity_data = zone_data.get("activityDataPoints") or {}
        flow_temp = (activity_data.get("boilerFlowTemperature") or {}).get("celsius")
        if flow_temp is not None:
            return flow_temp, f"zone:{zone_id}"

    entity_id = coordinator.config_manager.get_boiler_flow_temp_entity()
    if not entity_id:
        return None, None

    state = coordinator.hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None, None

    try:
        return float(state.state), "external"
    except (ValueError, TypeError):
        _LOGGER.debug(
            "Helpers: boiler flow entity %s has non-numeric state %r, ignoring",
            entity_id,
            state.state,
        )
        return None, None


def merge_homekit_into_zone_data(
    zone_data: dict[str, Any],
    zone_id: str,
    coordinator: TadoDataUpdateCoordinator,
) -> dict[str, Any]:
    """Return a shallow copy of zone_data with fresh HomeKit temperature merged in, and HomeKit humidity only when cloud has none."""
    try:
        provider = coordinator.homekit_provider
        reconciler = coordinator.state_reconciler
        if provider is None or reconciler is None:
            return zone_data
        if not provider.is_connected:
            return zone_data
        reconciler.local_provider = provider
        sensor_data = (zone_data.get("sensorDataPoints") or {}).copy()
        cloud_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
        cloud_humidity = (sensor_data.get("humidity") or {}).get("percentage")
        # purpose="control": Smart Valve Control reads this merged
        # insideTemperature as its calibration reference, so a per-zone
        # display-source preference must NOT bend it.
        merged_temp, temp_src = reconciler.merge_zone_temperature(zone_id, cloud_temp, purpose="control")
        merged_hum, hum_src = reconciler.merge_zone_humidity(zone_id, cloud_humidity)
        # Rebuild the nested dicts rather than setdefault(...)[key]=: the shallow
        # .copy() above shares the nested insideTemperature/humidity objects with
        # the coordinator's cloud cache, so an in-place write would overwrite the
        # real cloud reading there (defeating display_temp_source="cloud" and
        # pinning a stale HomeKit value as "cloud"). A fresh nested dict keeps the
        # merge local to this returned copy.
        if merged_temp is not None:
            sensor_data["insideTemperature"] = {
                **(sensor_data.get("insideTemperature") or {}),
                "celsius": merged_temp,
            }
        if merged_hum is not None:
            sensor_data["humidity"] = {
                **(sensor_data.get("humidity") or {}),
                "percentage": merged_hum,
            }
        # Only log when the merge actually changed something,
        # otherwise multi-zone homes would emit a debug line per
        # zone per poll for no signal.
        if cloud_temp != merged_temp or cloud_humidity != merged_hum:
            _LOGGER.debug(
                "Helpers: zone %s merge, temp %s → %s (%s), "
                "humidity %s → %s (%s)",
                zone_id, cloud_temp, merged_temp, temp_src,
                cloud_humidity, merged_hum, hum_src,
            )
        result = dict(zone_data)
        result["sensorDataPoints"] = sensor_data
    except (TypeError, ValueError, AttributeError):
        _LOGGER.debug(
            "Helpers: HomeKit merge failed for zone %s, falling "
            "back to cloud-only zone data for this poll",
            zone_id, exc_info=True,
        )
        return zone_data
    else:
        return result


# Backwards-compat alias for the `next_time_block` SERVICE argument only.
# NEXT_TIME_BLOCK is not a valid overlay mode, the /api/v2 overlay endpoint
# rejects a literal NEXT_TIME_BLOCK with HTTP 422. A user passing
# overlay="next_time_block" to a service call still means "end at the next
# automatic change", which is TADO_MODE, so we keep this one mapping to honour
# that intent.
_OVERLAY_API_MAP: dict[str, str] = {
    "NEXT_TIME_BLOCK": "TADO_MODE",
}


def _map_overlay_to_api(mode: str) -> str:
    """Map a legacy service-arg overlay value to its Tado API-accepted value."""
    return _OVERLAY_API_MAP.get(mode, mode)


def should_use_homekit_for_overlay(hass: HomeAssistant, zone_id: str, entry_id: str | None = None) -> bool:
    """Return True when a HomeKit local write is safe for this zone's overlay mode.

    HomeKit writes carry no termination, the bridge sets only the TRV's target
    state and Tado stamps a MANUAL overlay on any local-origin write. So HomeKit
    is only correct when the chosen overlay mode IS MANUAL. TADO_MODE ("until
    next automatic change") and TIMER need the explicit termination sent through
    the cloud API.

    Resolves the per-zone overlay mode first, then the global mode. Returns
    True only when the effective mode is MANUAL; False otherwise. With no
    entry_id (or on a lookup error) falls back to the integration default.
    """
    if not entry_id:
        return OVERLAY_MODE_DEFAULT == OVERLAY_MODE_MANUAL

    try:
        coordinator = _get_coordinator(hass, entry_id)
        if coordinator is None:
            return OVERLAY_MODE_DEFAULT == OVERLAY_MODE_MANUAL

        mode = None
        if coordinator.zone_config_manager:
            mode = coordinator.zone_config_manager.get_zone_value(zone_id, "overlay_mode", None)
        if not mode:
            mode = coordinator.overlay_mode or OVERLAY_MODE_DEFAULT

        if mode != OVERLAY_MODE_MANUAL:
            _LOGGER.debug(
                "Helpers: zone %s overlay mode is %s (not MANUAL), routing this "
                "write through the cloud API so the termination is honoured",
                zone_id, mode,
            )
            return False
    except (AttributeError, TypeError):
        return OVERLAY_MODE_DEFAULT == OVERLAY_MODE_MANUAL

    return True


def svc_schedule_zone_ids(
    zone_config_manager: ZoneConfigManager,
    hass: HomeAssistant,
) -> frozenset[str]:
    """Return zone_ids eligible for the SVC-driven schedule fetch.

    Eligible: svc_mode == "valve_target" and external_temp_sensor resolves
    to a real entity (a stale entity_id fails closed, same as
    read_external_sensor). Built from config, not from
    coordinator.valve_controllers - that registry only populates after
    the first full sync, so a registry-based gate would never fetch on
    the sync this fix exists to unblock.
    """
    eligible: set[str] = set()
    for zone_id in zone_config_manager.zones:
        config = zone_config_manager.get_zone_config(zone_id)
        if config.get("svc_mode") != "valve_target":
            continue
        entity_id = config.get("external_temp_sensor", "")
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state is None:
            continue
        eligible.add(zone_id)
    return frozenset(eligible)


def retry_delay(attempt: int, base_delay: float = RETRY_BASE_DELAY) -> float:
    """Calculate jittered retry delay for the given attempt number.

    Uses full jitter pattern: uniform random between 0 and base_delay^attempt.
    """
    return random.uniform(0, min(MAX_RETRY_DELAY, base_delay**attempt))


async def async_retry_with_backoff[T](
    callback: Callable[..., Awaitable[T]],
    *,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
    no_retry_exceptions: tuple[type[Exception], ...] = (),
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    on_retry: Callable[[int, Exception], Awaitable[None]] | None = None,
) -> T:
    """Execute callback with exponential backoff retry.

    Args:
        callback: Async callable to execute.
        max_attempts: Maximum number of attempts.
        no_retry_exceptions: Exception types that should never be retried.
        retryable_exceptions: Exception types eligible for retry.
        on_retry: Optional async callback(attempt, error) called before each retry sleep.

    Returns:
        Result of successful callback execution.

    Raises:
        The last exception if all attempts exhausted, or any no_retry_exception immediately.
    """
    if max_attempts < 1:
        msg = f"max_attempts must be >= 1, got {max_attempts}"
        raise ValueError(msg)
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await callback()
        except no_retry_exceptions:
            raise
        except retryable_exceptions as err:
            last_error = err
            if attempt >= max_attempts:
                raise
            if on_retry:
                await on_retry(attempt, err)
            delay = retry_delay(attempt)
            await asyncio.sleep(delay)
    msg = "async_retry_with_backoff: loop exited without returning or raising"
    raise RuntimeError(msg) from last_error


def _get_coordinator(hass: HomeAssistant, entry_id: str) -> TadoDataUpdateCoordinator:
    """Get coordinator from entry_id, or None."""
    try:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and hasattr(entry, "runtime_data") and entry.runtime_data is not None:
            return entry.runtime_data  # type: ignore[no-any-return]
    except (AttributeError, TypeError):
        _LOGGER.debug("helpers: entry %s has no usable runtime_data yet", entry_id)
    return None  # type: ignore[return-value]


def parse_iso_datetime(iso_str: str) -> datetime:
    """Parse an ISO 8601 datetime string to a timezone-aware UTC datetime.

    Naive datetimes (no tzinfo) are assumed UTC.

    Raises:
        ValueError: If the string cannot be parsed as ISO 8601.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def predicted_next_reset(last_reset_utc: str | None) -> datetime | None:
    """Return the moment 24 h after `last_reset_utc`, or None when it is absent or unparseable."""
    if not last_reset_utc:
        return None
    try:
        return parse_iso_datetime(last_reset_utc) + timedelta(hours=24)
    except (ValueError, TypeError, OverflowError):
        # Malformed stored values must never raise in the poll path.
        _LOGGER.debug("Helpers: could not read last_reset_utc %r", last_reset_utc)
        return None


def connection_value_to_bool(conn: dict[str, Any] | None) -> bool | None:
    """Parse a Tado `connectionState` dict to True (online), False (offline) or None (unknown)."""
    if not isinstance(conn, dict):
        return None
    raw = conn.get("value")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().upper()
        if text in ("TRUE", "FALSE"):
            return text == "TRUE"
    return None


def offline_since_date(conn: dict[str, Any] | None) -> str | None:
    """Return the local date an offline device was last seen, else None."""
    ts = conn.get("timestamp") if isinstance(conn, dict) else None
    if connection_value_to_bool(conn) is False and isinstance(ts, str) and ts:
        from homeassistant.util import dt as dt_util

        try:
            return dt_util.as_local(parse_iso_datetime(ts)).date().isoformat()
        except (ValueError, OverflowError):
            return None
    return None


# (group, key) -> zone role of the device that supplies that cloud reading; add a row to cover another reading.
CLOUD_READING_ROLES: Final[dict[tuple[str, str], str]] = {
    ("sensorDataPoints", "insideTemperature"): "ZONE_LEADER",
    ("sensorDataPoints", "humidity"): "ZONE_LEADER",
}
_GROUP_BY_KEY: Final[dict[str, str]] = {key: group for (group, key) in CLOUD_READING_ROLES}


def is_cloud_reading_untrusted(reading: Any, role_devices: list[Any], now: datetime) -> bool:
    """True when a supplying device has been offline for CLOUD_READING_STALE_AFTER and the reading is no newer than its last-seen time plus CLOUD_READING_REPORT_SLACK."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        reading_ts = parse_iso_datetime(reading["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False  # no usable timestamp, fail open
    for device in role_devices:
        conn = device.get("connectionState") if isinstance(device, dict) else None
        if connection_value_to_bool(conn) is not False:
            continue
        try:
            last_seen = parse_iso_datetime(conn["timestamp"])  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            continue
        if now - last_seen >= CLOUD_READING_STALE_AFTER and reading_ts <= last_seen + CLOUD_READING_REPORT_SLACK:
            return True
    return False


def _find_untrusted_readings(
    zone_states: dict[str, Any], zones_info: list[Any], now: datetime,
) -> frozenset[tuple[str, str]]:
    """Return the (zone_id, key) pairs whose reading should be withheld."""
    info_by_id = {str(z.get("id")): z for z in zones_info if isinstance(z, dict)}
    withheld: set[tuple[str, str]] = set()
    for zone_id, zone in zone_states.items():
        info = info_by_id.get(str(zone_id))
        if info is None or not isinstance(zone, dict):
            continue
        zone_type = info.get("type")
        if not isinstance(zone_type, str) or zone_type not in CLIMATE_ZONE_TYPES:
            continue
        devices = info.get("devices")
        for (group, key), role in CLOUD_READING_ROLES.items():
            points = zone.get(group)
            reading = points.get(key) if isinstance(points, dict) else None
            role_devices = [
                d for d in (devices if isinstance(devices, list) else [])
                if isinstance(d, dict) and isinstance(d.get("duties"), list) and role in d["duties"]
            ]
            if isinstance(reading, dict) and is_cloud_reading_untrusted(reading, role_devices, now):
                withheld.add((str(zone_id), key))
    return frozenset(withheld)


def _drop_readings(zone_data: dict[str, Any], withheld: frozenset[tuple[str, str]]) -> dict[str, Any]:
    """Return a copy of zone_data without the withheld readings, rebuilding every level touched."""
    zone_states = zone_data.get("zoneStates")
    if not withheld or not isinstance(zone_states, dict):
        return zone_data
    new_states = dict(zone_states)
    original_keys = {str(k): k for k in zone_states}
    changed = False
    for zone_id, key in withheld:
        state_key = original_keys.get(zone_id)
        zone = new_states.get(state_key)
        group = _GROUP_BY_KEY.get(key)
        if not isinstance(zone, dict) or group is None:
            continue
        points = zone.get(group)
        if not isinstance(points, dict) or key not in points:
            continue
        new_zone = dict(zone)
        new_zone[group] = {k: v for k, v in points.items() if k != key}
        new_states[state_key] = new_zone
        changed = True
    if not changed:
        return zone_data
    new_data = dict(zone_data)
    new_data["zoneStates"] = new_states
    return new_data


def apply_cloud_reading_trust(
    zone_data: Any,
    zones_info: Any,
    now: datetime,
    previous: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[Any, frozenset[tuple[str, str]]]:
    """Return zone_data without readings from a device offline past CLOUD_READING_STALE_AFTER, plus the withheld set; never raises or mutates input."""
    # An empty or malformed response keeps the last verdict instead of resetting it.
    if not isinstance(zone_data, dict):
        return zone_data, previous
    zone_states = zone_data.get("zoneStates")
    if not isinstance(zone_states, dict) or not zone_states:
        return zone_data, previous
    # zones_info is None only before any zone list was stored; keep the last verdict then.
    withheld = _find_untrusted_readings(zone_states, zones_info, now) if isinstance(zones_info, list) else previous
    return _drop_readings(zone_data, withheld), withheld


def is_reading_withheld(coord_data: dict[str, Any] | None, zone_id: str, key: str) -> bool:
    """Whether the coordinator withheld this zone's reading on the latest poll."""
    withheld = (coord_data or {}).get("withheld_readings") or frozenset()
    return (str(zone_id), key) in withheld


async def async_trigger_immediate_refresh(
    hass: HomeAssistant,
    entity_id: str,
    reason: str,
    force: bool = False,
    skip_debounce: bool = False,
) -> None:
    """Ask the entity's coordinator for an immediate (debounced) refresh.

    `force=True` bypasses the cooldown so button-driven writes
    show up straight away; `skip_debounce=True` removes the
    debounce delay entirely (also button-style flow).
    """
    try:
        from homeassistant.helpers import entity_registry as er

        entity_registry = er.async_get(hass)
        entity_entry = entity_registry.async_get(entity_id)
        if entity_entry and entity_entry.config_entry_id:
            coordinator = _get_coordinator(hass, entity_entry.config_entry_id)
            if coordinator and coordinator.refresh_handler:
                await coordinator.refresh_handler.trigger_refresh(
                    entity_id,
                    reason,
                    force=force,
                    skip_debounce=skip_debounce,
                )
                return
        _LOGGER.warning(
            "Helpers: no refresh handler found for %s, refresh "
            "request silently dropped, will retry on the next "
            "coordinator poll",
            entity_id,
        )
    except (KeyError, TypeError, ValueError) as e:
        _LOGGER.warning(
            "Helpers: immediate refresh trigger failed (%s), "
            "refresh request silently dropped, will retry on the "
            "next coordinator poll",
            e,
        )


def get_optimistic_window(hass: HomeAssistant, entry_id: str | None = None) -> float:
    """Compute how long entities should hold optimistic state after a write.

    Window = `refresh_debounce_seconds` + a small buffer. Falls
    back to the integration default when the config can't be
    read (e.g. coordinator not yet wired).
    """
    from .const import DEFAULT_OPTIMISTIC_WINDOW_SECONDS, OPTIMISTIC_WINDOW_BUFFER_SECONDS

    try:
        if entry_id:
            coordinator = _get_coordinator(hass, entry_id)
            if coordinator and coordinator.config_manager:
                return float(coordinator.config_manager.get_refresh_debounce_seconds()) + OPTIMISTIC_WINDOW_BUFFER_SECONDS
    except (AttributeError, TypeError, ValueError) as err:
        _LOGGER.debug(
            "Helpers: optimistic window read from config failed "
            "(%s), falling back to default %.1fs",
            err, DEFAULT_OPTIMISTIC_WINDOW_SECONDS,
        )
    return DEFAULT_OPTIMISTIC_WINDOW_SECONDS


def get_overlay_termination(hass: HomeAssistant, entry_id: str | None = None) -> dict[str, Any]:
    """Get the termination dict for overlay API calls.

    Args:
        hass: Home Assistant instance
        entry_id: Optional config entry ID for per-entry lookup

    Returns:
        {"type": "TADO_MODE"} or {"type": "MANUAL"} or {"type": "TIMER", "durationInSeconds": ...}
        The /api/v2 overlay endpoint accepts MANUAL, TADO_MODE, TIMER.

    """
    mode = OVERLAY_MODE_DEFAULT
    duration = TIMER_DURATION_DEFAULT
    if entry_id:
        try:
            coordinator = _get_coordinator(hass, entry_id)
            if coordinator:
                mode = coordinator.overlay_mode or OVERLAY_MODE_DEFAULT
                duration = coordinator.timer_duration or TIMER_DURATION_DEFAULT
        except (AttributeError, TypeError):
            _LOGGER.debug("helpers: entry %s coordinator missing overlay/timer attrs, using defaults", entry_id)

    # Map internal storage values to API-accepted values
    mode = _map_overlay_to_api(mode)

    if mode == "TIMER":
        return {"type": "TIMER", "durationInSeconds": duration * 60}

    return {"type": mode}


def get_zone_overlay_termination(hass: HomeAssistant, zone_id: str, entry_id: str | None = None) -> dict[str, Any]:
    """Get the termination dict for overlay API calls with per-zone support.

    Uses the per-zone overlay_mode (defaults merged in, so it's always present)
    unless it's TADO_MODE, in which case it falls through to the global
    overlay_mode instead.

    Args:
        hass: Home Assistant instance
        zone_id: Zone ID to get overlay mode for
        entry_id: Optional config entry ID for per-entry lookup

    Returns:
        {"type": "..."} or {"type": "...", "durationInSeconds": ...} for Timer mode

    """
    zone_config_manager = None
    if entry_id:
        try:
            coordinator = _get_coordinator(hass, entry_id)
            if coordinator:
                zone_config_manager = coordinator.zone_config_manager
        except (AttributeError, TypeError):
            _LOGGER.debug("helpers: entry %s coordinator has no zone_config_manager yet", entry_id)

    if zone_config_manager:
        # Get per-zone overlay mode (UPPERCASE values)
        zone_mode = zone_config_manager.get_zone_value(zone_id, "overlay_mode", None)

        if zone_mode and zone_mode != "TADO_MODE":
            # Map to API values
            api_mode = _map_overlay_to_api(zone_mode)

            # Handle Timer mode with duration
            if api_mode == "TIMER":
                duration = zone_config_manager.get_zone_value(zone_id, "timer_duration", TIMER_DURATION_DEFAULT)
                return {"type": "TIMER", "durationInSeconds": duration * 60}

            return {"type": api_mode}

    # Fallback to global overlay mode (handles TADO_MODE and when no per-zone config)
    return get_overlay_termination(hass, entry_id=entry_id)


def build_timer_termination(
    duration_minutes: int | None = None,
    overlay: str | None = None,
    hass: HomeAssistant | None = None,
    zone_id: str | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Build termination dict for set_timer / set_overlay calls.

    Priority:
    1. duration_minutes → TIMER
    2. overlay='next_time_block' → TADO_MODE
    3. overlay='manual' → MANUAL
    4. fallback → per-zone overlay termination
    """
    if duration_minutes:
        from .const import TIMER_DURATION_MAX, TIMER_DURATION_MIN

        clamped = max(TIMER_DURATION_MIN, min(int(duration_minutes), TIMER_DURATION_MAX))
        return {"type": "TIMER", "durationInSeconds": clamped * 60}

    if overlay:
        overlay_upper = overlay.upper()
        api_mode = _map_overlay_to_api(overlay_upper)
        if api_mode in ("TADO_MODE", "MANUAL"):
            return {"type": api_mode}

    # Fall back to per-zone / global overlay config
    if hass and zone_id:
        return get_zone_overlay_termination(hass, zone_id, entry_id=entry_id)
    if hass:
        return get_overlay_termination(hass, entry_id=entry_id)

    # Ultimate fallback
    return {"type": "MANUAL"}


def prune_zone_keyed_dict(
    d: dict[str, Any],
    current_zones: frozenset[str],
) -> int:
    """Drop entries whose zone_id is no longer in current_zones; return removed count.

    Recognised key shapes:
    - "zone_id":           DataLoader dict stores
    - "zone_id:entity":    state_restore key shape (split on first colon)
    """
    to_drop = {k for k in d if k.split(":", 1)[0] not in current_zones}
    for k in to_drop:
        d.pop(k, None)
    return len(to_drop)


def retry_after_to_minutes(seconds: int) -> str:
    """Format retry-after seconds as minute count for user-facing messages."""
    # WHY: floor at 1, a 30-second cooldown still reads "Try again in 1 min."
    return str(max(1, seconds // 60))


def low_quota_threshold(daily_limit: int | None) -> int:
    """Return the low-quota threshold scaled to the daily-limit tier."""
    # WHY: max(absolute floor, percent of daily limit). Floor keeps the
    # 100-call free tier on existing behaviour; percent scales the gate so
    # 1,000-call transitional and 20,000-call legacy tiers fire with
    # proportional cushion (10%) rather than the same absolute cushion as
    # the free tier (which would mean fires at 99% used on the legacy tier).
    from .const import LOW_QUOTA_RESERVE_FLOOR, LOW_QUOTA_RESERVE_PERCENT

    if daily_limit is None or daily_limit <= 0:
        return LOW_QUOTA_RESERVE_FLOOR
    return max(LOW_QUOTA_RESERVE_FLOOR, int(daily_limit * LOW_QUOTA_RESERVE_PERCENT))


def full_sync_period_seconds(daily_limit: int | None) -> int:
    """Return seconds between full syncs: the slower cadence on the free tier, hourly otherwise."""
    from .const import (
        ZONES_INFO_FREE_TIER_THRESHOLD,
        ZONES_INFO_REFRESH_SECONDS_FREE,
        ZONES_INFO_REFRESH_SECONDS_PAID,
    )

    if isinstance(daily_limit, int) and 0 < daily_limit <= ZONES_INFO_FREE_TIER_THRESHOLD:
        return ZONES_INFO_REFRESH_SECONDS_FREE
    return ZONES_INFO_REFRESH_SECONDS_PAID


def hard_quota_reserve(daily_limit: int | None) -> int:
    """Return the reserve at which polling pauses, scaled to the daily-limit tier."""
    # WHY: a hard floor kept for manual actions, unlike low_quota_threshold's
    # "start conserving" signal. Nothing optional may spend into it.
    from .const import QUOTA_RESERVE_CALLS, QUOTA_RESERVE_PERCENT

    if daily_limit is None or daily_limit <= 0:
        return QUOTA_RESERVE_CALLS
    return max(QUOTA_RESERVE_CALLS, int(daily_limit * QUOTA_RESERVE_PERCENT))
