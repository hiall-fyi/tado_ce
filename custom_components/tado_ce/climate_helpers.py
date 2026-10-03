"""Tado CE climate helpers: shared optimistic-update + sensor-subscription utilities.

Both heating and AC entities share the same offset / preset / API-
call-with-rollback machinery, so this module owns the helpers and
the climate platform classes call into it. Keeping the logic in
one file means a fix to (e.g.) the optimistic-rollback path lands
once and covers every climate type.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from homeassistant.components.climate import HVACAction, HVACMode  # type: ignore[attr-defined]
    from homeassistant.core import HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator
    from .zone_config_manager import ZoneConfigManager

from .const import DOMAIN, is_valid_device_offset
from .optimistic_helpers import clear_optimistic_state, set_optimistic_fields

_LOGGER = logging.getLogger(__name__)


def update_offset(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
) -> float | None:
    """Return the cached device offset for one zone in °C, or None when unavailable.

    Returns None when offset sync is disabled in config, when no
    offset is cached for the zone, or when a cached value falls
    outside the valid range. The caller keeps its previous value
    in any of those cases.
    """
    try:
        config_manager = coordinator.config_manager
        if not config_manager or not config_manager.get_offset_enabled():
            return None

        offsets = (coordinator.data or {}).get("offsets")
        if offsets:
            value = offsets.get(zone_id)
            if value is not None and not is_valid_device_offset(value):
                _LOGGER.warning(
                    "Climate: zone %s offset %s°C outside the valid "
                    "range, ignoring this reading, keeping the previous "
                    "cached value",
                    zone_id, value,
                )
                return None
            return value  # type: ignore[no-any-return]
        return None
    except (KeyError, TypeError, AttributeError):
        # Caller keeps its previous offset, better than crashing the
        # climate entity over a transient cache read failure.
        return None


def update_offset_clamp(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
) -> str | None:
    """Return the offset-sync clamp signal for one zone, or None when not applicable.

    Values: "none" (no clamp), "hit_max" (+10°C limit hit),
    "hit_min" (-9.9°C limit hit). The climate entity surfaces this
    in extra_state_attributes so users can see when the physical
    gap exceeds Tado's stored-offset range.
    """
    try:
        config_manager = coordinator.config_manager
        if not config_manager or not config_manager.get_offset_enabled():
            return None

        clamps = (coordinator.data or {}).get("offset_clamps")
        if not isinstance(clamps, dict):
            return None
        value = clamps.get(zone_id)
        if value in ("none", "hit_max", "hit_min"):
            return value  # type: ignore[no-any-return]
        return None
    except AttributeError:
        # coordinator may not be fully initialised yet; return None
        # so the entity doesn't surface the attribute prematurely.
        return None


def update_preset_mode(coordinator: TadoDataUpdateCoordinator) -> str | None:
    """Return PRESET_HOME / PRESET_AWAY from cached home state, or None on error."""
    from homeassistant.components.climate import PRESET_AWAY, PRESET_HOME  # type: ignore[attr-defined]

    try:
        home_state = (coordinator.data or {}).get("home_state")
        if home_state:
            presence = home_state.get("presence", "HOME")
            return PRESET_HOME if presence == "HOME" else PRESET_AWAY
    except (KeyError, AttributeError):
        _LOGGER.debug(
            "Climate: could not derive preset mode from home state, "
            "keeping previous value",
        )
    return None


async def inject_presence_state(
    coordinator: TadoDataUpdateCoordinator,
    presence: str | None,
    locked: bool,
) -> None:
    """Inject presence state into coordinator + DataLoader caches after a local write.

    Used after the user changes presence via the select entity or
    climate preset. The API call succeeds but the next coordinator
    poll may not fetch home_state (Home State Sync disabled), so
    we inject the known state into both caches so entities pick it
    up on the next update, and persist it to the Store so it survives
    a restart even when no poll ever re-saves it.

    `presence=None` is the "auto" path: the API call deleted the
    presence lock and geofencing now decides, so we don't yet know
    the resulting presence until the next poll. Forcing "HOME"
    would poison the cache for users who switch to auto while
    physically away.
    """
    existing = (coordinator.data or {}).get("home_state") if coordinator.data else None
    if not isinstance(existing, dict):
        existing = {}

    if presence is None:
        resolved_presence = existing.get("presence")
    else:
        resolved_presence = presence

    home_state: dict[str, Any] = {"presenceLocked": locked}
    if resolved_presence is not None:
        home_state["presence"] = resolved_presence

    if coordinator.data is None:
        coordinator.data = {}
    coordinator.data["home_state"] = home_state

    # Persist to the Store, not just the in-memory cache. When Home State
    # Sync is off no poll ever re-saves home_state, so a cache-only write
    # would be lost on the next restart, and the user's locked presence would
    # revert to whatever the last poll persisted hours earlier.
    if coordinator.data_loader is not None:
        await coordinator.data_loader.async_update_store("home_state", home_state)


def read_external_sensor(
    hass: HomeAssistant,
    zone_config_manager: ZoneConfigManager | None,
    zone_id: str,
    config_key: str,
) -> float | None:
    """Return the numeric value of an external HA sensor entity, or None.

    Returns None when no external sensor is configured for the
    zone, the sensor is unavailable / unknown, or the state isn't
    numeric.
    """
    if not zone_config_manager:
        return None

    config = zone_config_manager.get_zone_config(zone_id)
    entity_id = config.get(config_key, "")
    if not entity_id:
        return None

    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None

    try:
        return float(state.state)
    except (ValueError, TypeError):
        _LOGGER.debug(
            "Climate: external sensor %s has non-numeric state %r, "
            "ignoring",
            entity_id, state.state,
        )
        return None


def ac_power_percentage(activity_data: dict[str, Any]) -> int:
    """Coerce a zone's acPower reading to a percentage across firmware shapes."""
    # Newer firmware reports acPower.value ("ON"/"OFF"), older only .percentage.
    ac_power = activity_data.get("acPower") or {}
    power = ac_power.get("percentage")
    if power is None:
        power = 100 if ac_power.get("value") == "ON" else 0
    return power


