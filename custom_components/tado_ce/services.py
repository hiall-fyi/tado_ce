"""Tado CE custom HA services: climate timer, water heater timer, offsets, open window, restore."""

from __future__ import annotations

from datetime import timedelta
import functools
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.service import async_register_platform_entity_service
import probatio as vol

from . import ratelimit as _ratelimit
from .climate_base import cancel_zone_tap
from .const import (
    DAY_TYPES,
    DEVICE_OFFSET_MAX,
    DEVICE_OFFSET_MIN,
    DOMAIN,
    FORCEABLE_FETCH_HOME_STATE,
    FORCEABLE_FETCH_MOBILE,
    FORCEABLE_FETCH_ZONE_STATES,
    OPEN_WINDOW_DEFAULT_TEMP,
    OPEN_WINDOW_DEFAULT_TIMEOUT,
    SERVICE_ACTIVATE_CLIMATE_OPEN_WINDOW,
    SERVICE_ADD_METER_READING,
    SERVICE_DEACTIVATE_CLIMATE_OPEN_WINDOW,
    SERVICE_GET_CLIMATE_TEMPERATURE_OFFSET,
    SERVICE_GET_FLOW_TEMPERATURE_SETTINGS,
    SERVICE_IDENTIFY_DEVICE,
    SERVICE_REFRESH,
    SERVICE_RESTORE_CLIMATE_PREVIOUS_STATE,
    SERVICE_RESTORE_WATER_HEATER_PREVIOUS_STATE,
    SERVICE_RESUME_CLIMATE_SCHEDULE,
    SERVICE_RESUME_WATER_HEATER_SCHEDULE,
    SERVICE_SET_CLIMATE_ACTIVE_TIMETABLE,
    SERVICE_SET_CLIMATE_AWAY_CONFIGURATION,
    SERVICE_SET_CLIMATE_OPEN_WINDOW_MODE,
    SERVICE_SET_CLIMATE_SCHEDULE_TEMPERATURE,
    SERVICE_SET_CLIMATE_TIMER,
    SERVICE_SET_TEMP_OFFSET,
    SERVICE_SET_WATER_HEATER_TIMER,
    SERVICE_TURN_OFF_ALL_CLIMATE_ZONES,
    TIMER_DURATION_MAX,
    TIMER_DURATION_MIN,
    is_climate_zone,
)
from .error_dispatch import dispatch_to_service_call, handle_background_write_error, is_typed_cloud_rejection
from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError, TadoSyncError
from .helpers import async_trigger_immediate_refresh, build_timer_termination, live_zones, mask_serial
from .schedule_helpers import async_refresh_zone_schedule
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
MAX_TIMER_HOURS = TIMER_DURATION_MAX // 60
MAX_TIMER_MINUTES = 59
MAX_TIMER_SECONDS = 59

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
    """Return every device serial in a zone, used for multi-TRV writes (e.g. set_climate_temperature_offset)."""
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


