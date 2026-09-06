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
from datetime import UTC, datetime
import logging
import random
from typing import TYPE_CHECKING, Any

from .const import (
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


def zone_confirmed_on(coordinator: TadoDataUpdateCoordinator, zone_id: str) -> bool:
    """Whether the last cloud poll has this zone's power ON.

    Not the entity's own `_attr_hvac_mode`, which flips optimistically
    before a write is confirmed.
    """
    zone_data = get_zone_state(coordinator.data, zone_id) or {}
    setting = zone_data.get("setting") or {}
    return setting.get("power") == "ON"


def merge_homekit_into_zone_data(
    zone_data: dict[str, Any],
    zone_id: str,
    coordinator: TadoDataUpdateCoordinator,
) -> dict[str, Any]:
    """Overlay fresh HomeKit temperature onto cloud zone data.

    Returns a shallow copy of zone_data with sensorDataPoints merged.
    Temperature uses HomeKit when fresh (real-time, accurate).
    Humidity always uses cloud (bridge humidity is stale/unreliable).
    If HomeKit is not connected or has no fresh data, returns zone_data unchanged.

    Safe to call from any entity regardless of base class.
    """
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
    raise last_error  # type: ignore[misc]


def _get_coordinator(hass: HomeAssistant, entry_id: str) -> TadoDataUpdateCoordinator:
    """Get coordinator from entry_id, or None."""
    try:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and hasattr(entry, "runtime_data") and entry.runtime_data is not None:
            return entry.runtime_data  # type: ignore[no-any-return]
    except (AttributeError, TypeError):
        pass
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
            pass

    # Map internal storage values to API-accepted values
    mode = _map_overlay_to_api(mode)

    if mode == "TIMER":
        return {"type": "TIMER", "durationInSeconds": duration * 60}

    return {"type": mode}


def get_zone_overlay_termination(hass: HomeAssistant, zone_id: str, entry_id: str | None = None) -> dict[str, Any]:
    """Get the termination dict for overlay API calls with per-zone support.

    Priority:
    1. Per-zone overlay_mode (if zone_config_manager available and zone has override)
    2. Global overlay_mode (from coordinator)

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
            pass

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


def hard_quota_reserve(daily_limit: int | None) -> int:
    """Return the reserve at which polling pauses, scaled to the daily-limit tier."""
    # WHY: a hard floor kept for manual actions, unlike low_quota_threshold's
    # "start conserving" signal. Nothing optional may spend into it.
    from .const import QUOTA_RESERVE_CALLS, QUOTA_RESERVE_PERCENT

    if daily_limit is None or daily_limit <= 0:
        return QUOTA_RESERVE_CALLS
    return max(QUOTA_RESERVE_CALLS, int(daily_limit * QUOTA_RESERVE_PERCENT))
