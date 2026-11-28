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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import math
import random
import re
import time
from typing import TYPE_CHECKING, Any, Final, TypeGuard

from homeassistant.const import UnitOfTemperature
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import TemperatureConverter

from .cloud_health import CloudHealthState
from .const import (
    CLIMATE_ZONE_TYPES,
    CLOUD_READING_REPORT_SLACK,
    CLOUD_READING_STALE_AFTER,
    DOMAIN,
    EXTERNAL_TEMPERATURE_MAX_C,
    EXTERNAL_TEMPERATURE_MIN_C,
    HOMEKIT_CLOUD_LAG_SECONDS,
    MAX_RETRY_ATTEMPTS,
    MAX_RETRY_DELAY,
    OVERLAY_MODE_DEFAULT,
    OVERLAY_MODE_MANUAL,
    RETRY_BASE_DELAY,
    TIMER_DURATION_DEFAULT,
    TIMER_DURATION_MAX,
    TIMER_DURATION_MIN,
    WRITE_ROUTE_CLOUD,
)
from .entity_registry import get_meta
from .mechanism_counters import put_agrees

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

_INJECTED_PRESENCE_MAX_AHEAD_SECONDS: Final = 3600

REASON_NOT_CONNECTED: Final = "HomeKit is not connected"
REASON_NO_CHARACTERISTIC: Final = "the zone has no HomeKit target characteristic"
REASON_CIRCUIT_OPEN: Final = "the HomeKit write circuit is open"
REASON_HELD: Final = "the cloud is not reachable, so the write is held locally"
REASON_ZONE_OFF: Final = "the zone is off"
REASON_CLOUD_ROUTE: Final = "the zone sends changes via the cloud"
REASON_NOT_MANUAL: Final = "the overlay mode is not MANUAL"

# Every CloudHealth state in which the cloud refuses or cannot take a write.
_CLOUD_DOWN_STATES: Final = frozenset(state for state in CloudHealthState if state is not CloudHealthState.OK)

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


_UNIQUE_ID_HOME_RE = re.compile(r"(tado_ce_)(\d+)")


def mask_unique_id(unique_id: str | None) -> str:
    """Mask the home ID that follows tado_ce_ in a unique ID for safe logging."""
    if not unique_id:
        return ""
    return _UNIQUE_ID_HOME_RE.sub(lambda m: m.group(1) + mask_home_id(m.group(2)), unique_id)


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


def _is_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _injected_home_state_superseded(home_state: dict[str, Any], zone_fetch_started: datetime | None) -> bool:
    """True once an injected answer's window is over and a zone fetch newer than the injection has landed."""
    now = time.time()
    until = home_state["injected_until"]
    if _is_number(until) and now <= until <= now + _INJECTED_PRESENCE_MAX_AHEAD_SECONDS:
        return False
    if not isinstance(zone_fetch_started, datetime):
        return False
    injected_at = home_state.get("injected_at")
    return zone_fetch_started.timestamp() > (injected_at if _is_number(injected_at) else 0)


def resolve_home_presence(
    coord_data: dict[str, Any] | None,
    zone_fetch_started: datetime | None = None,
) -> tuple[str, bool, str] | None:
    """Return (presence, locked, source); home_state exists only with Home State Sync on, the zone fetch always carries tadoMode."""
    home_state = (coord_data or {}).get("home_state")
    if home_state and "presence" in home_state and not (
        "injected_until" in home_state and _injected_home_state_superseded(home_state, zone_fetch_started)
    ):
        return home_state["presence"], home_state.get("presenceLocked", False), "home_state"
    for zone in get_zone_states(coord_data).values():
        if isinstance(zone, dict) and zone.get("tadoMode"):
            return zone["tadoMode"], zone.get("geolocationOverride", False), "zones"
    if home_state:
        return home_state.get("presence", "HOME"), home_state.get("presenceLocked", False), "home_state"
    return None


def get_entity_zone_ids(coord_data: dict[str, Any] | None) -> frozenset[str]:
    """Zone ids that both zones_info and zoneStates list, the zones every per-zone platform can build for."""
    zones_info = (coord_data or {}).get("zones_info")
    listed = (
        {str(z["id"]) for z in zones_info if isinstance(z, dict) and z.get("id") is not None}
        if isinstance(zones_info, list)
        else set()
    )
    return frozenset(listed & set(get_zone_states(coord_data)))


def zone_build_identity(zone: dict[str, Any]) -> tuple[str, str]:
    """Type and name a zone's entities are built with, read the way the platforms read them."""
    return (zone.get("type") or "", zone.get("name", f"Zone {zone.get('id')}"))