def _resolve_coordinator(hass: HomeAssistant, entity_id: str) -> TadoDataUpdateCoordinator:
    """Resolve which Tado CE coordinator owns this entity_id.

    Raises a translated `ServiceValidationError` when the entity
    isn't ours, or a `HomeAssistantError` when its config entry isn't loaded; those errors
    surface in the HA UI.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)

    if entity_entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entity_not_found",
            translation_placeholders={"entity_id": entity_id},
        )

    if entity_entry.platform != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entity_not_tado_ce",
            translation_placeholders={"entity_id": entity_id, "platform": entity_entry.platform},
        )

    config_entry_id = entity_entry.config_entry_id
    if config_entry_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entity_config_entry_not_found",
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

    for device in device_registry.devices:
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier == device_serial:
                config_entry_id = device.config_entry_id
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
                    translation_placeholders={"device_serial": mask_serial(device_serial)},
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

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="device_not_found",
        translation_placeholders={"device_serial": mask_serial(device_serial)},
    )


def _resolve_single_coordinator(hass: HomeAssistant) -> TadoDataUpdateCoordinator:
    """Pick the only loaded Tado CE coordinator, or raise when ambiguous."""
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
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="multiple_entries",
        translation_placeholders={"home_ids": ", ".join(mask_home_id(h) for h in home_ids)},
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
                _LOGGER.debug("services: non-numeric captured termination duration %r, dropping", duration)

    return sanitized


def _time_period_text(value: Any) -> timedelta:
    if isinstance(value, (bool, int, float)) or (isinstance(value, str) and ":" not in value):
        msg = 'Write the value as hours:minutes:seconds with colons, for example "00:30:00". In YAML put it in quotes. A bare number is ambiguous.'
        raise vol.Invalid(msg)
    period: timedelta = cv.time_period(value)
    return period


def _open_window_period(value: timedelta) -> timedelta:
    if value != timedelta(0) and not timedelta(seconds=60) <= value <= timedelta(seconds=3600):
        msg = "Use 00:00:00 for until resumed, or a time period from 00:01:00 to 01:00:00"
        raise vol.Invalid(msg)
    return value


def _parse_time_period(time_period: Any) -> int:
    """Convert `timedelta` or `HH:MM:SS` to a duration in minutes (1–720)."""
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
            msg = f"Hours must be 0-{MAX_TIMER_HOURS}, got {hours}"
            raise ValueError(msg)
        if not (0 <= minutes <= MAX_TIMER_MINUTES):
            msg = f"Minutes must be 0-59, got {minutes}"
            raise ValueError(msg)
        if not (0 <= seconds <= MAX_TIMER_SECONDS):
            msg = f"Seconds must be 0-59, got {seconds}"
            raise ValueError(msg)

        duration_minutes = hours * 60 + minutes + (seconds // 60)

    if duration_minutes < TIMER_DURATION_MIN:
        msg = f"Duration must be at least {TIMER_DURATION_MIN} minute, got {duration_minutes}"
        raise ValueError(msg)
    if duration_minutes > TIMER_DURATION_MAX:
        msg = f"Duration must be at most {TIMER_DURATION_MAX} minutes ({TIMER_DURATION_MAX // 60} hours), got {duration_minutes}"
        raise ValueError(msg)

    return duration_minutes


async def _check_bootstrap_reserve(
    hass: HomeAssistant, entity_ids: list[str], cost: int = 1,
) -> TadoDataUpdateCoordinator | None:
    """Verify the bootstrap-reserve quota is not critically low before a service write of `cost` calls."""
    if not entity_ids:
        return None
    try:
        _coord = _resolve_coordinator(hass, entity_ids[0])
        should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord, cost=cost)
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


def _check_timer_temperature(entity: Any, temperature: float) -> None:
    """Refuse a temperature outside the range of this climate entity."""
    min_temp, max_temp = entity.min_temp, entity.max_temp
    if not min_temp <= temperature <= max_temp:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="temperature_out_of_range",
            translation_placeholders={
                "entity_id": entity.entity_id,
                "temperature": str(temperature),
                "minimum": str(min_temp),
                "maximum": str(max_temp),
            },
        )


def _validate_timer_params(
    call: ServiceCall,
) -> tuple[float, int | None, str | None]:
    """Parse and validate `set_climate_timer` params into `(temperature, duration_minutes, overlay)`."""
    temperature = call.data["temperature"]
    time_period = call.data.get("time_period")
    overlay = call.data.get("overlay")

    duration_minutes = None
    if time_period is not None:
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

    return temperature, duration_minutes, overlay


async def _execute_timer_on_entity(
    entity: Any,
    temperature: float | None,
    duration_minutes: int | None,
    overlay: str | None,
) -> None:
    """Run `async_set_timer` on one zone and raise when the entity reports the write failed."""
    extra = {} if overlay is None else {"overlay": overlay}
    success = await entity.async_set_timer(temperature=temperature, duration_minutes=duration_minutes, **extra)
    if not success:
        _raise_service_error(
            "timer_set_all_zones_failed",
            entity_count=1,
            reasons=f"{entity.entity_id}: API call failed",
        )
    degrees = "" if temperature is None else f": {temperature}°C"
    if duration_minutes:
        _LOGGER.debug(
            "Services: set timer on %s%s for %s minute(s)",
            entity.entity_id, degrees, duration_minutes,
        )
    elif overlay:
        _LOGGER.debug(
            "Services: set timer on %s%s, overlay=%s",
            entity.entity_id, degrees, overlay,
        )


async def handle_set_climate_timer(entity: Any, call: ServiceCall) -> None:
    """Service handler for `set_climate_timer`: a timed overlay on one zone."""
    _check_timer_temperature(entity, call.data["temperature"])
    await _check_bootstrap_reserve(entity.hass, [entity.entity_id], cost=1)
    temperature, duration_minutes, overlay = _validate_timer_params(call)
    await _execute_timer_on_entity(entity, temperature, duration_minutes, overlay)


def _notify_trusted_target_write(entity: Any, temperature: float, force_override: bool) -> None:
    """Tell a zone's valve controller (if any) to adopt a trusted target."""
    controllers = entity.coordinator.valve_controllers
    if entity.zone_id in controllers:
        controllers[entity.zone_id].on_trusted_target_write(temperature, force_override)


