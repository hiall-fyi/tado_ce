"""Tado CE setup helpers: optional per-entry feature initialisers (Smart Comfort uses 3-tier load: cache → recorder → statistics)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import slugify

from .helpers import mask_serial

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .adaptive_preheat import AdaptivePreheatManager
    from .api_client import TadoApiClient
    from .config_manager import ConfigurationManager
    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader
    from .heating_coordinator import HeatingCycleCoordinator
    from .smart_comfort import SmartComfortManager
    from .state_restore_manager import StateRestoreManager
    from .zone_config_manager import ZoneConfigManager

_LOGGER = logging.getLogger(__name__)


async def async_log_file_system_state(hass: HomeAssistant) -> None:
    """Log which Tado CE data files exist on disk during setup."""
    from .const import CONFIG_FILE, DATA_DIR, ZONES_FILE, ZONES_INFO_FILE

    def _check() -> dict[str, Any]:
        return {
            "data_dir_exists": DATA_DIR.exists(),
            "config_file_exists": CONFIG_FILE.exists(),
            "zones_file_exists": ZONES_FILE.exists(),
            "zones_info_file_exists": ZONES_INFO_FILE.exists(),
        }

    fs_state = await hass.async_add_executor_job(_check)
    _LOGGER.debug(
        "Setup: data file presence. DATA_DIR=%s (%s), "
        "CONFIG_FILE=%s (%s), ZONES_FILE=%s (%s), "
        "ZONES_INFO_FILE=%s (%s)",
        DATA_DIR,
        fs_state["data_dir_exists"],
        CONFIG_FILE,
        fs_state["config_file_exists"],
        ZONES_FILE,
        fs_state["zones_file_exists"],
        ZONES_INFO_FILE,
        fs_state["zones_info_file_exists"],
    )


async def async_init_smart_comfort(
    hass: HomeAssistant,
    data_loader: DataLoader,
    config_manager: ConfigurationManager,
    home_id: str | None,
) -> SmartComfortManager | None:
    """Build the Smart Comfort manager and run 3-tier load (2h cache → 24h recorder → 7d statistics)."""
    if not config_manager.get_smart_comfort_enabled():
        return None

    from .smart_comfort import (
        SmartComfortManager,
        async_load_baseline_from_statistics,
        async_load_history_from_recorder,
    )

    history_days = config_manager.get_smart_comfort_history_days()
    smart_comfort_manager = SmartComfortManager(
        hass=hass,
        home_id=home_id or "",
        history_days=history_days,
        data_loader=data_loader,
    )
    smart_comfort_manager.enable()

    outdoor_temp_entity = config_manager.get_outdoor_temp_entity()
    weather_compensation = config_manager.get_smart_comfort_mode()
    use_feels_like = config_manager.get_use_feels_like()

    if outdoor_temp_entity:
        smart_comfort_manager.configure_weather(
            outdoor_temp_entity=outdoor_temp_entity,
            weather_compensation=weather_compensation,
            use_feels_like=use_feels_like,
        )

    # Tier 1: 2h cache (fastest)
    cache_readings = await smart_comfort_manager.async_load()

    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)

    if zones_info:
        entity_to_zone_id = {
            slugify(zone.get("name", "")): str(zone.get("id"))
            for zone in zones_info
            if zone.get("name") and zone.get("id")
        }

        climate_entity_ids = [f"climate.{entity_name}" for entity_name in entity_to_zone_id]

        # Tier 2: 24h recorder (layered on top of cache)
        recorder_readings = 0
        if climate_entity_ids:
            recorder_readings = await async_load_history_from_recorder(
                hass,
                smart_comfort_manager,
                climate_entity_ids,
                entity_to_zone_id,
            )

        # Tier 3: 7d hourly baselines (fallback when zones have no cache/recorder data)
        zone_sensor_mapping = {
            str(zone.get("id")): f"sensor.{slugify(zone.get('name', ''))}_temperature"
            for zone in zones_info
            if zone.get("name") and zone.get("id")
        }
        baseline_stats = await async_load_baseline_from_statistics(
            hass,
            smart_comfort_manager,
            zone_sensor_mapping,
        )

        _LOGGER.debug(
            "Setup: Smart Comfort load summary, cache=%s, "
            "recorder=%s, baseline_zones=%s",
            cache_readings,
            recorder_readings,
            len(baseline_stats),
        )

    _LOGGER.info("Setup: Smart Comfort Analytics enabled")
    return smart_comfort_manager


async def async_init_heating_cycle(
    hass: HomeAssistant,
    config_manager: ConfigurationManager,
    home_id: str | None,
) -> HeatingCycleCoordinator | None:
    """Build the Heating Cycle Coordinator (always enabled for HEATING zones)."""
    if not home_id:
        return None

    try:
        from .heating_coordinator import HeatingCycleCoordinator
        from .heating_models import HeatingCycleConfig

        heating_cycle_config = HeatingCycleConfig(
            enabled=True,
            rolling_window_days=config_manager.get_heating_cycle_history_days(),
            inertia_threshold_celsius=config_manager.get_heating_cycle_inertia_threshold(),
            min_cycles=config_manager.get_heating_cycle_min_cycles(),
        )

        _LOGGER.debug(
            "Setup: Heating Cycle config, min_cycles=%d, "
            "history_days=%d, inertia_threshold=%.2f",
            heating_cycle_config.min_cycles,
            heating_cycle_config.rolling_window_days,
            heating_cycle_config.inertia_threshold_celsius,
        )

        heating_cycle_coordinator = HeatingCycleCoordinator(
            hass,
            home_id,
            heating_cycle_config,
        )
        await heating_cycle_coordinator.async_setup()

        _LOGGER.info("Setup: Heating Cycle Analysis ready")
        return heating_cycle_coordinator
    except Exception:
        _LOGGER.warning(
            "Setup: Heating Cycle Analysis init failed, feature "
            "disabled this session, will retry on the next "
            "integration reload",
            exc_info=True,
        )
        return None


async def async_init_adaptive_preheat(
    hass: HomeAssistant,
    config_manager: ConfigurationManager,
    *,
    api_client: TadoApiClient | None,
    data_loader: DataLoader | None,
    zone_config_manager: ZoneConfigManager | None = None,
) -> AdaptivePreheatManager | None:
    """Build the Adaptive Preheat manager when the feature is enabled."""
    if not config_manager.get_adaptive_preheat_enabled():
        return None

    try:
        from .adaptive_preheat import AdaptivePreheatManager

        apm = AdaptivePreheatManager(
            hass,
            config_manager,
            api_client=api_client,
            data_loader=data_loader,
            zone_config_manager=zone_config_manager,
        )
        await apm.async_setup()
        _LOGGER.info("Setup: Adaptive Preheat ready")
        return apm
    except Exception:
        _LOGGER.warning(
            "Setup: Adaptive Preheat init failed, feature "
            "disabled this session, will retry on the next "
            "integration reload",
            exc_info=True,
        )
        return None


async def async_init_state_restore(
    hass: HomeAssistant,
    config_manager: ConfigurationManager,
    data_loader: DataLoader,
) -> StateRestoreManager | None:
    """Build the State Restore manager (resumes overlays after HA restart)."""
    try:
        from .state_restore_manager import StateRestoreManager

        srm = StateRestoreManager(
            hass,
            config_manager,
            data_loader,
        )
        await srm.async_setup()
        _LOGGER.info("Setup: State Restore manager ready")
        return srm
    except Exception:
        _LOGGER.warning(
            "Setup: State Restore manager init failed, overlays "
            "will not auto-resume after this restart, will retry "
            "on the next integration reload",
            exc_info=True,
        )
        return None



def schedule_heating_cycle_timeouts(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    heating_cycle_coordinator: HeatingCycleCoordinator,
) -> None:
    """Run the heating-cycle timeout checker every 60 seconds.

    The cancel handle lives on the coordinator so unload can
    stop the timer cleanly.
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from homeassistant.helpers.event import async_track_time_interval as _track

    async def _check_cycle_timeouts(_now: _dt) -> None:
        await heating_cycle_coordinator.check_timeouts()

    coordinator._heating_cycle_timeout_cancel = _track(
        hass,
        _check_cycle_timeouts,
        _td(seconds=60),
    )


