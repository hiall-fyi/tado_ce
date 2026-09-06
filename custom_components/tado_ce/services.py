"""Tado CE custom HA services: climate timer, water heater timer, offsets, open window, restore."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import async_get_platforms
import voluptuous as vol

from . import ratelimit as _ratelimit
from .const import (
    DOMAIN,
    FORCEABLE_FETCH_HOME_STATE,
    FORCEABLE_FETCH_MOBILE,
    FORCEABLE_FETCH_ZONE_STATES,
    OPEN_WINDOW_DEFAULT_TEMP,
    OPEN_WINDOW_DEFAULT_TIMEOUT,
    SERVICE_ACTIVATE_OPEN_WINDOW,
    SERVICE_ADD_METER_READING,
    SERVICE_DEACTIVATE_OPEN_WINDOW,
    SERVICE_GET_TEMP_OFFSET,
    SERVICE_IDENTIFY_DEVICE,
    SERVICE_REFRESH,
    SERVICE_RESTORE_PREVIOUS_STATE,
    SERVICE_RESUME_SCHEDULE,
    SERVICE_SET_AWAY_CONFIG,
    SERVICE_SET_CLIMATE_TIMER,
    SERVICE_SET_OPEN_WINDOW_MODE,
    SERVICE_SET_SCHEDULE_TEMPERATURE,
    SERVICE_SET_TEMP_OFFSET,
    SERVICE_SET_WATER_HEATER_TIMER,
    SERVICE_TURN_OFF_ALL_ZONES,
    is_climate_zone,
)
from .helpers import async_trigger_immediate_refresh, build_timer_termination, mask_serial
from .services_helpers import run_service_call
from .write_optimizer import ResumeGuard

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader
    from .state_restore_manager import CapturedState

_LOGGER = logging.getLogger(__name__)

# Timer validation constants
TIMER_PARTS_COUNT = 3  # HH:MM:SS format
MAX_TIMER_HOURS = 24
MAX_TIMER_MINUTES = 59
MAX_TIMER_SECONDS = 59
MAX_TIMER_DURATION_MINUTES = 1440  # 24 hours in minutes

# Water heater temperature bounds (°C)
WATER_HEATER_MIN_TEMP = 30
WATER_HEATER_MAX_TEMP = 80
















def _raise_service_error(
    translation_key: str,
    **placeholders: object,
) -> None:
    """Raise a translated `HomeAssistantError` so the failure shows as an HA UI toast."""
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()},
    )


def _find_entity_by_id(
    hass: HomeAssistant,
    platform_domain: str,
    entity_id: str,
) -> object | None:
    """Look up an entity instance by entity_id via the public `async_get_platforms` API."""
    for platform in async_get_platforms(hass, DOMAIN):
        if platform.domain == platform_domain:
            for ent in platform.entities.values():
                if ent.entity_id == entity_id:
                    return ent
    return None


def _get_zone_device_serial(zone_id: str, data_loader: DataLoader | None = None) -> str | None:
    """Return the first device serial for a zone, or None when not in cache."""
    try:
        if data_loader is None:
            return None
        zones_info = data_loader.load_zones_info_file()
        if not zones_info:
            return None

        for zone in zones_info:
            if str(zone.get("id")) == zone_id:
                # `devices` can come back as null on a fresh zone;
                # `or []` covers both null and missing-key cases.
                for device in zone.get("devices") or []:
                    serial = device.get("shortSerialNo")
                    if serial:
                        return serial  # type: ignore[no-any-return]
        return None
    except (AttributeError, TypeError, KeyError, OSError) as e:
        # AttributeError / TypeError / KeyError: malformed zones_info.
        # OSError: load_zones_info_file disk read failure.
        _LOGGER.warning(
            "Services: could not look up device serial for zone "
            "%s (%s), service call will skip per-device write",
            zone_id, e,
        )
        return None


def _get_zone_device_serials(zone_id: str, data_loader: DataLoader | None = None) -> list[str]:
    """Return every device serial in a zone, used for multi-TRV writes (e.g. set_temperature_offset)."""
    serials = []
    try:
        if data_loader is None:
            return []
        zones_info = data_loader.load_zones_info_file()
        if not zones_info:
            return []

        for zone in zones_info:
            if str(zone.get("id")) == zone_id:
                for device in zone.get("devices") or []:
                    serial = device.get("shortSerialNo")
                    if serial:
                        serials.append(serial)
                break
        return serials
    except (AttributeError, TypeError, KeyError, OSError) as e:
        _LOGGER.warning(
            "Services: could not look up device serials for zone "
            "%s (%s), service call will skip per-device write",
            zone_id, e,
        )
        return []


def _expand_group_entity_ids(
    hass: HomeAssistant, entity_ids: list[Any], allowed_domains: list[Any] | None = None,
) -> list[Any]:
    """Replace `group.*` entries with their members, optionally filtered by domain."""
    expanded_ids = []
    for entity_id in entity_ids:
        if entity_id.startswith("group."):
            group_state = hass.states.get(entity_id)
            if group_state and "entity_id" in group_state.attributes:
                group_members = group_state.attributes["entity_id"]
                if allowed_domains:
                    group_members = [eid for eid in group_members if eid.split(".")[0] in allowed_domains]
                expanded_ids.extend(group_members)
                _LOGGER.debug(
                    "Services: expanded group %s to %d member(s)",
                    entity_id, len(group_members),
                )
            else:
                _LOGGER.warning(
                    "Services: group %s not found or has no members "
                    ", service call will skip this entry",
                    entity_id,
                )
        else:
            if allowed_domains:
                domain = entity_id.split(".")[0]
                if domain not in allowed_domains:
                    _LOGGER.debug(
                        "Services: skipping %s, domain not in "
                        "allowed list %s",
                        entity_id, allowed_domains,
                    )
                    continue
            expanded_ids.append(entity_id)
    return expanded_ids


def _resolve_coordinator(hass: HomeAssistant, entity_id: str) -> TadoDataUpdateCoordinator:
    """Resolve which Tado CE coordinator owns this entity_id.

    Raises a translated `HomeAssistantError` when the entity
    isn't ours or its config entry isn't loaded; those errors
    surface in the HA UI.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)

    if entity_entry is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="entity_not_found",
            translation_placeholders={"entity_id": entity_id},
        )

    if entity_entry.platform != DOMAIN:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="entity_not_tado_ce",
            translation_placeholders={"entity_id": entity_id, "platform": entity_entry.platform},
        )

    config_entry_id = entity_entry.config_entry_id
    if config_entry_id is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="entity_no_config_entry",
            translation_placeholders={"entity_id": entity_id},
        )

    config_entry = hass.config_entries.async_get_entry(config_entry_id)
    if config_entry is None or not hasattr(config_entry, "runtime_data") or config_entry.runtime_data is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"entity_id": entity_id, "config_entry_id": config_entry_id},
        )

    from .helpers import mask_home_id

    coordinator = config_entry.runtime_data
    _LOGGER.debug(
        "Services: resolved %s → entry %s (home_id %s)",
        entity_id,
        config_entry_id,
        mask_home_id(coordinator.home_id),
    )
    return coordinator  # type: ignore[no-any-return]


