"""Tado CE diagnostics: config entry debug dump with PII redaction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator

# Keys to redact from config entry data
TO_REDACT_CONFIG = {
    "home_id",
    "token",
    "refresh_token",
    "access_token",
    "username",
    "password",
    "email",
    "bridge_serial",
    "bridge_auth_key",
}

# Keys to redact from coordinator / API response data (defense-in-depth).
# Even though diagnostics currently only dumps safe summaries, this set
# ensures PII is auto-redacted if future code exposes raw API data.
TO_REDACT_DATA = {
    # User PII
    "users",
    "firstName",
    "lastName",
    "email",
    "phoneNumber",
    "identifiers",
    # Device / location identifiers
    "serialNo",
    "serialNumber",
    "macAddress",
    "shortSerialNo",
    # Home identifiers
    "homeId",
    "home_id",
    "name",
    # Network / security
    "authKey",
    "bridge_auth_key",
    # Mobile device PII
    "deviceMetadata",
    "location",
    "geoTrackingEnabled",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    # Redact sensitive fields from entry data and options
    redacted_entry_data = async_redact_data(dict(entry.data), TO_REDACT_CONFIG)
    redacted_options = async_redact_data(dict(entry.options), TO_REDACT_CONFIG)

    # Build coordinator data summary (redact PII from nested dicts)
    coord_data = coordinator.data or {}
    redacted_coord: dict[str, Any] = {}

    # Safe keys: defense-in-depth redaction for future API schema changes
    for key in ("ratelimit", "weather", "offsets"):
        if key in coord_data:
            redacted_coord[key] = async_redact_data(coord_data[key], TO_REDACT_DATA)

    # Zone count summary (don't dump full zone state)
    zones_info = coord_data.get("zones_info") or []
    redacted_coord["zones_info_count"] = len(zones_info)
    redacted_coord["zone_types"] = _summarise_zone_types(zones_info)

    # Config manager settings (redact any PII that might leak through)
    config_summary: dict[str, Any] = {}
    if coordinator.config_manager:
        config_summary = coordinator.config_manager.get_all_config()
    redacted_coord["config_settings"] = async_redact_data(config_summary, TO_REDACT_DATA)

    # Coordinator metadata
    redacted_coord["update_interval_seconds"] = (
        coordinator.update_interval.total_seconds() if coordinator.update_interval else None
    )
    redacted_coord["last_update_success"] = coordinator.last_update_success

    # State restore captured states (privacy-safe: no temperature values)
    sr_diagnostics = coordinator.get_state_restore_diagnostics()
    if sr_diagnostics:
        redacted_coord["state_restore_captured"] = sr_diagnostics

    # HomeKit diagnostics (redact credentials)
    homekit_diag: dict[str, Any] = {"status": "not_configured"}
    if coordinator.homekit_client is not None:
        client = coordinator.homekit_client
        if client.is_connected:
            homekit_diag["status"] = "connected"
        else:
            homekit_diag["status"] = "disconnected"
        mapped = len(client.zone_aid_map) if hasattr(client, "zone_aid_map") else 0
        homekit_diag["mapped_zones"] = mapped
        from .const import HOMEKIT_CACHE_REFRESH_SECONDS, get_climate_zone_ids

        all_climate = get_climate_zone_ids(zones_info)
        homekit_diag["unmapped_zones"] = max(0, len(all_climate) - mapped)
        homekit_diag["cache_refresh_interval_seconds"] = HOMEKIT_CACHE_REFRESH_SECONDS
        # Check pairing store existence without exposing credentials
        homekit_diag["pairing_configured"] = client.is_connected or client.pairing is not None

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "data": redacted_entry_data,
            "options": redacted_options,
        },
        "coordinator": redacted_coord,
        "homekit": homekit_diag,
        "data_flow_health": _build_data_flow_health(coordinator),
    }


def _build_data_flow_health(
    coordinator: TadoDataUpdateCoordinator,
) -> dict[str, Any]:
    """Build data flow health summary for diagnostics."""
    health: dict[str, Any] = {}

    last_cloud = getattr(coordinator, "_last_cloud_zone_fetch", None)
    health["last_cloud_zone_fetch"] = last_cloud.isoformat() if last_cloud else None

    last_weather = getattr(coordinator, "_last_weather_fetch", None)
    health["last_weather_fetch"] = last_weather.isoformat() if last_weather else None

    provider = getattr(coordinator, "homekit_provider", None)
    if provider is not None:
        health["homekit_connected"] = provider.is_connected
    else:
        health["homekit_status"] = "not_configured"

    bht = getattr(coordinator, "bridge_health_tracker", None)
    if bht is not None:
        health["bridge_connected"] = bht.state.is_connected
        health["bridge_consecutive_failures"] = bht.state.consecutive_failures
    else:
        health["bridge_status"] = "not_configured"

    health["outdoor_temp_history_length"] = len(
        getattr(coordinator, "_outdoor_temp_history", []),
    )
    health["wc_state_loaded"] = getattr(coordinator, "_wc_state_loaded", False)

    scm = getattr(coordinator, "smart_comfort_manager", None)
    if scm is not None:
        health["smart_comfort_zones"] = len(getattr(scm, "_zones", {}))
    else:
        health["smart_comfort_status"] = "disabled"

    return health


def _summarise_zone_types(zones_info: list[dict[str, Any]]) -> dict[str, int]:
    """Summarise zone types from zones_info list."""
    counts: dict[str, int] = {}
    for zone in zones_info:
        zone_type = zone.get("type", "UNKNOWN")
        counts[zone_type] = counts.get(zone_type, 0) + 1
    return counts