async def async_load_insight_history(coordinator: TadoDataUpdateCoordinator) -> None:
    """Load persisted insight history and drop stale entries."""
    loaded_count = await coordinator.insight_history.async_load()
    pruned_count = coordinator.insight_history.prune_old_entries()
    if loaded_count or pruned_count:
        _LOGGER.debug(
            "Setup: insight history loaded %d entr(ies), pruned "
            "%d stale",
            loaded_count,
            pruned_count,
        )


async def async_activate_homekit(
    coordinator: TadoDataUpdateCoordinator,
    provider: Any,
    homekit_client: Any,
) -> None:
    """Bring HomeKit local control online during setup, degrading on failure.

    When the bridge is already connected, refresh the accessory list and
    subscribe to characteristic events. When it is not, start the background
    reconnect loop so it recovers without a manual reload.

    Both `async_refresh_accessories` and `async_subscribe_events` self-protect
    against a bridge dropping mid-operation (refresh returns an empty list,
    subscribe triggers a reconnect), so this brings HomeKit online without its
    own guard and a bridge failure here can never abort the config entry. The
    same two primitives back the per-poll and reconnect-callback paths, so all
    three callers bring HomeKit online the one way.
    """
    if homekit_client.is_connected:
        await provider.async_refresh_accessories()
        await provider.async_subscribe_events()
        return

    # Not connected at startup: let the background loop retry so recovery
    # needs no manual reload.
    await homekit_client.async_reconnect()