def _resolve_coordinator_for_device(hass: HomeAssistant, device_serial: str) -> TadoDataUpdateCoordinator:
    """Resolve which Tado CE coordinator owns this device serial."""
    from homeassistant.helpers import device_registry as dr

    device_registry = dr.async_get(hass)

    from .helpers import mask_home_id

    for device in device_registry.devices.values():
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier == device_serial:
                for config_entry_id in device.config_entries:
                    config_entry = hass.config_entries.async_get_entry(config_entry_id)
                    if (
                        config_entry is not None
                        and hasattr(config_entry, "runtime_data")
                        and config_entry.runtime_data is not None
                    ):
                        coordinator = config_entry.runtime_data
                        _LOGGER.debug(
                            "Services: resolved device %s → entry %s "
                            "(home_id %s)",
                            mask_serial(device_serial),
                            config_entry_id,
                            mask_home_id(coordinator.home_id),
                        )
                        return coordinator  # type: ignore[no-any-return]

                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_entry_not_loaded",
                    translation_placeholders={"device_serial": device_serial},
                )

    # Only bridges are registered with a device-serial identifier
    # (register_bridge_devices); TRVs and thermostats attach to a zone-keyed
    # device, so the registry lookup above never matches them. Fall back
    # to the zones_info each loaded coordinator caches, the same serial→device
    # data the offset / child-lock services route through.
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(config_entry, "runtime_data", None)
        if coordinator is None:
            continue
        zones_info = coordinator.data_loader.load_zones_info_file() or []
        for zone in zones_info:
            for device in zone.get("devices") or []:
                if device.get("shortSerialNo") == device_serial:
                    _LOGGER.debug(
                        "Services: resolved device %s via zones_info (home_id %s)",
                        mask_serial(device_serial), mask_home_id(coordinator.home_id),
                    )
                    return coordinator  # type: ignore[no-any-return]

    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="device_not_found",
        translation_placeholders={"device_serial": device_serial},
    )


def _resolve_single_coordinator(hass: HomeAssistant) -> TadoDataUpdateCoordinator:
    """Pick the only loaded Tado CE coordinator, or raise when ambiguous.

    Used by home-level services (e.g. `add_meter_reading`) that
    can't route via entity_id. Multi-home setups must route
    explicitly; the raised error lists home_ids so the user
    can disambiguate.
    """
    from .helpers import mask_home_id

    entries = hass.config_entries.async_entries(DOMAIN)
    loaded = [e for e in entries if hasattr(e, "runtime_data") and e.runtime_data is not None]

    if len(loaded) == 0:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="no_entries_loaded",
        )

    if len(loaded) == 1:
        coordinator = loaded[0].runtime_data
        _LOGGER.debug(
            "Services: single entry resolved (home_id %s)",
            mask_home_id(coordinator.home_id),
        )
        return coordinator  # type: ignore[no-any-return]

    home_ids = [e.runtime_data.home_id for e in loaded]
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="multiple_entries",
        translation_placeholders={"home_ids": ", ".join(home_ids)},
    )


