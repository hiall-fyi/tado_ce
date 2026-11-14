"""Tado CE climate helpers: shared optimistic-update + sensor-subscription utilities.

Both heating and AC entities share the same offset / preset / API-
call-with-rollback machinery, so this module owns the helpers and
the climate platform classes call into it. Keeping the logic in
one file means a fix to (e.g.) the optimistic-rollback path lands
once and covers every climate type.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from datetime import datetime

    from homeassistant.components.climate import HVACAction, HVACMode
    from homeassistant.core import HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator
    from .zone_config_manager import ZoneConfigManager
    from .zone_write_ledger import WriteIntent

from .const import DOMAIN, is_valid_device_offset
from .helpers import (
    mask_api_url,
    parse_percentage,
    parse_temperature_celsius,
)
from .optimistic_helpers import (
    _request_still_queued,
    accept_write,
    accepted_values,
    clear_homekit_write,
    clear_optimistic_state,
    fail_write,
    find_intent,
    rearm_hold,
    restart_hold_clock,
    seed_accepted,
    set_optimistic_fields,
)
from .zone_signals import zone_view
from .zone_write_ledger import RestorePlan, ordered_zone_write

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
    from homeassistant.components.climate import PRESET_AWAY, PRESET_HOME

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


async def inject_heating_circuit(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    circuit_number: int | None,
) -> None:
    """Carry an accepted circuit reassignment into coordinator data, the loader cache and the Store."""
    control = await coordinator.data_loader.async_merge_store(
        "heating_circuit_control", {zone_id: {"heatingCircuit": circuit_number}},
    )
    if coordinator.data is None:
        coordinator.data = {}
    coordinator.data["heating_circuit_control"] = control


async def inject_child_lock(
    coordinator: TadoDataUpdateCoordinator,
    serial: str,
    enabled: bool,
) -> bool:
    """Carry an accepted child lock write into the shared zones_info list and its Store; False if the device is gone."""
    zones_info = (coordinator.data or {}).get("zones_info")
    if not isinstance(zones_info, list):
        return False
    for zone in zones_info:
        for device in zone.get("devices") or []:
            if device.get("shortSerialNo") == serial:
                device["childLockEnabled"] = enabled
                if coordinator.data_loader is not None:
                    await coordinator.data_loader.async_update_store("zones_info", zones_info)
                return True
    return False


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

    if config_key == "external_humidity_sensor":
        value = parse_percentage(state.state)
    else:
        attributes = state.attributes
        unit = attributes.get("unit_of_measurement") if isinstance(attributes, Mapping) else None
        value = parse_temperature_celsius(state.state, unit)
    if value is None:
        _LOGGER.debug("Climate: external sensor %s has unusable state %r, ignoring", entity_id, state.state)
    return value


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


def apply_homekit_target(entity: Any, target: float) -> None:
    """Show a HomeKit target; a held request that differs is superseded, since the device now holds another value."""
    entity._attr_native_target_temperature = target
    expected = entity._expected_target_temperature
    # A request queued for sending (even one the timer just fired) has not reached the device, so the device value does not answer it.
    if expected is not None and expected != target and not _request_still_queued(entity):
        clear_optimistic_state(entity)
        clear_homekit_write(entity)


def source_updated_at(coordinator: Any, zone_id: str, source: str, characteristic: str) -> datetime | None:
    """Return when the value last changed on its source (HomeKit) or was last fetched (cloud), or None when unknown."""
    if source == "homekit":
        provider = coordinator.homekit_provider
        if provider is None:
            return None
        getter = provider.get_humidity if characteristic == "humidity" else provider.get_temperature
        return getter(zone_id)[1]  # type: ignore[no-any-return]
    if source == "cloud":
        return coordinator.last_zone_fetch_ts()  # type: ignore[no-any-return]
    return None


def source_update_attrs(coordinator: Any, zone_id: str, temperature_source: str, humidity_source: str) -> dict[str, str | None]:
    """Return the temperature and humidity updated-at attributes, which change only when a source reports."""
    stamps = {
        "temperature_updated_at": source_updated_at(coordinator, zone_id, temperature_source, "temperature"),
        "humidity_updated_at": source_updated_at(coordinator, zone_id, humidity_source, "humidity"),
    }
    return {key: stamp.isoformat() if stamp is not None else None for key, stamp in stamps.items()}


def homekit_target_attribute(coordinator: Any, zone_id: str, shown: float | None) -> float | None:
    """Return the bridge's target when it differs from the card's without a reason, else None."""
    provider = coordinator.homekit_provider
    # A disconnected provider holds an old cache, and a FAN card shows no target.
    if shown is None or provider is None or not provider.is_connected:
        return None
    # An OFF heating card shows the 5 C frost value while the device keeps a setpoint.
    if zone_view(coordinator, zone_id).power != "ON":
        return None
    zcm = coordinator.zone_config_manager
    if zcm is not None and zcm.get_zone_value(zone_id, "display_temp_source", "auto") == "cloud":
        return None
    value = provider.get_target_temperature(zone_id)[0]
    return None if value is None or value == shown else value


def cloud_write_failed(entity: Any) -> HomeAssistantError:
    """The error a failed cloud write raises by default."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key="cloud_write_failed")