def register_bridge_devices(
    hass: HomeAssistant,
    entry_id: str,
    zones_info: list[dict[str, Any]],
) -> None:
    """Pre-register Tado bridge devices so zone devices can reference them via_device (HA registry contract)."""
    from homeassistant.helpers import device_registry as dr

    from .const import DOMAIN, TADO_BRIDGE_MODELS

    device_registry = dr.async_get(hass)
    seen_serials: set[str] = set()
    for zone in zones_info:
        for device in zone.get("devices") or []:
            device_type = device.get("deviceType", "")
            serial = device.get("shortSerialNo", "")
            if device_type in TADO_BRIDGE_MODELS and serial and serial not in seen_serials:
                seen_serials.add(serial)
                device_registry.async_get_or_create(
                    config_entry_id=entry_id,
                    identifiers={(DOMAIN, serial)},
                    manufacturer="Tado",
                    model=device_type,
                    name=device.get("serialNo", serial),
                    sw_version=device.get("currentFwVersion"),
                )
                _LOGGER.debug(
                    "Setup: pre-registered Tado bridge %s (%s)",
                    mask_serial(serial), device_type,
                )


def register_hub_device(hass: HomeAssistant, entry_id: str, home_id: str) -> None:
    """Pre-register the Tado CE hub device and cache its registry id for via_device_id lookups."""
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr

    from .device_manager import get_hub_device_info, set_cached_hub_device_id

    try:
        device_registry = dr.async_get(hass)
        hub_info = get_hub_device_info(home_id)
        entry = device_registry.async_get_or_create(config_entry_id=entry_id, **hub_info)
        set_cached_hub_device_id(home_id, entry.id)
    except HomeAssistantError:
        _LOGGER.warning(
            "Setup: could not pre-register the hub device, zone devices will "
            "register unparented this session",
            exc_info=True,
        )