def _build_setting_from_captured(
    captured: CapturedState,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild a `set_zone_overlay` payload from a previously captured zone state."""
    setting: dict[str, Any] = {}

    if captured.entity_type in ("climate_heating", "climate_ac"):
        setting["type"] = (
            "HEATING" if captured.entity_type == "climate_heating" else "AIR_CONDITIONING"
        )
        setting["power"] = captured.power or "ON"
        if captured.entity_type == "climate_ac" and captured.hvac_mode is not None:
            setting["mode"] = captured.hvac_mode  # Raw API mode: COOL/HEAT/DRY/FAN
        if captured.temperature is not None:
            setting["temperature"] = {"celsius": captured.temperature}
        if captured.fan_mode is not None:  # AC only
            # Legacy units use the singular fanSpeed payload field; the key
            # was recorded at capture time. Older snapshots without it
            # default to the modern fanLevel.
            setting[captured.fan_key or "fanLevel"] = captured.fan_mode
        if captured.swing_mode is not None:  # AC only
            setting["verticalSwing"] = captured.swing_mode
        if captured.horizontal_swing_mode is not None:  # AC only
            setting["horizontalSwing"] = captured.horizontal_swing_mode

    elif captured.entity_type == "water_heater":
        setting["type"] = "HOT_WATER"
        setting["power"] = captured.power or "ON"
        if captured.temperature is not None:
            setting["temperature"] = {"celsius": captured.temperature}

    # The captured termination came from an overlay GET, which
    # carries response-only fields the PUT endpoint rejects
    # (HTTP 422), strip them before sending.
    termination: dict[str, Any] = _sanitize_termination_for_request(captured.termination)

    return setting, termination


def _sanitize_termination_for_request(
    captured_termination: dict[str, Any] | None,
) -> dict[str, Any]:
    """Trim response-only fields off a captured termination so the cloud accepts the PUT.

    Response shape: `{type, typeSkillBasedApp, projectedExpiry,
    expiry, remainingTimeInSeconds}`. Request shape:
    `{type}` or `{type, durationInSeconds}`. Sending the
    response shape unmodified results in HTTP 422.
    """
    if not captured_termination or not isinstance(captured_termination, dict):
        return {"type": "MANUAL"}

    t_type = captured_termination.get("type") or "MANUAL"
    sanitized: dict[str, Any] = {"type": t_type}

    if t_type == "TIMER":
        # Request uses `durationInSeconds`; response uses
        # `remainingTimeInSeconds` (countdown). Prefer the
        # explicit request key when both are present.
        duration = (
            captured_termination.get("durationInSeconds")
            or captured_termination.get("remainingTimeInSeconds")
        )
        if duration:
            try:
                sanitized["durationInSeconds"] = int(duration)
            except (TypeError, ValueError):
                pass

    return sanitized


def _parse_time_period(time_period: Any) -> int:
    """Convert `timedelta` or `HH:MM:SS` to a duration in minutes (1–1440)."""
    from datetime import timedelta

    if isinstance(time_period, timedelta):
        duration_minutes = int(time_period.total_seconds() / 60)
    else:
        time_parts = str(time_period).split(":")
        if len(time_parts) != TIMER_PARTS_COUNT:
            msg = f"Invalid time_period format: {time_period}. Expected HH:MM:SS"
            raise ValueError(msg)

        hours = int(time_parts[0])
        minutes = int(time_parts[1])
        seconds = int(time_parts[2])

        if not (0 <= hours <= MAX_TIMER_HOURS):
            msg = f"Hours must be 0-24, got {hours}"
            raise ValueError(msg)
        if not (0 <= minutes <= MAX_TIMER_MINUTES):
            msg = f"Minutes must be 0-59, got {minutes}"
            raise ValueError(msg)
        if not (0 <= seconds <= MAX_TIMER_SECONDS):
            msg = f"Seconds must be 0-59, got {seconds}"
            raise ValueError(msg)

        duration_minutes = hours * 60 + minutes + (seconds // 60)

    if duration_minutes < 1:
        msg = f"Duration must be at least 1 minute, got {duration_minutes}"
        raise ValueError(msg)
    if duration_minutes > MAX_TIMER_DURATION_MINUTES:
        msg = f"Duration must be at most 1440 minutes (24 hours), got {duration_minutes}"
        raise ValueError(msg)

    return duration_minutes


async def _check_bootstrap_reserve(hass: HomeAssistant, entity_ids: list[str]) -> TadoDataUpdateCoordinator | None:
    """Verify the bootstrap-reserve quota is not critically low before a service write."""
    if not entity_ids:
        return None
    try:
        _coord = _resolve_coordinator(hass, entity_ids[0])
        should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
        if should_block:
            await _ratelimit.async_show_api_limit_notification(hass, reason)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_quota_critically_low",
            )
        return _coord
    except HomeAssistantError:
        raise
    except (OSError, ValueError, KeyError) as err:
        _LOGGER.warning(
            "Services: API quota reserve check failed (%s), letting "
            "the call through without the safety check",
            err,
        )
    return None


def _validate_timer_params(
    call: ServiceCall,
) -> tuple[float, int | None, str | None]:
    """Parse + validate timer params for `set_climate_timer` / `set_water_heater_timer`."""
    temperature = call.data.get("temperature")
    time_period = call.data.get("time_period")
    overlay = call.data.get("overlay")

    duration_minutes = None
    if time_period:
        try:
            duration_minutes = _parse_time_period(time_period)
            _LOGGER.debug(
                "Services: parsed time_period %s → %s minute(s)",
                time_period, duration_minutes,
            )
        except (ValueError, AttributeError, TypeError) as e:
            error_msg = f"Failed to parse time_period: {e}"
            _LOGGER.warning(
                "Services: time_period parse failed: %s",
                error_msg,
                exc_info=True,
            )
            raise vol.Invalid(error_msg) from e
    elif not overlay:
        error_msg = "Either time_period or overlay is required"
        _LOGGER.warning("Services: %s", error_msg)
        raise vol.Invalid(error_msg)

    if temperature is None:
        error_msg = "temperature is required"
        _LOGGER.warning("Services: %s", error_msg)
        raise vol.Invalid(error_msg)

    return temperature, duration_minutes, overlay


async def _execute_timer_on_entity(
    hass: HomeAssistant,
    coord: TadoDataUpdateCoordinator | None,
    entity_id: str,
    domain: str,
    temperature: float,
    duration_minutes: int | None,
    overlay: str | None,
) -> bool:
    """Run `async_set_timer` for one entity; the entity captures state itself on success.

    Returns False when the entity lookup fails or when the API
    write failed (the entity swallowed a timeout / HTTP error).
    Network exceptions and `HomeAssistantError` propagate so
    the whole group call aborts.
    """
    ent = _find_entity_by_id(hass, domain, entity_id)
    if not ent or not hasattr(ent, "async_set_timer"):
        return False
    success = await ent.async_set_timer(temperature, duration_minutes, overlay)
    if success:
        if duration_minutes:
            _LOGGER.debug(
                "Services: set timer on %s: %s°C for %s minute(s)",
                entity_id, temperature, duration_minutes,
            )
        elif overlay:
            _LOGGER.debug(
                "Services: set timer on %s: %s°C, overlay=%s",
                entity_id, temperature, overlay,
            )
    return bool(success)


async def handle_set_climate_timer(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `set_climate_timer`: apply a timed overlay across one or more zones.

    Group-friendly: every `group.*` entity in `entity_id`
    expands to its members. Partial failures warn; total
    failure raises a translated `HomeAssistantError`.
    """
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["climate"])

    coord = await _check_bootstrap_reserve(hass, entity_ids)
    temperature, duration_minutes, overlay = _validate_timer_params(call)

    # Group partial-failure pattern: 0 succeed → raise, partial
    # → warn. Network errors propagate so the whole group aborts.
    failures: list[tuple[str, str]] = []  # (entity_id, reason)
    success_count = 0
    total = 0
    for entity_id in entity_ids:
        if not hass.states.get(entity_id):
            continue
        total += 1
        try:
            if await _execute_timer_on_entity(
                hass, coord, entity_id, "climate", temperature, duration_minutes, overlay,
            ):
                success_count += 1
            else:
                # `async_set_timer` returned False: the entity
                # swallowed an API timeout / error. Record so the
                # group-level raise surfaces it to the UI.
                failures.append((entity_id, "API call failed"))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            _LOGGER.warning(
                "Services: set_climate_timer failed for %s (%s), "
                "the rest of the group will continue",
                entity_id, e,
            )
            failures.append((entity_id, str(e)))

    if total > 0 and success_count == 0:
        _raise_service_error(
            "timer_set_failed_all",
            entity_count=total,
            reasons=", ".join(f"{eid}: {reason}" for eid, reason in failures[:3]),
        )
    elif failures:
        _LOGGER.warning(
            "Services: set_climate_timer succeeded on %d of %d "
            "zone(s); failed for %s",
            success_count, total,
            ", ".join(eid for eid, _ in failures),
        )


def _notify_trusted_target_write(
    hass: HomeAssistant,
    coord: TadoDataUpdateCoordinator | None,
    entity_id: str,
    temperature: float,
    force_override: bool,
) -> None:
    """Tell a zone's valve controller (if any) to adopt a trusted target.

    No-op when the zone has no valve controller (SVC off / offset_sync mode):
    the overlay was still written, it just behaves as a plain timer write.
    """
    if coord is None:
        return
    ent = _find_entity_by_id(hass, "climate", entity_id)
    zone_id = getattr(ent, "zone_id", None) if ent else None
    if zone_id and zone_id in coord.valve_controllers:
        coord.valve_controllers[zone_id].on_trusted_target_write(
            temperature, force_override,
        )