async def handle_set_schedule_temperature(entity: Any, call: ServiceCall) -> None:
    """Service handler for `set_climate_schedule_temperature`: set a zone target that SVC treats as a trusted write."""
    temperature = call.data["temperature"]
    _check_timer_temperature(entity, temperature)
    await _check_bootstrap_reserve(entity.hass, [entity.entity_id], cost=1)
    overlay = call.data.get("overlay", "next_time_block")
    await _execute_timer_on_entity(entity, temperature, None, overlay)
    _notify_trusted_target_write(entity, temperature, call.data.get("force_override", False))


def _validate_water_heater_timer_params(
    call: ServiceCall,
) -> tuple[int, float | None]:
    """Parse + validate `set_water_heater_timer` params into `(duration_minutes, temperature)`."""
    time_period = call.data.get("time_period")
    temperature = call.data.get("temperature")

    if time_period is None:
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


async def handle_set_water_heater_timer(entity: Any, call: ServiceCall) -> None:
    """Service handler for `set_water_heater_timer`: a timed overlay on one hot water zone."""
    duration_minutes, temperature = _validate_water_heater_timer_params(call)
    await _execute_timer_on_entity(entity, temperature, duration_minutes, None)


async def handle_turn_off_all_zones(hass: HomeAssistant, call: ServiceCall) -> None:
    """Place every climate zone (heating + AC) into a MANUAL OFF overlay.

    Mirrors the Tado app's "Turn OFF all rooms"; schedules stay
    suppressed until the user resumes each zone (or calls
    `tado_ce.resume_climate_schedule`). Hot water is out of scope.

    Multi-home installs raise a translated `multiple_entries` error so
    the caller picks one home (same as `set_climate_away_configuration`).

    Payload is the canonical OFF shape `{"type": "HEATING" |
    "AIR_CONDITIONING", "power": "OFF"}` with a fresh
    `{"type": "MANUAL"}` termination. Each zone's state is saved when its cloud call succeeds (an earlier saved state of the zone is kept, and nothing is saved while State Restore is off),
    so `restore_climate_previous_state` can put the zone back (a scheduled zone returns to its schedule).
    """
    coord = _resolve_single_coordinator(hass)

    zones_info = coord.data_loader.get_cached("zones_info")
    if not zones_info or not isinstance(zones_info, list):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="turn_off_all_no_climate_zones",
        )

    target_zones: list[tuple[str, str]] = [
        (str(zone["id"]), zone["type"])
        for zone in live_zones(zones_info, coord.data)
        if is_climate_zone(zone.get("type") or "")
    ]
    if not target_zones:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="turn_off_all_no_climate_zones",
        )

    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(
        hass, coordinator=coord, cost=len(target_zones),
    )
    if should_block:
        await _ratelimit.async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )

    failures: list[tuple[str, str]] = []
    success_count = 0
    for zone_id, zone_type in target_zones:
        ok = await run_service_call(
            hass=hass,
            coordinator=coord,
            zone_id=zone_id,
            entity_type="climate_ac" if zone_type == "AIR_CONDITIONING" else "climate_heating",
            api_coro=coord.api_client.set_zone_overlay(
                zone_id,
                {"type": zone_type, "power": "OFF"},
                {"type": "MANUAL"},
            ),
            capture_source="set_hvac_mode",
            refresh_entity_id=None,
            reason="turn_off_all_zones",
            own_power_off=True,
        )
        if ok:
            success_count += 1
        else:
            failures.append((zone_id, "cloud rejected the call"))

    if success_count == 0:
        _raise_service_error(
            "turn_off_all_zones_failed",
            reasons=", ".join(f"zone {z}: {r}" for z, r in failures[:3]),
        )
    elif failures:
        _LOGGER.warning(
            "Services: turn_off_all_climate_zones succeeded on %d of %d zone(s); "
            "failed for %s",
            success_count, len(target_zones),
            ", ".join(z for z, _ in failures),
        )

    # A forced zone fetch, so the poll reads the new state even while HomeKit would skip the zone states.
    coord.request_forced_zone_fetch()
    await coord.async_request_refresh()