@dataclass(frozen=True, slots=True)
class CloudWriteWording:
    """Failure log texts (`%(prefix)s`, `%(zone)s`, `%(reason)s`, `%(error)s`); `single` logs one warning per failure."""

    timeout: str = (
        "%(prefix)s: %(zone)s: %(reason)s timed out after 10s, rolling back optimistic "
        "state so the entity reflects the actual zone state"
    )
    refused: str = "%(prefix)s: %(zone)s: %(reason)s refused by Tado (%(error)s), rolling back optimistic state"
    network: str = "%(prefix)s: %(zone)s: %(reason)s network error (%(error)s), rolling back optimistic state"
    unexpected: str = "%(prefix)s: %(zone)s: %(reason)s failed (%(error)s), rolling back optimistic state"
    failed: str = "%(prefix)s: %(zone)s: %(reason)s failed, reverted to previous state"
    single: bool = False


@dataclass(slots=True)
class RollbackPlan:
    """Optimistic-write + rollback plan applied by `run_cloud_write`."""

    optimistic: dict[str, Any]
    # The names of the attributes a failure restores; the values come from what Tado is known to hold.
    rollback: tuple[str, ...]
    expected: dict[str, Any]
    preserved_attrs: dict[str, Any] | None
    refresh_signal: str
    reason: str
    capture_source: str | None = None
    capture_zone_id: str | None = None
    capture_entity_type: str | None = None
    log_prefix: str = "Climate"
    own_power_off: bool | None = None
    wording: CloudWriteWording = CloudWriteWording()
    log: logging.Logger | None = None
    failure: Callable[[Any], HomeAssistantError] = cloud_write_failed
    # The sequence the caller's own stamp produced; None means the caller stamped nothing and the transaction arms.
    sequence: int | None = None
    # Run before the send when the entry point skipped the reserve gate for a HomeKit route; a refusal restores the card.
    gate: Callable[[], Awaitable[None]] | None = None


def build_plan(
    entity: Any,
    assign: dict[str, Any],
    *,
    also_restore: tuple[str, ...] = (),
    expected: dict[str, Any],
    preserved: dict[str, Any] | None,
    refresh_signal: str,
    reason: str,
    capture_source: str | None = None,
    capture_zone_id: str | None = None,
    capture_entity_type: str | None = None,
    log_prefix: str = "Climate",
    own_power_off: bool | None = None,
    wording: CloudWriteWording = CloudWriteWording(),
    log: logging.Logger | None = None,
    failure: Callable[[Any], HomeAssistantError] = cloud_write_failed,
    sequence: int | None = None,
    gate: Callable[[], Awaitable[None]] | None = None,
) -> RollbackPlan:
    """Plan a write that assigns `assign`; a failure restores `assign` and `also_restore` from what Tado is known to hold."""
    names = tuple(dict.fromkeys((*assign, *also_restore)))
    if sequence is None:
        # Nothing is assigned yet, so the card is still what a first write has to go back to; a stamped caller seeded before it assigned.
        seed_accepted(entity, {name: getattr(entity, name) for name in names})
    return RollbackPlan(
        optimistic=assign,
        rollback=names,
        expected=expected,
        preserved_attrs=preserved,
        refresh_signal=refresh_signal,
        reason=reason,
        capture_source=capture_source,
        capture_zone_id=capture_zone_id,
        capture_entity_type=capture_entity_type,
        log_prefix=log_prefix,
        own_power_off=own_power_off,
        wording=wording,
        log=log,
        failure=failure,
        sequence=sequence,
        gate=gate,
    )