async def handle_set_schedule_temperature(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `set_schedule_temperature`: set a zone target that SVC treats as a trusted write.

    Group-friendly: every `group.*` entity expands to its members. Writes the
    overlay through the same path as set_climate_timer (no hand-rolled payload),
    then tells any valve controller on the zone to adopt the new target so Smart
    Valve Control keeps compensating toward it instead of backing off.
    """
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["climate"])

    coord = await _check_bootstrap_reserve(hass, entity_ids)
    temperature = call.data["temperature"]
    overlay = call.data.get("overlay", "next_time_block")
    force_override = call.data.get("force_override", False)

    # Group partial-failure pattern: 0 succeed → raise, partial → warn.
    failures: list[tuple[str, str]] = []
    success_count = 0
    total = 0
    for entity_id in entity_ids:
        if not hass.states.get(entity_id):
            continue
        total += 1
        try:
            if await _execute_timer_on_entity(
                hass, coord, entity_id, "climate", temperature,
                duration_minutes=None, overlay=overlay,
            ):
                success_count += 1
                _notify_trusted_target_write(
                    hass, coord, entity_id, temperature, force_override,
                )
            else:
                failures.append((entity_id, "API call failed"))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            _LOGGER.warning(
                "Services: set_schedule_temperature failed for %s (%s), "
                "the rest of the group will continue",
                entity_id, e,
            )
            failures.append((entity_id, str(e)))

    if total > 0 and success_count == 0:
        _raise_service_error(
            "timer_set_failed_all",
            entity_count=total,
            reasons=", ".join(f"{eid}: {reason}" for eid, reason in failures[:3]),
        )
    elif failures:
        _LOGGER.warning(
            "Services: set_schedule_temperature succeeded on %d of %d "
            "zone(s); failed for %s",
            success_count, total,
            ", ".join(eid for eid, _ in failures),
        )


def _validate_water_heater_timer_params(
    call: ServiceCall,
) -> tuple[int, float | None]:
    """Parse + validate `set_water_heater_timer` params into `(duration_minutes, temperature)`."""
    time_period = call.data.get("time_period")
    temperature = call.data.get("temperature")

    if not time_period:
        error_msg = "time_period is required for set_water_heater_timer service"
        _LOGGER.warning("Services: %s", error_msg)
        raise vol.Invalid(error_msg)

    try:
        duration_minutes = _parse_time_period(time_period)
        _LOGGER.debug(
            "Services: parsed time_period %s → %s minute(s)",
            time_period, duration_minutes,
        )
    except (ValueError, AttributeError, TypeError) as e:
        error_msg = f"Failed to parse time_period: {e}"
        _LOGGER.warning(
            "Services: time_period parse failed: %s",
            error_msg,
            exc_info=True,
        )
        raise vol.Invalid(error_msg) from e

    if temperature is not None:
        if not (WATER_HEATER_MIN_TEMP <= temperature <= WATER_HEATER_MAX_TEMP):
            error_msg = f"Temperature must be 30-80°C, got {temperature}"
            _LOGGER.warning("Services: %s", error_msg)
            raise vol.Invalid(error_msg)

    return duration_minutes, temperature


async def handle_set_water_heater_timer(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `set_water_heater_timer`: apply a timed overlay across one or more DHW zones."""
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["water_heater"])

    await _check_bootstrap_reserve(hass, entity_ids)
    duration_minutes, temperature = _validate_water_heater_timer_params(call)

    failures: list[tuple[str, str]] = []
    success_count = 0
    total = 0
    for entity_id in entity_ids:
        ent = _find_entity_by_id(hass, "water_heater", entity_id)
        if not ent or not hasattr(ent, "async_set_timer"):
            continue
        total += 1
        try:
            success = await ent.async_set_timer(duration_minutes, temperature)
            if success:
                _LOGGER.debug(
                    "Services: set water heater timer on %s: %s "
                    "minute(s)",
                    entity_id, duration_minutes,
                )
                success_count += 1
            else:
                failures.append((entity_id, "API call failed"))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            _LOGGER.warning(
                "Services: set_water_heater_timer failed for %s "
                "(%s), the rest of the group will continue",
                entity_id, e,
            )
            failures.append((entity_id, str(e)))

    if total > 0 and success_count == 0:
        _raise_service_error(
            "timer_set_failed_all",
            entity_count=total,
            reasons=", ".join(f"{eid}: {reason}" for eid, reason in failures[:3]),
        )
    elif failures:
        _LOGGER.warning(
            "Services: set_water_heater_timer succeeded on %d of "
            "%d zone(s); failed for %s",
            success_count, total,
            ", ".join(eid for eid, _ in failures),
        )


async def handle_turn_off_all_zones(hass: HomeAssistant, call: ServiceCall) -> None:
    """Place every climate zone (heating + AC) into a MANUAL OFF overlay.

    Mirrors the Tado app's "Turn OFF all rooms"; schedules stay
    suppressed until the user resumes each zone (or calls
    `tado_ce.resume_schedule`). Hot water is out of scope.

    Multi-home installs raise a translated `multiple_entries` error so
    the caller picks one home (same as `set_away_config`).

    Payload is the canonical OFF shape `{"type": "HEATING" |
    "AIR_CONDITIONING", "power": "OFF"}` with a fresh
    `{"type": "MANUAL"}` termination. Deliberately skips capture-state:
    the override is permanent-until-resumed, not a restorable temporary one.
    """
    coord = _resolve_single_coordinator(hass)

    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(
        hass, coordinator=coord,
    )
    if should_block:
        await _ratelimit.async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )

    zones_info = coord.data_loader.get_cached("zones_info")
    if not zones_info or not isinstance(zones_info, list):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="turn_off_all_no_climate_zones",
        )

    target_zones: list[tuple[str, str]] = [
        (str(zone["id"]), zone["type"])
        for zone in zones_info
        if is_climate_zone(zone.get("type") or "")
    ]
    if not target_zones:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="turn_off_all_no_climate_zones",
        )

    failures: list[tuple[str, str]] = []
    success_count = 0
    for zone_id, zone_type in target_zones:
        ok = await run_service_call(
            hass=hass,
            coordinator=coord,
            zone_id=zone_id,
            entity_type="climate",
            api_coro=coord.api_client.set_zone_overlay(
                zone_id,
                {"type": zone_type, "power": "OFF"},
                {"type": "MANUAL"},
            ),
            capture_source="set_hvac_mode",
            refresh_entity_id=None,
            reason="turn_off_all_zones",
        )
        if ok:
            success_count += 1
        else:
            failures.append((zone_id, "cloud rejected the call"))

    if success_count == 0:
        _raise_service_error(
            "turn_off_all_failed_all",
            reasons=", ".join(f"zone {z}: {r}" for z, r in failures[:3]),
        )
    elif failures:
        _LOGGER.warning(
            "Services: turn_off_all_zones succeeded on %d of %d zone(s); "
            "failed for %s",
            success_count, len(target_zones),
            ", ".join(z for z, _ in failures),
        )

    # One coordinator-wide refresh picks up the new state for every
    # zone in a single poll, regardless of how many succeeded.
    await coord.async_request_refresh()