def ac_power_is_on(activity_data: dict[str, Any]) -> bool:
    """Return True when the zone's own AC is powered on (either firmware shape)."""
    return ac_power_percentage(activity_data) > 0


@dataclass(slots=True)
class RollbackPlan:
    """Optimistic-write + rollback plan applied by `_attempt_with_rollback`."""

    optimistic: dict[str, Any]
    rollback: dict[str, Any]
    expected: dict[str, Any]
    preserved_attrs: dict[str, Any] | None
    refresh_signal: str
    reason: str
    capture_source: str | None = None
    capture_zone_id: str | None = None
    capture_entity_type: str | None = None
    log_prefix: str = "Climate"


async def _attempt_with_rollback(
    entity: Any,
    api_coro: Coroutine,  # type: ignore[type-arg]
    plan: RollbackPlan,
) -> bool:
    """Run `api_coro` with optimistic UI, rollback on failure, and capture-on-success."""
    import asyncio

    from .helpers import async_trigger_immediate_refresh

    for attr, value in plan.optimistic.items():
        setattr(entity, attr, value)

    await set_optimistic_fields(
        entity, entity.coordinator,
        expected=plan.expected,
        preserved_attrs=plan.preserved_attrs,
    )
    entity.async_write_ha_state()

    import aiohttp

    from .error_dispatch import dispatch_to_service_call
    from .exceptions import TadoAuthError, TadoRateLimitError

    api_success = False
    try:
        async with asyncio.timeout(10):
            api_success = await api_coro
    except TimeoutError:
        _LOGGER.warning(
            "%s: %s: %s timed out after 10s, rolling back optimistic "
            "state so the entity reflects the actual zone state",
            plan.log_prefix, entity._zone_name, plan.reason,
        )
    except (TadoAuthError, TadoRateLimitError) as e:
        for attr, value in plan.rollback.items():
            setattr(entity, attr, value)
        clear_optimistic_state(entity)
        entity.async_write_ha_state()
        dispatch_to_service_call(e, entity.coordinator.config_entry, entity.hass, entity.coordinator)
    except aiohttp.ClientError as e:
        _LOGGER.warning(
            "%s: %s: %s network error (%s), rolling back optimistic state",
            plan.log_prefix, entity._zone_name, plan.reason, e,
        )

    if api_success:
        _LOGGER.debug(
            "%s: %s: %s succeeded",
            plan.log_prefix, entity._zone_name, plan.reason,
        )
        if entity.coordinator.state_reconciler:
            entity.coordinator.state_reconciler.record_local_write(entity._zone_id)
        if plan.capture_source is not None and plan.capture_zone_id is not None:
            try:
                await entity.coordinator.async_capture_state(
                    plan.capture_zone_id,
                    plan.capture_entity_type or "climate",
                    plan.capture_source,
                )
            except Exception as e:
                _LOGGER.warning(
                    "%s: %s, capture-on-success failed (%s); "
                    "user write succeeded, restoration may be unavailable",
                    plan.log_prefix, entity._zone_name, e,
                )
        await async_trigger_immediate_refresh(
            entity.hass, entity.entity_id, plan.refresh_signal,
        )
        return True

    _LOGGER.warning(
        "%s: %s: %s failed, reverted to previous state",
        plan.log_prefix, entity._zone_name, plan.reason,
    )
    for attr, value in plan.rollback.items():
        setattr(entity, attr, value)
    clear_optimistic_state(entity)
    entity.async_write_ha_state()
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="cloud_write_failed",
    )