async def run_cloud_write(
    entity: Any,
    api_coro: Coroutine,  # type: ignore[type-arg]
    plan: RollbackPlan,
) -> bool:
    """Run `api_coro` with optimistic UI, rollback on failure, and capture-on-success."""
    from .helpers import async_trigger_immediate_refresh

    log = plan.log or _LOGGER
    texts = {"prefix": plan.log_prefix, "zone": entity._zone_name, "reason": plan.reason}

    # The hold this write owns; a newer write, a device change or a confirming poll replaces or clears it.
    sequence = plan.sequence
    if sequence is None:
        for attr, value in plan.optimistic.items():
            setattr(entity, attr, value)

        await set_optimistic_fields(
            entity, entity.coordinator,
            source="cloud",
            expected=plan.expected,
            preserved_attrs=plan.preserved_attrs,
        )
        entity.async_write_ha_state()
        sequence = entity._optimistic_sequence
    else:
        # The caller stamped at the tap; the hold now counts from the send.
        sequence = await _restart_own_hold(entity, sequence)

    # Taken before the await: a newer write replaces the zone's live intent while this one is in flight.
    intent = find_intent(entity, sequence)

    if plan.gate is not None:
        try:
            await plan.gate()
        except HomeAssistantError:
            # A HomeKit attempt painted the card and the reserve refuses its cloud fallback, so the card goes back like a failed write.
            if asyncio.iscoroutine(api_coro):
                api_coro.close()
            _fail_and_revert(entity, plan, intent, sequence, log)
            raise

    import aiohttp

    from .api_client import raising_network_errors
    from .error_dispatch import dispatch_to_service_call
    from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError

    api_success = False
    warned = False
    unknown_outcome = False
    cause: Exception | None = None
    try:
        # The slot is taken outside the timeout, so waiting for an earlier request does not use up this one's 10 s.
        async with ordered_zone_write(entity.coordinator, entity._zone_id) as waited:
            if waited:
                # Left waiting, the hold would have aged unsent; it counts from the send.
                sequence = await _restart_own_hold(entity, sequence)
            with raising_network_errors():
                async with asyncio.timeout(10):
                    api_success = await api_coro
    except TimeoutError:
        log.warning(plan.wording.timeout, texts)
        warned = True
        unknown_outcome = True
    except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as e:
        log.warning(plan.wording.refused, {**texts, "error": e})
        _fail_and_revert(entity, plan, intent, sequence, log)
        dispatch_to_service_call(e, entity.coordinator.config_entry, entity.hass, entity.coordinator)
    except aiohttp.ClientError as e:
        log.warning(plan.wording.network, {**texts, "error": mask_api_url(str(e) or type(e).__name__)})
        warned = True
        unknown_outcome = True
    except Exception as e:
        # Broad on purpose: any failure restores the card; a typed refusal is dispatched after the revert
        log.warning(plan.wording.unexpected, {**texts, "error": e})
        warned = True
        cause = e
    finally:
        # A wait that is cancelled never awaited the coroutine; closing it keeps the loop from warning.
        if asyncio.iscoroutine(api_coro):
            api_coro.close()

    if api_success:
        log.debug("%s: %s: %s succeeded", plan.log_prefix, entity._zone_name, plan.reason)
        accept_write(entity, intent, plan.optimistic)
        if entity.coordinator.state_reconciler:
            entity.coordinator.state_reconciler.record_local_write(entity._zone_id)
        # Before the refresh, and only on success: the refresh this write triggers must see the zone's new power, and a rolled-back OFF must never set the mark
        if plan.own_power_off is not None:
            entity.coordinator.record_own_power_write(entity._zone_id, powered_off=plan.own_power_off)
        if plan.capture_source is not None and plan.capture_zone_id is not None:
            try:
                await entity.coordinator.async_capture_state(
                    plan.capture_zone_id,
                    plan.capture_entity_type or "climate",
                    plan.capture_source,
                )
            except Exception as e:
                log.warning(
                    "%s: %s, capture-on-success failed (%s); "
                    "user write succeeded, restoration may be unavailable",
                    plan.log_prefix, entity._zone_name, e,
                )
        await async_trigger_immediate_refresh(
            entity.hass, entity.entity_id, plan.refresh_signal,
        )
        return True

    # Before the revert: a guard run by a caller that awaits this write must never see the rolled-back card without the mark.
    if unknown_outcome:
        entity.coordinator.write_ledger.mark_unknown(entity._zone_id)
    if not (plan.wording.single and warned):
        log.warning(plan.wording.failed, texts)
    _fail_and_revert(entity, plan, intent, sequence, log)
    if unknown_outcome:
        await async_trigger_immediate_refresh(entity.hass, entity.entity_id, "write_unknown")
    raise plan.failure(entity) from cause