async def handle_resume_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `resume_schedule`: clear the overlay on one or more zones."""
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]

    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["climate", "water_heater"])

    failures: list[str] = []
    for entity_id in entity_ids:
        try:
            _coord = _resolve_coordinator(hass, entity_id)
        except HomeAssistantError:
            _LOGGER.warning(
                "Services: resume_schedule could not resolve "
                "coordinator for %s, skipping this entity",
                entity_id,
                exc_info=True,
            )
            continue

        should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
        if should_block:
            await _ratelimit.async_show_api_limit_notification(hass, reason)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_quota_critically_low",
            )

        domain = entity_id.split(".")[0]
        ent = _find_entity_by_id(hass, domain, entity_id)
        if ent:
            zone_id = ent.zone_id  # type: ignore[attr-defined]
            if zone_id:
                # A zone with no active overlay is already on its schedule,
                # so resuming would be a wasted cloud call, skip it.
                if ResumeGuard.should_skip_resume(_coord, zone_id):
                    _LOGGER.debug(
                        "Services: resume_schedule, zone %s already on "
                        "schedule, skipping redundant cloud call",
                        zone_id,
                    )
                    continue
                api_success = await run_service_call(
                    hass=hass,
                    coordinator=_coord,
                    zone_id=zone_id,
                    entity_type=domain,
                    api_coro=_coord.api_client.delete_zone_overlay(zone_id),
                    capture_source=None,
                    refresh_entity_id=entity_id,
                    reason="resume_schedule",
                )
                if not api_success:
                    _LOGGER.warning(
                        "Services: resume_schedule failed for %s, "
                        "the cloud rejected the call, the zone "
                        "remains on its overlay",
                        entity_id,
                    )
                    failures.append(entity_id)

    if failures:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="resume_schedule_failed",
            translation_placeholders={"entity_id": ", ".join(failures)},
        )


async def handle_set_temp_offset(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `set_temperature_offset`: write the offset to every TRV in the zone.

    Multi-TRV zones get the same offset on every device. The
    cache value is read back from the cloud (rather than reusing
    the user's input) to avoid feedback loops where automations
    react to their own write.
    """
    entity_id = call.data.get("entity_id")
    offset = call.data.get("offset")

    _coord = _resolve_coordinator(hass, entity_id)  # type: ignore[arg-type]

    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
    if should_block:
        await _ratelimit.async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )

    ent = _find_entity_by_id(hass, "climate", entity_id)  # type: ignore[arg-type]
    if ent:
        zone_id = ent.zone_id  # type: ignore[attr-defined]
        if zone_id:
            # Find ALL device serials for this zone (multi-TRV support)
            serials = await hass.async_add_executor_job(
                _get_zone_device_serials,
                zone_id,
                _coord.data_loader,
            )
            if serials:
                # Track per-device write success so we can
                # distinguish total / partial / clean cases.
                success_count = 0
                for serial in serials:
                    if await run_service_call(
                        hass=hass,
                        coordinator=_coord,
                        zone_id=zone_id,
                        entity_type="climate",
                        api_coro=_coord.api_client.set_device_offset(serial, offset),  # type: ignore[arg-type]
                        capture_source=None,
                        refresh_entity_id=None,
                        reason="set_temp_offset",
                    ):
                        success_count += 1

                if success_count == 0:
                    # Don't update the cache or notify the sync
                    # controller, the user must see the failure
                    # so they can retry or investigate.
                    _LOGGER.warning(
                        "Services: set_temperature_offset failed on "
                        "every device in %s (%d device(s)), cache "
                        "left untouched, raising error to the UI",
                        entity_id, len(serials),
                    )
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="offset_write_failed",
                        translation_placeholders={
                            "entity_id": str(entity_id),
                            "device_count": str(len(serials)),
                        },
                    )

                _LOGGER.info(
                    "Services: set offset %s°C for %s "
                    "(%d/%d device(s))",
                    offset, entity_id, success_count, len(serials),
                )
                if success_count < len(serials):
                    _LOGGER.warning(
                        "Services: partial offset write for %s. "
                        "%d of %d device(s) failed, cache will "
                        "reflect the readback from the first "
                        "successful device",
                        entity_id,
                        len(serials) - success_count,
                        len(serials),
                    )

                # Read the offset back from the cloud rather than
                # caching the user's input, that breaks the
                # automation feedback loop where `offset_celsius`
                # reflects the automation's own last write.
                readback = await _coord.api_client.get_device_offset(serials[0])
                cache_value: float | None = readback if readback is not None else offset

                from .const import is_valid_device_offset

                if not is_valid_device_offset(cache_value):
                    _LOGGER.warning(
                        "Services: offset readback for zone %s was "
                        "%s°C, outside the valid range, ignoring "
                        "this readback and keeping the previous "
                        "cached value",
                        zone_id, cache_value,
                    )
                else:
                    cached_offsets = _coord.data_loader.get_cached("offsets")
                    if cached_offsets is None:
                        cached_offsets = {}
                    cached_offsets[zone_id] = cache_value
                    _coord.data_loader.update_cache("offsets", cached_offsets)
                    await _coord.data_loader.async_update_store("offsets", cached_offsets)

                    if _coord.data and isinstance(_coord.data, dict):
                        _coord.data["offsets"] = cached_offsets

                    _LOGGER.debug(
                        "Services: offsets cache updated for zone "
                        "%s: %s°C (readback)",
                        zone_id, cache_value,
                    )

                # Notify the offset-sync controller BEFORE the
                # refresh, otherwise the controller could fire an
                # evaluation against the freshly-cached value and
                # write a counter-correction that fights the
                # service caller.
                if zone_id in _coord.offset_sync_controllers:
                    _coord.offset_sync_controllers[zone_id].on_external_offset_write()

                # Smart Valve Control already compensates inside
                # the controller; a non-zero device offset stacks
                # on top and causes double compensation because
                # the TRV's reported temperature is itself
                # offset-adjusted.
                if (
                    zone_id in _coord.valve_controllers
                    and offset is not None
                    and abs(float(offset)) >= 0.1
                ):
                    _LOGGER.warning(
                        "Services: Smart Valve Control is active "
                        "for zone %s and the device offset was just "
                        "set to %.1f°C, this will cause double "
                        "compensation, reset the offset to 0°C for "
                        "accurate SVC tracking",
                        zone_id, float(offset),
                    )

                await _coord.async_request_refresh()
            else:
                _LOGGER.warning(
                    "Services: no devices found for %s, "
                    "set_temperature_offset cannot proceed",
                    entity_id,
                )