async def handle_resume_schedule(entity: Any, call: ServiceCall) -> None:
    """Service handler for the resume schedule services: clear the overlay on one zone."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id
    # Read before the cancel below ends the entry: data from before the outage cannot show a change held since.
    owed_before = _coord.write_ledger.owes_change(zone_id) is True
    # Before the on-schedule skip: a pending debounced write must not re-create an overlay.
    cancel_zone_tap(hass, _coord, zone_id)
    if ResumeGuard.should_skip_resume(_coord, zone_id, owed_before=owed_before):
        _LOGGER.debug(
            "Services: resume schedule, zone %s already on "
            "schedule, skipping redundant cloud call",
            zone_id,
        )
        return

    await _check_bootstrap_reserve(hass, [entity_id], cost=1)

    api_success = await run_service_call(
        hass=hass,
        coordinator=_coord,
        zone_id=zone_id,
        entity_type=entity.entity_type,
        api_coro=_coord.api_client.delete_zone_overlay(zone_id),
        capture_source=None,
        refresh_entity_id=entity_id,
        reason="resume_schedule",
        own_power_off=False,
    )
    if not api_success:
        _LOGGER.warning(
            "Services: resume schedule failed for %s, "
            "the cloud rejected the call, the zone "
            "remains on its overlay",
            entity_id,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="resume_schedule_failed",
            translation_placeholders={"entity_id": entity_id},
        )


async def handle_set_active_timetable(entity: Any, call: ServiceCall) -> None:
    """Service handler for `set_climate_active_timetable`: switch the active Tado timetable of one zone."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id
    timetable_type = str(call.data["timetable"]).upper()

    # One PUT, plus the read-back (the active timetable and one call per day block) when a schedule is cached.
    read_back_calls = 1 + len(DAY_TYPES[timetable_type]) if isinstance(_coord.data_loader.get_zone_schedule(zone_id), dict) else 0
    await _check_bootstrap_reserve(hass, [entity_id], cost=1 + read_back_calls)

    api_success = await run_service_call(
        hass=hass,
        coordinator=_coord,
        zone_id=zone_id,
        entity_type="climate",
        api_coro=_coord.api_client.set_zone_active_timetable(zone_id, timetable_type),
        capture_source=None,
        refresh_entity_id=entity_id,
        reason="set_active_timetable",
        stamp_write=False,
        cancels_pending_write=False,
        own_power_off=None,
    )
    if not api_success:
        _LOGGER.warning(
            "Services: set_climate_active_timetable failed for %s, "
            "the cloud rejected the call, the zone keeps its timetable",
            entity_id,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="set_active_timetable_failed",
            translation_placeholders={"entity_id": entity_id},
        )
    _LOGGER.info("Services: set the active timetable of %s to %s", entity_id, timetable_type)
    cached = _coord.data_loader.get_zone_schedule(zone_id)
    if isinstance(cached, dict):
        try:
            await async_refresh_zone_schedule(
                _coord, zone_id, str(cached.get("name", entity_id)), fallback_type=timetable_type,
            )
        except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as read_err:
            dispatch_to_service_call(read_err, _coord.config_entry, hass)
    else:
        _LOGGER.debug("Services: set_climate_active_timetable for %s has no cached schedule, skipping the read-back", entity_id)


async def handle_set_temp_offset(entity: Any, call: ServiceCall) -> None:
    """Service handler for `set_climate_temperature_offset`: write the offset to one zone."""
    await _write_zone_offset(entity, call.data.get("offset"))