async def _restart_own_hold(entity: Any, sequence: int | None) -> int | None:
    """Restart the hold clock while the hold is still this write's; return the sequence the write now holds."""
    if entity._optimistic_sequence != sequence:
        return sequence
    restart_hold_clock(entity, entity.coordinator)
    await entity.coordinator.mark_entity_fresh(entity.entity_id)
    restarted: int | None = entity._optimistic_sequence
    return restarted


def _fail_and_revert(entity: Any, plan: RollbackPlan, intent: WriteIntent | None, sequence: int | None, log: logging.Logger) -> None:
    """Ask the ledger what the failed write puts back, then revert it only while its hold is still the entity's."""
    restore = fail_write(entity, intent, plan.rollback) if intent is not None else None
    if not isinstance(restore, RestorePlan):
        # No live intent to judge by (the recorder missed the arm): a hold still this write's goes back to what Tado holds; with no ledger there is nothing to restore and only the hold is cleared.
        restore = RestorePlan(accepted_values(entity, plan.rollback))
    _revert_if_still_held(entity, restore.values, sequence, log, rearm=restore.rearm)


def _revert_if_still_held(
    entity: Any, rollback: Mapping[str, Any], sequence: int | None, log: logging.Logger, *, rearm: WriteIntent | None = None,
) -> None:
    """Revert a failed write only while its hold is still the entity's; a cleared hold (None) is not its own."""
    if entity._optimistic_sequence != sequence:
        log.debug("%s: failed write no longer holds the optimistic state, leaving the card as it is", entity._zone_name)
        return
    revert_failed_write(entity, rollback, rearm=rearm)
    entity.async_write_ha_state()


# The card attributes every climate write can move, in the order a snapshot lists them.
CLIMATE_STATE_NAMES = ("_attr_hvac_mode", "_attr_hvac_action", "_overlay_type", "_attr_native_target_temperature")


def capture_climate_rollback_state(entity: Any, include: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Read the card's climate attributes (only `include` when given)."""
    state = {name: getattr(entity, name) for name in CLIMATE_STATE_NAMES}
    if include is None:
        return state
    return {attr: value for attr, value in state.items() if attr in include}


def observed_climate_state(entity: Any) -> dict[str, Any]:
    """The climate attributes the card shows after taking Tado's values, with an AC's fan and swing axes."""
    state = capture_climate_rollback_state(entity)
    for name in ("_attr_fan_mode", "_attr_swing_mode", "_attr_swing_horizontal_mode"):
        if hasattr(entity, name):
            state[name] = getattr(entity, name)
    return state


def revert_failed_write(entity: Any, state: Mapping[str, Any], *, rearm: WriteIntent | None = None) -> None:
    """Put the restore plan's values back and clear the hold, or keep waiting for an older accepted write's echo."""
    for attr, value in state.items():
        setattr(entity, attr, value)
    clear_optimistic_state(entity)
    clear_homekit_write(entity)
    if rearm is not None:
        rearm_hold(entity, rearm)


async def api_call_with_rollback(
    entity: Any,
    api_coro: Coroutine,  # type: ignore[type-arg]
    *,
    hvac_mode: HVACMode,
    hvac_action: HVACAction,
    overlay_type: str | None = "MANUAL",
    target_temp: float | None = None,
    reason: str,
    capture_source: str | None = None,
    sequence: int | None = None,
    gate: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Optimistic-write + rollback shim; a failure restores every climate attribute from what Tado is known to hold."""
    # Runtime copy: the module-level HVACMode is type-only
    from homeassistant.components.climate import HVACMode as _HVACMode

    optimistic: dict[str, Any] = {
        "_attr_hvac_mode": hvac_mode,
        "_attr_hvac_action": hvac_action,
        "_overlay_type": overlay_type,
    }
    expected: dict[str, Any] = {"hvac_mode": hvac_mode, "hvac_action": hvac_action}
    if overlay_type is None:
        # An AUTO write is confirmed by the overlay being gone, the schedule block may read off
        expected["overlay_present"] = False

    if target_temp is not None:
        optimistic["_attr_native_target_temperature"] = target_temp
        expected["target_temperature"] = target_temp

    plan = build_plan(
        entity,
        optimistic,
        also_restore=CLIMATE_STATE_NAMES,
        expected=expected,
        preserved=(
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
        own_power_off=hvac_mode == _HVACMode.OFF,
        sequence=sequence,
        gate=gate,
    )
    return await run_cloud_write(entity, api_coro, plan)


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
            entity._attr_native_current_temperature = ext_temp
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