async def handle_add_meter_reading(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `add_meter_reading`: submit a meter reading to the cloud."""
    _coord = _resolve_single_coordinator(hass)

    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
    if should_block:
        await _ratelimit.async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )

    reading = call.data.get("reading")
    date = call.data.get("date")

    success = await run_service_call(
        hass=hass,
        coordinator=_coord,
        zone_id="",
        entity_type="meter_reading",
        api_coro=_coord.api_client.add_meter_reading(reading, date),  # type: ignore[arg-type]
        capture_source=None,
        refresh_entity_id=None,
        reason="add_meter_reading",
    )

    if not success:
        _LOGGER.warning(
            "Services: add_meter_reading failed (reading=%s), "
            "the cloud rejected the submission, please retry",
            reading,
        )


async def handle_refresh(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `refresh`: force an immediate fetch of one floored data type."""
    fetch_type = str(call.data["data"])
    coord = _resolve_single_coordinator(hass)
    cm = coord.config_manager

    if fetch_type == FORCEABLE_FETCH_ZONE_STATES:
        # Zone states are core polling data, not an optional feature, so
        # there's no toggle to gate on here.
        coord.request_forced_zone_fetch()
        await coord.async_request_refresh()
        return

    if fetch_type == FORCEABLE_FETCH_MOBILE:
        enabled = cm.get_mobile_devices_enabled() and cm.get_mobile_devices_frequent_sync()
    elif fetch_type == FORCEABLE_FETCH_HOME_STATE:
        enabled = cm.get_home_state_sync_enabled()
    else:
        enabled = False

    if not enabled:
        _LOGGER.warning(
            "Services: refresh(%s) requested but the feature is off, ignoring",
            fetch_type,
        )
        return

    coord.request_forced_fetch(fetch_type)
    await coord.async_request_refresh()


async def handle_identify_device(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `identify_device`: flash the LED on a device by serial."""
    device_serial = call.data.get("device_serial")

    _coord = _resolve_coordinator_for_device(hass, device_serial)  # type: ignore[arg-type]

    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
    if should_block:
        await _ratelimit.async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )

    success = await run_service_call(
        hass=hass,
        coordinator=_coord,
        zone_id="",
        entity_type="device",
        api_coro=_coord.api_client.identify_device(device_serial),  # type: ignore[arg-type]
        capture_source=None,
        refresh_entity_id=None,
        reason="identify_device",
    )

    if not success:
        _LOGGER.warning(
            "Services: identify_device failed for %s, the cloud "
            "did not acknowledge the request, please retry",
            mask_serial(device_serial or ""),
        )


async def handle_set_away_config(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `set_away_configuration`: configure away-mode behaviour for a zone."""
    entity_id = call.data.get("entity_id")
    mode = call.data.get("mode")
    temperature = call.data.get("temperature")
    comfort_level = call.data.get("comfort_level", 50)

    _coord = _resolve_coordinator(hass, entity_id)  # type: ignore[arg-type]

    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
    if should_block:
        await _ratelimit.async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )

    ent = _find_entity_by_id(hass, "climate", entity_id)  # type: ignore[arg-type]
    if ent:
        zone_id = ent.zone_id  # type: ignore[attr-defined]
        if zone_id:
            success = await run_service_call(
                hass=hass,
                coordinator=_coord,
                zone_id=zone_id,
                entity_type="climate",
                api_coro=_coord.api_client.set_away_configuration(
                    zone_id,
                    mode,  # type: ignore[arg-type]
                    temperature,
                    comfort_level,
                ),
                capture_source=None,
                refresh_entity_id=entity_id,
                reason="set_away_config",
            )
            if not success:
                _LOGGER.warning(
                    "Services: set_away_configuration failed for "
                    "%s, the cloud rejected the call, please retry",
                    entity_id,
                )


async def handle_activate_open_window(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `activate_open_window`: same effect as tapping the icon in the Tado app."""
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]

    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["climate"])

    for entity_id in entity_ids:
        try:
            _coord = _resolve_coordinator(hass, entity_id)
        except HomeAssistantError:
            _LOGGER.warning(
                "Services: activate_open_window could not resolve "
                "coordinator for %s, skipping this entity",
                entity_id,
                exc_info=True,
            )
            continue

        should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
        if should_block:
            await _ratelimit.async_show_api_limit_notification(hass, reason)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_quota_critically_low",
            )

        ent = _find_entity_by_id(hass, "climate", entity_id)
        if ent:
            zone_id = ent.zone_id  # type: ignore[attr-defined]
            if zone_id:
                success = await run_service_call(
                    hass=hass,
                    coordinator=_coord,
                    zone_id=zone_id,
                    entity_type="climate",
                    api_coro=_coord.api_client.activate_open_window(zone_id),
                    capture_source=None,
                    refresh_entity_id=entity_id,
                    reason="activate_open_window",
                )
                if success:
                    _LOGGER.debug(
                        "Services: activated open window for %s",
                        entity_id,
                    )
                else:
                    _LOGGER.warning(
                        "Services: activate_open_window failed for "
                        "%s, the cloud rejected the call, the zone "
                        "remains in its current state",
                        entity_id,
                    )


async def handle_deactivate_open_window(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `deactivate_open_window`: clear open-window mode and resume heating."""
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]

    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["climate"])

    for entity_id in entity_ids:
        try:
            _coord = _resolve_coordinator(hass, entity_id)
        except HomeAssistantError:
            _LOGGER.warning(
                "Services: deactivate_open_window could not resolve "
                "coordinator for %s, skipping this entity",
                entity_id,
                exc_info=True,
            )
            continue

        should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
        if should_block:
            await _ratelimit.async_show_api_limit_notification(hass, reason)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_quota_critically_low",
            )

        ent = _find_entity_by_id(hass, "climate", entity_id)
        if ent:
            zone_id = ent.zone_id  # type: ignore[attr-defined]
            if zone_id:
                success = await run_service_call(
                    hass=hass,
                    coordinator=_coord,
                    zone_id=zone_id,
                    entity_type="climate",
                    api_coro=_coord.api_client.deactivate_open_window(zone_id),
                    capture_source=None,
                    refresh_entity_id=entity_id,
                    reason="deactivate_open_window",
                )
                if success:
                    _LOGGER.debug(
                        "Services: deactivated open window for %s",
                        entity_id,
                    )
                else:
                    _LOGGER.warning(
                        "Services: deactivate_open_window failed "
                        "for %s, the cloud rejected the call, the "
                        "zone remains in open-window mode",
                        entity_id,
                    )