async def _write_zone_offset(entity: Any, offset: Any) -> None:
    """Write the offset to every TRV in the zone, then read it back from the cloud for the cache."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id

    # Find ALL device serials for this zone (multi-TRV support)
    serials = await hass.async_add_executor_job(
        _get_zone_device_serials,
        zone_id,
        _coord.data_loader,
    )
    if serials:
        # One write per device plus the readback.
        should_block, reason = await _ratelimit.async_check_bootstrap_reserve(
            hass, coordinator=_coord, cost=len(serials) + 1,
        )
        if should_block:
            await _ratelimit.async_show_api_limit_notification(hass, reason)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_quota_critically_low",
            )
        # Track per-device write success so we can
        # distinguish total / partial / clean cases.
        success_count = 0
        typed_refusal: HomeAssistantError | None = None
        for serial in serials:
            try:
                written = await run_service_call(
                    hass=hass,
                    coordinator=_coord,
                    zone_id=zone_id,
                    entity_type="climate",
                    api_coro=_coord.api_client.set_device_offset(serial, offset),
                    capture_source=None,
                    refresh_entity_id=None,
                    reason="set_temp_offset",
                    stamp_write=False,
                    cancels_pending_write=False,
                )
            except HomeAssistantError as err:
                # With a device already written, finish the bookkeeping and re-raise after it.
                if success_count == 0 or not is_typed_cloud_rejection(err):
                    raise
                typed_refusal = err
                break
            if written:
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
                "hold the first device's readback, or the "
                "written value when the readback is skipped",
                entity_id,
                len(serials) - success_count,
                len(serials),
            )

        # Read the offset back from the cloud rather than
        # caching the user's input, that breaks the
        # automation feedback loop where `offset_celsius`
        # reflects the automation's own last write.
        try:
            # A refused device means the cloud is refusing us, so the readback would only spend quota.
            readback = None if typed_refusal else await _coord.api_client.get_device_offset(serials[0])
        except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as err:
            # The write already landed: recover (back-off / reauth) and carry on so the sync pause still starts.
            handle_background_write_error(
                err, _coord.config_entry, hass,
                f"Services: offset readback for zone {zone_id} was refused after the write, "
                "caching the written value and starting recovery",
            )
            readback = None
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
            cached_offsets = await _coord.data_loader.async_merge_store("offsets", {zone_id: cache_value})

            if _coord.data and isinstance(_coord.data, dict):
                _coord.data["offsets"] = cached_offsets

            _LOGGER.debug(
                "Services: offsets cache updated for zone "
                "%s: %s°C",
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
        if typed_refusal is not None:
            raise typed_refusal
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
        stamp_write=False,
    )

    if not success:
        _LOGGER.warning(
            "Services: add_meter_reading failed (reading=%s), "
            "the cloud rejected the submission, please retry",
            reading,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_write_failed",
        )


_REFRESH_FEATURE_NAMES = {
    FORCEABLE_FETCH_MOBILE: "Mobile devices",
    FORCEABLE_FETCH_HOME_STATE: "Home presence",
}


async def handle_refresh(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for `refresh`: force an immediate fetch of one floored data type."""
    fetch_type = str(call.data["data"])
    coord = _resolve_single_coordinator(hass)
    cm = coord.config_manager

    if fetch_type == FORCEABLE_FETCH_ZONE_STATES:
        # Zone states are core polling data, not an optional feature, so
        # there's no toggle to gate on here.
        enabled = True
    elif fetch_type == FORCEABLE_FETCH_MOBILE:
        enabled = cm.get_mobile_devices_enabled() and cm.get_mobile_devices_frequent_sync()
    elif fetch_type == FORCEABLE_FETCH_HOME_STATE:
        enabled = cm.get_home_state_sync_enabled()
    else:
        enabled = False

    if not enabled:
        _LOGGER.warning("Services: refresh(%s) requested but the feature is off", fetch_type)
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="feature_not_enabled",
            translation_placeholders={"feature": _REFRESH_FEATURE_NAMES[fetch_type]},
        )

    await _ratelimit.async_check_bootstrap_reserve_or_raise(hass, "refresh", coordinator=coord)

    if fetch_type == FORCEABLE_FETCH_ZONE_STATES:
        coord.request_forced_zone_fetch()
    else:
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
        stamp_write=False,
    )

    if not success:
        _LOGGER.warning(
            "Services: identify_device failed for %s, the cloud "
            "did not acknowledge the request, please retry",
            mask_serial(device_serial or ""),
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_write_failed",
        )


async def handle_set_away_config(entity: Any, call: ServiceCall) -> None:
    """Service handler for `set_climate_away_configuration`: configure away-mode behaviour for one zone."""
    mode = call.data.get("mode")
    temperature = call.data.get("temperature")
    comfort_level = call.data.get("comfort_level", 50)
    await _set_away_on_zone(entity, mode, temperature, comfort_level)


async def _set_away_on_zone(entity: Any, mode: Any, temperature: Any, comfort_level: Any) -> None:
    """Configure away-mode behaviour for one zone."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id

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
        zone_id=zone_id,
        entity_type="climate",
        api_coro=_coord.api_client.set_away_configuration(
            zone_id,
            mode,
            temperature,
            comfort_level,
        ),
        capture_source=None,
        refresh_entity_id=entity_id,
        reason="set_away_config",
        stamp_write=False,
        cancels_pending_write=False,
    )
    if not success:
        _LOGGER.warning(
            "Services: set_climate_away_configuration failed for "
            "%s, the cloud rejected the call, please retry",
            entity_id,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_write_failed",
        )


async def handle_activate_open_window(entity: Any, call: ServiceCall) -> None:
    """Service handler for `activate_climate_open_window`: same effect as tapping the icon in the Tado app."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id

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
        zone_id=zone_id,
        entity_type="climate",
        api_coro=_coord.api_client.activate_open_window(zone_id),
        capture_source=None,
        refresh_entity_id=entity_id,
        reason="activate_open_window",
        stamp_write=False,
    )
    if success:
        _LOGGER.debug(
            "Services: activated open window for %s",
            entity_id,
        )
    else:
        _LOGGER.warning(
            "Services: activate_climate_open_window failed for "
            "%s, the cloud rejected the call, the zone "
            "remains in its current state",
            entity_id,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_write_failed",
        )