async def api_call_with_rollback(
    entity: Any,
    api_coro: Coroutine,  # type: ignore[type-arg]
    *,
    hvac_mode: HVACMode,
    hvac_action: HVACAction,
    overlay_type: str | None = "MANUAL",
    target_temp: float | None = None,
    rollback_target_temp: float | None = None,
    reason: str,
    capture_source: str | None = None,
) -> bool:
    """Climate-flavoured optimistic-write + rollback shim around `_attempt_with_rollback`.

    Pass `rollback_target_temp` when the caller already applied the new target, since the plan
    captures the attribute after that.
    """
    optimistic: dict[str, Any] = {
        "_attr_hvac_mode": hvac_mode,
        "_attr_hvac_action": hvac_action,
        "_overlay_type": overlay_type,
    }
    rollback: dict[str, Any] = {
        "_attr_hvac_mode": entity._attr_hvac_mode,
        "_attr_hvac_action": entity._attr_hvac_action,
        "_overlay_type": entity._overlay_type,
    }
    expected: dict[str, Any] = {"hvac_mode": hvac_mode, "hvac_action": hvac_action}

    if target_temp is not None:
        optimistic["_attr_target_temperature"] = target_temp
        expected["target_temperature"] = target_temp
        rollback["_attr_target_temperature"] = (
            rollback_target_temp
            if rollback_target_temp is not None
            else entity._attr_target_temperature
        )

    plan = RollbackPlan(
        optimistic=optimistic,
        rollback=rollback,
        expected=expected,
        preserved_attrs=(
            {
                "fan_mode": getattr(entity, "_attr_fan_mode", None),
                "swing_mode": getattr(entity, "_attr_swing_mode", None),
                "swing_horizontal_mode": getattr(entity, "_attr_swing_horizontal_mode", None),
            }
            if hasattr(entity, "_attr_fan_mode") else None
        ),
        refresh_signal="hvac_mode_change",
        reason=reason,
        capture_source=capture_source,
        capture_zone_id=getattr(entity, "_zone_id", None),
        capture_entity_type=getattr(entity, "_entity_type", "climate"),
        log_prefix="Climate",
    )
    return await _attempt_with_rollback(entity, api_coro, plan)


class SensorProxy:
    """Minimal stand-in for an HA entity, just exposing `hass` + `coordinator`."""

    def __init__(self, hass: HomeAssistant, coordinator: TadoDataUpdateCoordinator) -> None:
        """Initialise the proxy."""
        self.hass = hass
        self.coordinator = coordinator


def subscribe_external_sensors(
    entity: Any,
    zone_id: str,
    on_change: Callable[[Event[EventStateChangedData]], None],
    *,
    include_humidity: bool = True,
) -> list[CALLBACK_TYPE]:
    """Subscribe to external temp/humidity sensor changes, returning unsub callbacks.

    Filters out unavailable / unknown / non-numeric updates before
    delegating to `on_change`, so the caller doesn't have to repeat
    the validation in every consumer.
    """
    zcm = entity.coordinator.zone_config_manager
    if not zcm:
        return []

    config = zcm.get_zone_config(zone_id)
    entity_ids: list[str] = []

    temp_sensor = config.get("external_temp_sensor", "")
    if temp_sensor:
        entity_ids.append(temp_sensor)

    if include_humidity:
        humidity_sensor = config.get("external_humidity_sensor", "")
        if humidity_sensor:
            entity_ids.append(humidity_sensor)

    if not entity_ids:
        return []

    @callback
    def _validated_change(event: Event[EventStateChangedData]) -> None:
        """Filter invalid states then delegate to caller's on_change."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable", ""):
            return
        try:
            float(new_state.state)
        except (ValueError, TypeError):
            return
        on_change(event)

    unsub = async_track_state_change_event(entity.hass, entity_ids, _validated_change)
    return [unsub]


def unsubscribe_external_sensors(unsub_list: list[CALLBACK_TYPE]) -> None:
    """Invoke every unsubscribe callback and clear the list in place."""
    for unsub in unsub_list:
        unsub()
    unsub_list.clear()


def setup_climate_external_sensor_subscription(
    entity: Any,
    zone_id: str,
    unsub_list: list[CALLBACK_TYPE],
    *,
    label: str = "",
) -> list[CALLBACK_TYPE]:
    """Subscribe a climate entity to external temperature + humidity sensor changes."""
    unsubscribe_external_sensors(unsub_list)

    zcm = entity.coordinator.zone_config_manager

    @callback
    def _on_external_sensor_change(event: Event[EventStateChangedData]) -> None:
        ext_temp = read_external_sensor(entity.hass, zcm, zone_id, "external_temp_sensor")
        if ext_temp is not None:
            entity._attr_current_temperature = ext_temp
            entity._temperature_source = "external"

        ext_hum = read_external_sensor(entity.hass, zcm, zone_id, "external_humidity_sensor")
        if ext_hum is not None:
            entity._attr_current_humidity = ext_hum
            entity._humidity_source = "external"

        entity.async_write_ha_state()
        _LOGGER.debug(
            "Climate: %s external sensor updated, climate state refreshed",
            label or zone_id,
        )

    return subscribe_external_sensors(entity, zone_id, _on_external_sensor_change)