def _resolve_open_window_timeout(
    coord: TadoDataUpdateCoordinator, zone_id: str, user_duration: int | None,
) -> int:
    """Pick the open-window timeout: explicit user param > zone setting > integration default."""
    if user_duration is not None:
        return user_duration
    zones_info = coord.data.get("zones_info") or []
    for zone in zones_info:
        if str(zone.get("id")) == zone_id:
            owd = zone.get("openWindowDetection") or {}
            timeout = owd.get("timeoutInSeconds")
            if timeout is not None:
                return int(timeout)
            break
    return OPEN_WINDOW_DEFAULT_TIMEOUT


def _build_open_window_overlay(
    zone_type: str, timeout: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Build a frost-protection overlay for set_open_window_mode (setting, termination, human duration)."""
    setting: dict[str, str | dict[str, float]] = (
        {"type": "AIR_CONDITIONING", "power": "OFF"}
        if zone_type == "AIR_CONDITIONING"
        else {"type": "HEATING", "power": "ON", "temperature": {"celsius": OPEN_WINDOW_DEFAULT_TEMP}}
    )

    if timeout == 0:
        termination: dict[str, str | int] = build_timer_termination(overlay="manual")
        duration_desc = "indefinite"
    else:
        termination = build_timer_termination(duration_minutes=int(timeout) // 60)
        duration_desc = f"{int(timeout) // 60} min"

    return setting, termination, duration_desc


async def handle_set_open_window_mode(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `set_open_window_mode`: simulate open-window via a frost overlay.

    Differs from `activate_open_window` (which uses Tado's
    detection state machine) by writing an explicit frost
    overlay so users without OpenWindowDetection licenses can
    still trigger the same heating behaviour.
    """
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["climate"])

    duration_seconds = call.data.get("duration")
    capture_state = call.data.get("capture_state", True)

    for entity_id in entity_ids:
        try:
            _coord = _resolve_coordinator(hass, entity_id)
        except HomeAssistantError:
            _LOGGER.warning(
                "Services: set_open_window_mode could not resolve "
                "coordinator for %s, skipping this entity",
                entity_id,
                exc_info=True,
            )
            continue

        should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
        if should_block:
            await _ratelimit.async_show_api_limit_notification(hass, reason)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_quota_critically_low",
            )

        ent = _find_entity_by_id(hass, "climate", entity_id)
        if not ent:
            continue
        zone_id = ent.zone_id  # type: ignore[attr-defined]
        if not zone_id:
            continue

        timeout = _resolve_open_window_timeout(_coord, zone_id, duration_seconds)
        zone_type = ent.zone_type  # type: ignore[attr-defined]
        setting, termination, duration_desc = _build_open_window_overlay(zone_type, timeout)
        entity_type = ent.entity_type  # type: ignore[attr-defined]

        success = await run_service_call(
            hass=hass,
            coordinator=_coord,
            zone_id=zone_id,
            entity_type=entity_type,
            api_coro=_coord.api_client.set_zone_overlay(zone_id, setting, termination),
            capture_source="set_open_window_mode" if capture_state else None,
            refresh_entity_id=entity_id,
            reason="set_open_window_mode",
        )
        if success:
            _LOGGER.info(
                "Services: set open window mode on %s (%s, "
                "%s°C frost protection)",
                entity_id, duration_desc, OPEN_WINDOW_DEFAULT_TEMP,
            )
        else:
            _LOGGER.warning(
                "Services: set_open_window_mode failed for %s, "
                "the cloud rejected the call, the zone remains in "
                "its current state",
                entity_id,
            )