async def handle_deactivate_open_window(entity: Any, call: ServiceCall) -> None:
    """Service handler for `deactivate_climate_open_window`: clear open-window mode and resume heating."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id

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
        zone_id=zone_id,
        entity_type="climate",
        api_coro=_coord.api_client.deactivate_open_window(zone_id),
        capture_source=None,
        refresh_entity_id=entity_id,
        reason="deactivate_open_window",
        stamp_write=False,
    )
    if success:
        _LOGGER.debug(
            "Services: deactivated open window for %s",
            entity_id,
        )
    else:
        _LOGGER.warning(
            "Services: deactivate_climate_open_window failed "
            "for %s, the cloud rejected the call, the "
            "zone remains in open-window mode",
            entity_id,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_write_failed",
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
    """Build a frost-protection overlay for set_climate_open_window_mode (setting, termination, human duration)."""
    setting: dict[str, str | dict[str, float]] = (
        {"type": "AIR_CONDITIONING", "power": "OFF"}
        if zone_type == "AIR_CONDITIONING"
        else {"type": "HEATING", "power": "ON", "temperature": {"celsius": OPEN_WINDOW_DEFAULT_TEMP}}
    )

    if timeout == 0:
        termination: dict[str, str | int] = build_timer_termination(overlay="manual")
        duration_desc = "indefinite"
    else:
        timeout_minutes = min(int(timeout) // 60, TIMER_DURATION_MAX)
        termination = build_timer_termination(duration_minutes=timeout_minutes)
        duration_desc = f"{timeout_minutes} min"

    return setting, termination, duration_desc


async def handle_set_open_window_mode(entity: Any, call: ServiceCall) -> None:
    """Service handler for `set_climate_open_window_mode`: an explicit frost overlay, for zones without Open Window Detection."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id

    time_period = call.data.get("time_period")
    duration_seconds = None if time_period is None else int(time_period.total_seconds())

    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
    if should_block:
        await _ratelimit.async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )

    timeout = _resolve_open_window_timeout(_coord, zone_id, duration_seconds)
    setting, termination, duration_desc = _build_open_window_overlay(entity.zone_type, timeout)

    success = await run_service_call(
        hass=hass,
        coordinator=_coord,
        zone_id=zone_id,
        entity_type=entity.entity_type,
        api_coro=_coord.api_client.set_zone_overlay(zone_id, setting, termination),
        capture_source="set_open_window_mode",
        refresh_entity_id=entity_id,
        reason="set_open_window_mode",
        own_power_off=setting.get("power") == "OFF",
    )
    if success:
        _LOGGER.info(
            "Services: set open window mode on %s (%s, "
            "%s°C frost protection)",
            entity_id, duration_desc, OPEN_WINDOW_DEFAULT_TEMP,
        )
    else:
        _LOGGER.warning(
            "Services: set_climate_open_window_mode failed for %s, "
            "the cloud rejected the call, the zone remains in "
            "its current state",
            entity_id,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_write_failed",
        )


async def handle_get_temp_offset(entity: Any, call: ServiceCall) -> dict[str, Any]:
    """Service handler for `get_climate_temperature_offset`: return the live offset of one zone for an automation."""
    hass, entity_id, _coord, zone_id = entity.hass, entity.entity_id, entity.coordinator, entity.zone_id

    serial = await hass.async_add_executor_job(
        _get_zone_device_serial,
        zone_id,
        _coord.data_loader,
    )
    if not serial:
        _LOGGER.warning("Services: get_climate_temperature_offset found no device for %s", entity_id)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="zone_without_device",
            translation_placeholders={"entity_id": entity_id},
        )
    result = None
    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=_coord)
    if should_block:
        _LOGGER.warning("Services: get_climate_temperature_offset blocked by the quota floor: %s", reason)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )
    try:
        result = await _coord.api_client.get_device_offset(serial)
    except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as err:
        dispatch_to_service_call(err, _coord.config_entry, hass)
    if result is None:
        _LOGGER.warning("Services: get_climate_temperature_offset could not fetch the offset for %s", entity_id)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_read_failed",
        )
    return {"offset": result}


_FLOW_TEMPERATURE_KNOWN_KEYS = frozenset({
    "maxFlowTemperature",
    "openThermDeviceSerialNumber",
    "autoAdaptation",
    "maxFlowTemperatureConstraints",
    "hasMultipleBoilerControlDevices",
})