def get_entity_zone_identity(coord_data: dict[str, Any] | None) -> dict[str, tuple[str, str]]:
    """Map each buildable zone id to the identity its entities are made with."""
    listed = get_entity_zone_ids(coord_data)
    return {
        str(z["id"]): zone_build_identity(z)
        for z in (coord_data or {}).get("zones_info") or []
        if isinstance(z, dict) and z.get("id") is not None and str(z["id"]) in listed
    }


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


def payload_to_time(coordinator: TadoDataUpdateCoordinator, zone_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the put payload, or None when the cloud state already shows it (it would agree at once)."""
    cloud_state = get_zone_state(coordinator.data, zone_id)
    return None if isinstance(cloud_state, dict) and put_agrees(payload, cloud_state) else payload


_TEMPERATURE_UNIT_ALIASES: Final[dict[str, str]] = {
    "c": UnitOfTemperature.CELSIUS, "°c": UnitOfTemperature.CELSIUS, "degc": UnitOfTemperature.CELSIUS,
    "celsius": UnitOfTemperature.CELSIUS,
    "f": UnitOfTemperature.FAHRENHEIT, "°f": UnitOfTemperature.FAHRENHEIT, "degf": UnitOfTemperature.FAHRENHEIT,
    "fahrenheit": UnitOfTemperature.FAHRENHEIT,
    "k": UnitOfTemperature.KELVIN,
    # U+2103 / U+2109 and the ordinal-indicator forms a sensor may publish
    "℃": UnitOfTemperature.CELSIUS, "ºc": UnitOfTemperature.CELSIUS,
    "℉": UnitOfTemperature.FAHRENHEIT, "ºf": UnitOfTemperature.FAHRENHEIT,
}
# A sensor can read slightly above 100 (rounding or supersaturation), so up to this is clamped to 100.
_HUMIDITY_OVERSHOOT_MAX: Final = 105.0


def parse_temperature_celsius(raw: Any, unit: Any) -> float | None:
    """Return an external sensor reading in Celsius, or None when it is non-numeric, in an unknown unit or implausible."""
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None
    # A missing or non-string unit means Celsius, so sensors that publish no unit keep working.
    if isinstance(unit, str) and unit.strip():
        canonical = _TEMPERATURE_UNIT_ALIASES.get(unit.strip().lower())
        if canonical is None:
            _LOGGER.debug("Helpers: external temperature unit %r is not supported, ignoring", unit)
            return None
        if canonical != UnitOfTemperature.CELSIUS:
            value = TemperatureConverter.convert(value, canonical, UnitOfTemperature.CELSIUS)
    if not EXTERNAL_TEMPERATURE_MIN_C <= value <= EXTERNAL_TEMPERATURE_MAX_C:
        return None
    return value


def parse_percentage(raw: Any) -> float | None:
    """Return an external humidity reading from 0 to 100 (a reading of up to 105 is clamped to 100), else None."""
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None
    return min(value, 100.0) if 0.0 <= value <= _HUMIDITY_OVERSHOOT_MAX else None


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

    value = parse_temperature_celsius(state.state, state.attributes.get("unit_of_measurement"))
    if value is None:
        _LOGGER.debug("Helpers: boiler flow entity %s has unusable state %r, ignoring", entity_id, state.state)
        return None, None
    return value, "external"


_merge_logged: dict[tuple[str, str], tuple[Any, ...]] = {}


def merge_homekit_into_zone_data(
    zone_data: dict[str, Any],
    zone_id: str,
    coordinator: TadoDataUpdateCoordinator,
    purpose: str = "control",
) -> dict[str, Any]:
    """Return a shallow copy of zone_data with fresh HomeKit temperature merged in (purpose="display" honours the zone's Temperature source), and HomeKit humidity only when cloud has none."""
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
        # Default purpose="control": Smart Valve Control reads this merged
        # insideTemperature as its calibration reference, so a per-zone
        # display-source preference must NOT bend it.
        merge_kwargs: dict[str, Any] = {"purpose": purpose}
        if purpose == "display":
            zcm = coordinator.zone_config_manager
            merge_kwargs["display_source"] = (
                zcm.get_zone_value(zone_id, "display_temp_source", "auto") if zcm else "auto"
            )
        merged_temp, temp_src = reconciler.merge_zone_temperature(zone_id, cloud_temp, **merge_kwargs)
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
        # Only log when the merge changed something and differs from the last line
        # logged for this zone and purpose; with no cloud data every entity update
        # would otherwise repeat the same line.
        merge_state = (cloud_temp, merged_temp, temp_src, cloud_humidity, merged_hum, hum_src)
        if (cloud_temp != merged_temp or cloud_humidity != merged_hum) and _merge_logged.get((zone_id, purpose)) != merge_state:
            _merge_logged[(zone_id, purpose)] = merge_state
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


class Route(StrEnum):
    """Where one zone write goes."""

    CLOUD = "cloud"
    HOMEKIT = "homekit"
    # Only the heating write entry points allow it, and only while CloudHealth is not OK.
    HOMEKIT_HELD = "homekit_held"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The route one zone write takes and, when it is not HomeKit, the first reason."""

    route: Route
    reasons: tuple[str, ...] = ()


def _cloud(reason: str) -> RouteDecision:
    return RouteDecision(Route.CLOUD, (reason,))


def _overlay_refusal_reason(hass: HomeAssistant, zone_id: str, entry_id: str | None) -> str:
    """Name which of the two overlay gates refused (called only after `should_use_homekit_for_overlay` said no)."""
    try:
        coordinator = _get_coordinator(hass, entry_id) if entry_id else None
        zone_config_manager = getattr(coordinator, "zone_config_manager", None)
        if zone_config_manager and zone_config_manager.get_write_route(zone_id) == WRITE_ROUTE_CLOUD:
            return REASON_CLOUD_ROUTE
    except (AttributeError, TypeError):
        # The same lookup errors the predicate degrades on; the mode reason is the fallback.
        _LOGGER.debug("Helpers: zone %s write route unreadable while naming a route reason", zone_id)
    return REASON_NOT_MANUAL


def decide_route(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    *,
    entry_id: str | None = None,
    zone_was_off: bool = False,
    honour_route: bool = True,
    allow_held: bool = False,
) -> RouteDecision:
    """Decide where this zone's target write goes, in one written order (shared by heating, AC and the valve)."""
    provider = coordinator.homekit_provider
    if provider is None or not provider.is_connected:
        return _cloud(REASON_NOT_CONNECTED)
    # A zone with no accessory or characteristic can never recover, so it must not feed its own tracker.
    if not provider.has_target_characteristic(zone_id):
        return _cloud(REASON_NO_CHARACTERISTIC)
    tracker = coordinator.write_health_for(zone_id)
    if tracker is None or not tracker.should_try_homekit():
        return _cloud(REASON_CIRCUIT_OPEN)
    if allow_held and coordinator.cloud_health.state in _CLOUD_DOWN_STATES:
        return RouteDecision(Route.HOMEKIT_HELD, (REASON_HELD,))
    if zone_was_off:
        return _cloud(REASON_ZONE_OFF)
    if honour_route and not should_use_homekit_for_overlay(hass, zone_id, entry_id):
        return _cloud(_overlay_refusal_reason(hass, zone_id, entry_id))
    return RouteDecision(Route.HOMEKIT)


def can_write_target(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    *,
    entry_id: str | None = None,
    honour_route: bool = True,
) -> bool:
    """Return True when this zone's target write may be tried over HomeKit (the valve's wrapper over `decide_route`)."""
    decision = decide_route(hass, coordinator, zone_id, entry_id=entry_id, honour_route=honour_route)
    return decision.route is not Route.CLOUD


def should_use_homekit_for_overlay(hass: HomeAssistant, zone_id: str, entry_id: str | None = None) -> bool:
    """Return True when a HomeKit local write is safe for this zone's overlay mode.

    HomeKit writes carry no termination, the bridge sets only the TRV's target
    state and Tado stamps a MANUAL overlay on any local-origin write. So HomeKit
    is only correct when the chosen overlay mode IS MANUAL. TADO_MODE ("until
    next automatic change") and TIMER need the explicit termination sent through
    the cloud API.

    A zone set to send changes via the cloud never uses HomeKit. Otherwise resolves
    the zone's own overlay mode, else the hub's, and returns True only for MANUAL.
    With no entry_id (or on a lookup error) falls back to the integration default.
    """
    if not entry_id:
        return OVERLAY_MODE_DEFAULT == OVERLAY_MODE_MANUAL

    try:
        coordinator = _get_coordinator(hass, entry_id)
        if coordinator is None:
            return OVERLAY_MODE_DEFAULT == OVERLAY_MODE_MANUAL

        zone_config_manager = coordinator.zone_config_manager
        if zone_config_manager:
            if zone_config_manager.get_write_route(zone_id) == WRITE_ROUTE_CLOUD:
                _LOGGER.debug(
                    "Helpers: routing this write through the cloud API because zone %s "
                    "is set to send changes via the cloud",
                    zone_id,
                )
                return False
            mode = zone_config_manager.get_effective_overlay_mode(zone_id, coordinator.overlay_mode)
        else:
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


def zone_entity_id(
    hass: HomeAssistant,
    domain: str,
    meta_key: str,
    home_id: str | None,
    zone_id: str,
    fallback_object_id: str,
) -> str:
    """Return a zone entity's registry id by unique id, else the guessed `<domain>.<fallback_object_id>`."""
    if home_id:
        suffix = get_meta(meta_key).unique_id_suffix.format(zone_id=zone_id)
        entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"tado_ce_{home_id}_{suffix}")
        if entity_id:
            return entity_id
    _LOGGER.debug("Helpers: no registry entry for zone %s %s, falling back to %s.%s", zone_id, meta_key, domain, fallback_object_id)
    return f"{domain}.{fallback_object_id}"


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

    `force=True` bypasses the entity-domain filter (the quota check still
    runs) so button-driven writes are refreshed; `skip_debounce=True`
    removes the debounce delay entirely (also button-style flow).
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


def get_homekit_cloud_lag_seconds(hass: HomeAssistant, entry_id: str | None = None) -> float:
    """Return the cloud lag, never below the optimistic hold and never above the write protection window."""
    # Imported here to keep helpers free of a module-level state_reconciler dependency
    from .const import OPTIMISTIC_WINDOW_BUFFER_SECONDS
    from .state_reconciler import WRITE_PROTECTION_WINDOW

    floor = get_optimistic_window(hass, entry_id) + OPTIMISTIC_WINDOW_BUFFER_SECONDS
    return min(max(HOMEKIT_CLOUD_LAG_SECONDS, floor), WRITE_PROTECTION_WINDOW.total_seconds())


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
                duration = normalise_timer_minutes(coordinator.timer_duration)
        except (AttributeError, TypeError):
            _LOGGER.debug("helpers: entry %s coordinator missing overlay/timer attrs, using defaults", entry_id)

    # Map internal storage values to API-accepted values
    mode = _map_overlay_to_api(mode)

    if mode == "TIMER":
        return {"type": "TIMER", "durationInSeconds": duration * 60}

    return {"type": mode}


def get_zone_overlay_termination(hass: HomeAssistant, zone_id: str, entry_id: str | None = None) -> dict[str, Any]:
    """Get the termination dict for overlay API calls with per-zone support.

    Uses the zone's own overlay mode and timer when set, otherwise the hub's.

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
        mode = _map_overlay_to_api(
            zone_config_manager.get_effective_overlay_mode(zone_id, coordinator.overlay_mode),
        )
        if mode == "TIMER":
            minutes = zone_config_manager.get_effective_timer_duration(zone_id, coordinator.timer_duration)
            return {"type": "TIMER", "durationInSeconds": minutes * 60}
        return {"type": mode}

    return get_overlay_termination(hass, entry_id=entry_id)


_timer_normalised_logged: set[int] = set()


def normalise_timer_minutes(value: Any) -> int:
    """Bring a stored timer duration into the allowed range, logging each distinct correction once."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return TIMER_DURATION_DEFAULT
    if minutes < TIMER_DURATION_MIN:
        return TIMER_DURATION_DEFAULT
    if minutes > TIMER_DURATION_MAX:
        if minutes not in _timer_normalised_logged:
            _timer_normalised_logged.add(minutes)
            _LOGGER.info(
                "Timer: saved duration %d minutes is above the %d minute limit, using %d",
                minutes, TIMER_DURATION_MAX, TIMER_DURATION_MAX,
            )
        return TIMER_DURATION_MAX
    return minutes


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
        minutes = int(duration_minutes)
        if not TIMER_DURATION_MIN <= minutes <= TIMER_DURATION_MAX:
            msg = f"Timer duration must be {TIMER_DURATION_MIN} to {TIMER_DURATION_MAX} minutes, got {minutes}"
            raise ValueError(msg)
        return {"type": "TIMER", "durationInSeconds": minutes * 60}

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


def unlisted_zone_keys(d: dict[str, Any], current_zones: frozenset[str]) -> list[str]:
    """Return the keys of d whose zone_id (before any first colon) is not in current_zones, without touching d."""
    return [k for k in d if k.split(":", 1)[0] not in current_zones]


def retry_after_to_minutes(seconds: int) -> str:
    """Format retry-after seconds as minute count for user-facing messages."""
    # WHY: floor at 1, a 30-second cooldown still reads "Try again in 1 min."
    return str(max(1, seconds // 60))


def hard_quota_reserve(daily_limit: int | None) -> int:
    """Return the reserve at which polling pauses, scaled to the daily-limit tier."""
    from .quota_levels import QuotaLevels

    return QuotaLevels(QuotaLevels.normalise_limit(daily_limit)).hard_reserve