async def handle_get_temp_offset(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `get_temperature_offset`: return the live offset for an automation."""
    entity_id = call.data.get("entity_id")

    try:
        _coord = _resolve_coordinator(hass, entity_id)  # type: ignore[arg-type]
    except HomeAssistantError as e:
        _LOGGER.warning(
            "Services: get_temperature_offset could not resolve "
            "coordinator for %s, returning None to the caller",
            entity_id,
            exc_info=True,
        )
        return {"offset_celsius": None, "error": str(e)}  # type: ignore[return-value]

    ent = _find_entity_by_id(hass, "climate", entity_id)  # type: ignore[arg-type]
    if ent:
        zone_id = ent.zone_id  # type: ignore[attr-defined]
        if zone_id:
            serial = await hass.async_add_executor_job(
                _get_zone_device_serial,
                zone_id,
                _coord.data_loader,
            )
            if serial:
                result = await _coord.api_client.get_device_offset(serial)
                if result is not None:
                    return {"offset_celsius": result}  # type: ignore[return-value]

        _LOGGER.warning(
            "Services: get_temperature_offset could not fetch "
            "offset for %s, returning None to the caller",
            entity_id,
        )
        return {"offset_celsius": None, "error": "Failed to fetch offset"}  # type: ignore[return-value]

    _LOGGER.warning(
        "Services: get_temperature_offset entity not found: %s",
        entity_id,
    )
    return {"offset_celsius": None, "error": "Entity not found"}  # type: ignore[return-value]


async def handle_restore_previous_state(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `restore_previous_state`: undo the last overlay write.

    Falls back to `resume_schedule` when no captured baseline
    exists. Captured state is only cleared after the cloud
    write succeeds, so a transient failure (rate-limit /
    network / cloud rejection) doesn't lose the baseline,
    the user can retry.
    """
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]

    entity_ids = _expand_group_entity_ids(hass, entity_ids, allowed_domains=["climate", "water_heater"])

    failures: list[str] = []
    for entity_id in entity_ids:
        try:
            _coord = _resolve_coordinator(hass, entity_id)
        except HomeAssistantError:
            _LOGGER.warning(
                "Services: restore_previous_state could not resolve "
                "coordinator for %s, skipping this entity",
                entity_id,
                exc_info=True,
            )
            continue

        domain = entity_id.split(".")[0]
        ent = _find_entity_by_id(hass, domain, entity_id)
        if ent is None:
            _LOGGER.warning(
                "Services: restore_previous_state entity not found "
                ": %s, skipping",
                entity_id,
            )
            continue

        zone_id = ent.zone_id  # type: ignore[attr-defined]
        entity_type = ent.entity_type  # type: ignore[attr-defined]
        if not zone_id or not entity_type:
            _LOGGER.warning(
                "Services: restore_previous_state missing zone_id "
                "or entity_type for %s, skipping",
                entity_id,
            )
            continue

        # Peek without consuming: the captured state is only
        # cleared after the cloud write succeeds, so a transient
        # failure leaves the baseline intact for a retry.
        captured = await _coord.async_peek_state(zone_id, entity_type)

        # Both resume branches below call delete_zone_overlay. If the zone is
        # already on schedule (no active overlay), that cloud call is a no-op,
        # so skip it to save quota.
        wants_resume = captured is None or captured.overlay_type is None
        if wants_resume and ResumeGuard.should_skip_resume(_coord, zone_id):
            _LOGGER.debug(
                "Services: restore_previous_state, zone %s already on "
                "schedule, skipping redundant resume call", zone_id,
            )
            continue

        if captured is None:
            api_success = await run_service_call(
                hass=hass,
                coordinator=_coord,
                zone_id=zone_id,
                entity_type=entity_type,
                api_coro=_coord.api_client.delete_zone_overlay(zone_id),
                capture_source=None,
                refresh_entity_id=entity_id,
                reason="restore_previous_state",
            )
            if api_success:
                _LOGGER.info(
                    "Services: restore_previous_state, no baseline "
                    "for %s, resumed schedule instead",
                    entity_id,
                )
        elif captured.overlay_type is None:
            api_success = await run_service_call(
                hass=hass,
                coordinator=_coord,
                zone_id=zone_id,
                entity_type=entity_type,
                api_coro=_coord.api_client.delete_zone_overlay(zone_id),
                capture_source=None,
                refresh_entity_id=entity_id,
                reason="restore_previous_state",
            )
            if api_success:
                _LOGGER.info(
                    "Services: restore_previous_state, restored "
                    "schedule for %s",
                    entity_id,
                )
        else:
            setting, termination = _build_setting_from_captured(captured)
            api_success = await run_service_call(
                hass=hass,
                coordinator=_coord,
                zone_id=zone_id,
                entity_type=entity_type,
                api_coro=_coord.api_client.set_zone_overlay(zone_id, setting, termination),
                capture_source=None,
                refresh_entity_id=entity_id,
                reason="restore_previous_state",
            )
            if api_success:
                _LOGGER.info(
                    "Services: restore_previous_state, restored "
                    "overlay for %s (type %s, %s°C)",
                    entity_id,
                    captured.overlay_type,
                    captured.temperature,
                )

        if api_success:
            if captured is not None:
                await _coord.async_clear_captured_state(zone_id, entity_type)
            await async_trigger_immediate_refresh(hass, entity_id, "restore_previous_state")
        else:
            _LOGGER.warning(
                "Services: restore_previous_state failed for %s, "
                "the cloud rejected the call, captured baseline "
                "preserved so the user can retry",
                entity_id,
            )
            failures.append(entity_id)

    if failures:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="state_restore_failed",
            translation_placeholders={"entity_id": ", ".join(failures)},
        )


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register every Tado CE service handler with HA's service registry."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_CLIMATE_TIMER):
        _LOGGER.debug(
            "Services: registration already complete, skipping",
        )
        return

    # `cv.entity_ids` + handler-side expansion is what lets every
    # service support `group.*` entities transparently.
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CLIMATE_TIMER,
        functools.partial(handle_set_climate_timer, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
                vol.Required("temperature"): vol.All(vol.Coerce(float), vol.Range(min=5, max=30)),
                vol.Optional("time_period"): cv.time_period,
                vol.Optional("overlay"): cv.string,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULE_TEMPERATURE,
        functools.partial(handle_set_schedule_temperature, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
                vol.Required("temperature"): vol.All(vol.Coerce(float), vol.Range(min=5, max=30)),
                vol.Optional("overlay"): cv.string,
                vol.Optional("force_override"): cv.boolean,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_WATER_HEATER_TIMER,
        functools.partial(handle_set_water_heater_timer, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
                vol.Required("time_period"): cv.time_period,
                vol.Optional("temperature"): vol.Coerce(float),
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESUME_SCHEDULE,
        functools.partial(handle_resume_schedule, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_TURN_OFF_ALL_ZONES,
        functools.partial(handle_turn_off_all_zones, hass),
        schema=vol.Schema({}),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TEMP_OFFSET,
        functools.partial(handle_set_temp_offset, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("offset"): vol.All(vol.Coerce(float), vol.Range(min=-10.0, max=10.0)),
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_METER_READING,
        functools.partial(handle_add_meter_reading, hass),
        schema=vol.Schema(
            {
                vol.Required("reading"): vol.Coerce(int),
                vol.Optional("date"): cv.string,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH,
        functools.partial(handle_refresh, hass),
        schema=vol.Schema(
            {
                vol.Required("data"): vol.In(
                    [FORCEABLE_FETCH_MOBILE, FORCEABLE_FETCH_HOME_STATE, FORCEABLE_FETCH_ZONE_STATES],
                ),
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_IDENTIFY_DEVICE,
        functools.partial(handle_identify_device, hass),
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_AWAY_CONFIG,
        functools.partial(handle_set_away_config, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("mode"): cv.string,
                vol.Optional("temperature"): vol.Coerce(float),
                vol.Optional("comfort_level"): vol.Coerce(int),
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ACTIVATE_OPEN_WINDOW,
        functools.partial(handle_activate_open_window, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DEACTIVATE_OPEN_WINDOW,
        functools.partial(handle_deactivate_open_window, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_OPEN_WINDOW_MODE,
        functools.partial(handle_set_open_window_mode, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
                vol.Optional("duration"): vol.All(
                    vol.Coerce(int), vol.Any(0, vol.Range(min=60, max=3600)),
                ),
                vol.Optional("capture_state", default=True): cv.boolean,
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TEMP_OFFSET,
        functools.partial(handle_get_temp_offset, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
            },
        ),
        supports_response=True,  # type: ignore[arg-type]
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_PREVIOUS_STATE,
        functools.partial(handle_restore_previous_state, hass),
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
            },
        ),
    )

    _LOGGER.info("Services: registered all Tado CE service handlers")
