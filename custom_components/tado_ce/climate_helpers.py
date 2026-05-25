"""Tado CE climate helpers — shared optimistic-update + sensor-subscription utilities.

Both heating and AC entities share the same offset / preset / API-
call-with-rollback machinery, so this module owns the helpers and
the climate platform classes call into it. Keeping the logic in
one file means a fix to (e.g.) the optimistic-rollback path lands
once and covers every climate type.
"""

from __future__ import annotations

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
    outside the valid range — the caller keeps its previous value
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
                    "range — ignoring this reading, keeping the previous "
                    "cached value",
                    zone_id, value,
                )
                return None
            return value  # type: ignore[no-any-return]
        return None
    except Exception:
        # Caller keeps its previous offset — better than crashing the
        # climate entity over a transient cache read failure.
        return None


def update_offset_clamp(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
) -> str | None:
    """Return the offset-sync clamp signal for one zone, or None when not applicable.

    Values: "none" (no clamp), "hit_max" (+10°C limit hit),
    "hit_min" (-10°C limit hit). The climate entity surfaces this
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
    except Exception:
        _LOGGER.debug(
            "Climate: could not derive preset mode from home state — "
            "keeping previous value",
        )
    return None


def inject_presence_state(
    coordinator: TadoDataUpdateCoordinator,
    presence: str | None,
    locked: bool,
) -> None:
    """Inject presence state into coordinator + DataLoader caches after a local write.

    Used after the user changes presence via the select entity or
    climate preset. The API call succeeds but the next coordinator
    poll may not fetch home_state (Home State Sync disabled), so
    we inject the known state into both caches so entities pick it
    up on the next update.

    `presence=None` is the "auto" path — the API call deleted the
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

    # Also update DataLoader cache so the next _async_post_sync_processing
    # reads the injected value instead of the stale cached one.
    if coordinator.data_loader is not None:
        coordinator.data_loader.update_cache("home_state", home_state)


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
            "Climate: external sensor %s has non-numeric state %r — "
            "ignoring",
            entity_id, state.state,
        )
        return None


async def api_call_with_rollback(
    entity: Any,
    api_coro: Coroutine,  # type: ignore[type-arg]
    *,
    hvac_mode: HVACMode,
    hvac_action: HVACAction,
    overlay_type: str | None = "MANUAL",
    target_temp: float | None = None,
    reason: str,
) -> bool:
    """Run an API call wrapped in optimistic-update + rollback for climate entities.

    Saves the entity's old state, applies the optimistic update,
    fires the API call with a 10-second timeout, then either
    confirms (refresh + record local write) or rolls back to the
    saved state and raises `HomeAssistantError` so the user sees
    the failure surface in the HA UI.
    """
    import asyncio

    from .helpers import async_trigger_immediate_refresh

    old_mode = entity._attr_hvac_mode
    old_action = entity._attr_hvac_action
    old_overlay = entity._overlay_type

    entity._attr_hvac_mode = hvac_mode
    entity._attr_hvac_action = hvac_action
    entity._overlay_type = overlay_type
    await set_optimistic_fields(
        entity, entity.coordinator,
        expected={"hvac_mode": hvac_mode, "hvac_action": hvac_action},
        preserved_attrs={
            "fan_mode": getattr(entity, "_attr_fan_mode", None),
            "swing_mode": getattr(entity, "_attr_swing_mode", None),
            "swing_horizontal_mode": getattr(entity, "_attr_swing_horizontal_mode", None),
        } if hasattr(entity, "_attr_fan_mode") else None,
    )
    entity.async_write_ha_state()

    api_success = False
    try:
        async with asyncio.timeout(10):
            api_success = await api_coro
    except TimeoutError:
        _LOGGER.warning(
            "Climate: %s — %s timed out after 10s, rolling back optimistic "
            "state so the entity reflects the actual zone state",
            entity._zone_name, reason,
        )
    except Exception as e:
        _LOGGER.warning(
            "Climate: %s — %s failed (%s), rolling back optimistic state",
            entity._zone_name, reason, e,
        )

    if api_success:
        _LOGGER.debug(
            "Climate: %s — %s succeeded", entity._zone_name, reason,
        )
        # Record the local write so the HomeKit bridge can't push a
        # stale value over the optimistic state during the protection
        # window.
        if entity.coordinator.state_reconciler:
            entity.coordinator.state_reconciler.record_local_write(entity._zone_id)
        await async_trigger_immediate_refresh(entity.hass, entity.entity_id, "hvac_mode_change")
    else:
        _LOGGER.warning(
            "Climate: %s — %s failed, reverted to previous state",
            entity._zone_name, reason,
        )
        entity._attr_hvac_mode = old_mode
        entity._attr_hvac_action = old_action
        entity._overlay_type = old_overlay
        clear_optimistic_state(entity)
        entity.async_write_ha_state()
        raise HomeAssistantError(
            f"{entity._zone_name}: {reason} failed",
            translation_domain=DOMAIN,
        )

    return api_success


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
    """Subscribe a climate entity to external temperature + humidity sensor changes.

    Shared by `TadoClimate` (heating) and `TadoACClimate` (AC).
    On every external-sensor update, refreshes the entity's
    `current_temperature`, `current_humidity`, and source tracking
    attrs and writes the new HA state.
    """
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
            "Climate: %s external sensor updated — climate state refreshed",
            label or zone_id,
        )

    return subscribe_external_sensors(entity, zone_id, _on_external_sensor_change)