def _mask_serials(value: Any, in_serial: bool = False) -> Any:
    """Return `value` with everything under a key containing 'serial' shortened, keys included."""
    if isinstance(value, dict):
        return {
            (mask_serial(key) if in_serial and isinstance(key, str) else key): _mask_serials(
                item, in_serial or "serial" in str(key).lower(),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_serials(item, in_serial) for item in value]
    if in_serial and value is not None:
        return mask_serial(str(value))
    return value


def _classify_flow_temperature_body(body: dict[str, Any] | None) -> tuple[str, str, dict[str, Any]]:
    """Return (status, message, masked settings) for the flowTemperatureOptimization body."""
    if body is None:
        return "failed", "The request to Tado failed, see the Home Assistant log for details.", {}
    if not body:
        return (
            "not_applicable",
            "Tado has no flow temperature setting for this home, which is normal without an OpenTherm heat source.",
            {},
        )
    errors = body.get("errors")
    if isinstance(errors, list) and any(isinstance(e, dict) and e.get("code") == "accessDenied" for e in errors):
        return (
            "access_denied",
            "Tado refused the request for this home (HTTP 403). Nothing was changed.",
            {},
        )
    if _FLOW_TEMPERATURE_KNOWN_KEYS & body.keys():
        return "ok", "Tado's stored flow temperature setting, serial numbers shortened.", _mask_serials(body)
    return (
        "unexpected_response",
        "Tado answered with a body this integration does not recognise, returned as it came, serial numbers shortened.",
        _mask_serials(body),
    )


async def handle_get_flow_temperature_settings(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Service handler for `get_flow_temperature_settings`: read Tado's stored flow temperature setting."""
    coord = _resolve_single_coordinator(hass)
    should_block, reason = await _ratelimit.async_check_bootstrap_reserve(hass, coordinator=coord)
    if should_block:
        _LOGGER.warning("Services: get_flow_temperature_settings blocked by the quota floor: %s", reason)
        return {"status": "failed", "message": reason, "settings": {}}
    body: dict[str, Any] | None
    try:
        body = await coord.api_client.get_flow_temperature_settings()
    except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as err:
        dispatch_to_service_call(err, coord.config_entry, hass)
    except TadoSyncError:
        _LOGGER.debug("Services: get_flow_temperature_settings hit a Tado sync error", exc_info=True)
        body = None
    status, message, settings = _classify_flow_temperature_body(body)
    _LOGGER.debug(
        "Services: get_flow_temperature_settings status=%s keys=%s body=%s",
        status, sorted(settings), settings,
    )
    return {"status": status, "message": message, "settings": settings}


async def handle_restore_previous_state(entity: Any, call: ServiceCall) -> None:
    """Service handler for the restore previous state services: undo the last overlay write of one zone."""
    hass, entity_id, _coord, zone_id, entity_type = (
        entity.hass, entity.entity_id, entity.coordinator, entity.zone_id, entity.entity_type,
    )

    # Peek without consuming: the captured state is only
    # cleared after the cloud write succeeds, so a transient
    # failure leaves the baseline intact for a retry.
    captured = await _coord.async_peek_state(zone_id, entity_type)

    # Both resume branches below call delete_zone_overlay. If the zone is
    # already on schedule (no active overlay), that cloud call is a no-op,
    # so skip it to save quota.
    wants_resume = captured is None or captured.overlay_type is None
    # Before the on-schedule skip: a pending debounced write must not re-create an overlay.
    # Read before the cancel ends the entry: data from before the outage cannot show a change held since.
    owed_before = _coord.write_ledger.owes_change(zone_id) is True
    if wants_resume:
        cancel_zone_tap(hass, _coord, zone_id)
    if wants_resume and ResumeGuard.should_skip_resume(_coord, zone_id, owed_before=owed_before):
        _LOGGER.debug(
            "Services: restore previous state, zone %s already on "
            "schedule, skipping redundant resume call", zone_id,
        )
        return

    await _check_bootstrap_reserve(hass, [entity_id], cost=1)

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
            own_power_off=False,
        )
        if api_success:
            _LOGGER.info(
                "Services: restore previous state, no baseline "
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
            own_power_off=False,
        )
        if api_success:
            _LOGGER.info(
                "Services: restore previous state, restored "
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
            own_power_off=setting.get("power") == "OFF",
        )
        if api_success:
            degrees = "" if captured.temperature is None else f", {captured.temperature}°C"
            _LOGGER.info(
                "Services: restore previous state, restored overlay for %s (type %s%s)",
                entity_id,
                captured.overlay_type,
                degrees,
            )

    if api_success:
        if captured is not None:
            await _coord.async_clear_captured_state(zone_id, entity_type)
        await async_trigger_immediate_refresh(hass, entity_id, "restore_previous_state")
    else:
        _LOGGER.warning(
            "Services: restore previous state failed for %s, "
            "the cloud rejected the call, captured baseline "
            "preserved so the user can retry",
            entity_id,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="state_restore_failed",
            translation_placeholders={"entity_id": entity_id},
        )


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register every Tado CE service handler with HA's service registry."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_CLIMATE_TIMER):
        _LOGGER.debug(
            "Services: registration already complete, skipping",
        )
        return

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_CLIMATE_TIMER,
        entity_domain="climate",
        func=handle_set_climate_timer,
        schema=cv.make_entity_service_schema(
            {
                vol.Required("temperature"): vol.All(vol.Coerce(float), vol.Range(min=5, max=30)),
                vol.Optional("time_period"): _time_period_text,
                vol.Optional("overlay"): vol.All(cv.string, vol.Lower, vol.In(["next_time_block", "tado_mode", "manual"])),
            },
        ),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_CLIMATE_SCHEDULE_TEMPERATURE,
        entity_domain="climate",
        func=handle_set_schedule_temperature,
        schema=cv.make_entity_service_schema(
            {
                vol.Required("temperature"): vol.All(vol.Coerce(float), vol.Range(min=5, max=30)),
                vol.Optional("overlay"): vol.All(cv.string, vol.Lower, vol.In(["next_time_block", "tado_mode", "manual"])),
                vol.Optional("force_override"): cv.boolean,
            },
        ),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_WATER_HEATER_TIMER,
        entity_domain="water_heater",
        func=handle_set_water_heater_timer,
        schema=cv.make_entity_service_schema(
            {
                vol.Required("time_period"): _time_period_text,
                vol.Optional("temperature"): vol.Coerce(float),
            },
        ),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RESUME_CLIMATE_SCHEDULE,
        entity_domain="climate",
        func=handle_resume_schedule,
        schema=cv.make_entity_service_schema({}),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RESUME_WATER_HEATER_SCHEDULE,
        entity_domain="water_heater",
        func=handle_resume_schedule,
        schema=cv.make_entity_service_schema({}),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_CLIMATE_ACTIVE_TIMETABLE,
        entity_domain="climate",
        func=handle_set_active_timetable,
        schema=cv.make_entity_service_schema(
            {
                vol.Required("timetable"): vol.In(["one_day", "three_day", "seven_day"]),
            },
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_TURN_OFF_ALL_CLIMATE_ZONES,
        functools.partial(handle_turn_off_all_zones, hass),
        schema=vol.Schema({}),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_TEMP_OFFSET,
        entity_domain="climate",
        func=handle_set_temp_offset,
        schema=cv.make_entity_service_schema(
            {
                vol.Required("offset"): vol.All(
                    vol.Coerce(float), vol.Range(min=DEVICE_OFFSET_MIN, max=DEVICE_OFFSET_MAX),
                ),
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

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_CLIMATE_AWAY_CONFIGURATION,
        entity_domain="climate",
        func=handle_set_away_config,
        schema=cv.make_entity_service_schema(
            {
                vol.Required("mode"): cv.string,
                vol.Optional("temperature"): vol.Coerce(float),
                vol.Optional("comfort_level"): vol.Coerce(int),
            },
        ),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_ACTIVATE_CLIMATE_OPEN_WINDOW,
        entity_domain="climate",
        func=handle_activate_open_window,
        schema=cv.make_entity_service_schema({}),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_DEACTIVATE_CLIMATE_OPEN_WINDOW,
        entity_domain="climate",
        func=handle_deactivate_open_window,
        schema=cv.make_entity_service_schema({}),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_CLIMATE_OPEN_WINDOW_MODE,
        entity_domain="climate",
        func=handle_set_open_window_mode,
        schema=cv.make_entity_service_schema(
            {
                vol.Optional("time_period"): vol.All(_time_period_text, _open_window_period),
            },
        ),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_GET_CLIMATE_TEMPERATURE_OFFSET,
        entity_domain="climate",
        func=handle_get_temp_offset,
        schema=cv.make_entity_service_schema({}),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FLOW_TEMPERATURE_SETTINGS,
        functools.partial(handle_get_flow_temperature_settings, hass),
        schema=vol.Schema({}),
        supports_response=True,  # type: ignore[arg-type]
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RESTORE_CLIMATE_PREVIOUS_STATE,
        entity_domain="climate",
        func=handle_restore_previous_state,
        schema=cv.make_entity_service_schema({}),
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RESTORE_WATER_HEATER_PREVIOUS_STATE,
        entity_domain="water_heater",
        func=handle_restore_previous_state,
        schema=cv.make_entity_service_schema({}),
    )

    _LOGGER.info("Services: registered all Tado CE service handlers")
