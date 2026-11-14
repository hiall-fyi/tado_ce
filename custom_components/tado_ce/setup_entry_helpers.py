"""Tado CE setup helpers: optional per-entry feature initialisers (Smart Comfort uses 3-tier load: cache → recorder → statistics)."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import slugify

from .const import SIGNAL_HOMEKIT_UPDATE
from .helpers import mask_serial, parse_iso_datetime, zone_entity_id
from .zone_signals import is_homekit_off_held

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant

    from .adaptive_preheat import AdaptivePreheatManager
    from .api_client import TadoApiClient
    from .config_manager import ConfigurationManager
    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader
    from .heating_coordinator import HeatingCycleCoordinator
    from .homekit_client import HomeKitClient
    from .smart_comfort import SmartComfortManager
    from .state_restore_manager import StateRestoreManager
    from .zone_config_manager import ZoneConfigManager

_LOGGER = logging.getLogger(__name__)


def _zone_created_dates(zones: list[dict[str, Any]]) -> dict[str, datetime]:
    """Map zone id to its current dateCreated; a missing or unreadable date leaves the zone uncut."""
    dates: dict[str, datetime] = {}
    for zone in zones:
        raw = zone.get("dateCreated")
        if not raw:
            continue
        try:
            dates[str(zone["id"])] = parse_iso_datetime(raw)
        except (ValueError, TypeError):
            _LOGGER.debug("Setup: unreadable dateCreated %r for zone %s, loading its history uncut", raw, zone.get("id"))
    return dates


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

    # Tier 1: 2h cache (fastest)
    cache_readings = await smart_comfort_manager.async_load()

    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)

    if zones_info:
        # Entities register under "default" when the entry has no home id.
        entity_home_id = home_id or "default"
        named_zones = [zone for zone in zones_info if zone.get("name") and zone.get("id")]
        climate_ids = {
            zone_entity_id(
                hass, "climate",
                "climate_ac" if zone.get("type") == "AIR_CONDITIONING" else "climate_heating",
                entity_home_id, str(zone.get("id")), slugify(zone["name"]),
            ): str(zone.get("id"))
            for zone in named_zones
        }
        # The recorder load looks zones up by the id without its domain prefix.
        entity_to_zone_id = {entity_id.removeprefix("climate."): zone_id for entity_id, zone_id in climate_ids.items()}

        climate_entity_ids = list(climate_ids)
        zone_since = _zone_created_dates(named_zones)

        # Tier 2: 24h recorder (layered on top of cache)
        recorder_readings = 0
        if climate_entity_ids:
            recorder_readings = await async_load_history_from_recorder(
                hass,
                smart_comfort_manager,
                climate_entity_ids,
                entity_to_zone_id,
                zone_since=zone_since,
            )

        # Tier 3: 7d hourly baselines (fallback when zones have no cache/recorder data)
        zone_sensor_mapping = {
            str(zone.get("id")): zone_entity_id(
                hass, "sensor", "sensor_temperature", entity_home_id, str(zone.get("id")),
                f"{slugify(zone['name'])}_temperature",
            )
            for zone in named_zones
        }
        baseline_stats = await async_load_baseline_from_statistics(
            hass,
            smart_comfort_manager,
            zone_sensor_mapping,
            zone_since=zone_since,
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
    home_id: str | None = None,
) -> AdaptivePreheatManager | None:
    """Build the Adaptive Preheat manager when the feature is enabled."""
    if not config_manager.get_adaptive_preheat_enabled():
        return None

    apm = None
    try:
        from .adaptive_preheat import AdaptivePreheatManager

        apm = AdaptivePreheatManager(
            hass,
            config_manager,
            api_client=api_client,
            data_loader=data_loader,
            zone_config_manager=zone_config_manager,
            home_id=home_id,
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
        # A setup that failed midway may already hold state listeners, and nothing else knows this manager.
        if apm is not None:
            await apm.async_unload()
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


def register_homekit_drop_fetch(
    coordinator: TadoDataUpdateCoordinator,
    homekit_client: HomeKitClient,
) -> None:
    """Fetch the zones from the cloud once on a HomeKit drop and once on its reconnect."""

    # Only the first reconnect the read loop starts fetches; repeats on a dead accessory change nothing in the cloud, so they ask for none.
    def _on_drop() -> None:
        if not homekit_client.catch_up_fetch:
            return
        coordinator.hass.async_create_task(
            coordinator.async_request_zone_fetch("homekit_disconnect"),
        )

    async def _on_reconnect() -> None:
        if not homekit_client.catch_up_fetch:
            return
        await coordinator.async_request_zone_fetch("homekit_reconnect")

    homekit_client.add_disconnect_callback(_on_drop)
    homekit_client.add_reconnect_callback(_on_reconnect)


def register_homekit_off_latch_listener(coordinator: TadoDataUpdateCoordinator) -> None:
    """Hold a room switched off in the Tado app from the moment the bridge reports it, until the entry unloads."""

    @callback
    def _on_homekit_update(zone_id: str) -> None:
        is_homekit_off_held(coordinator, zone_id)

    # The entry unload list runs on unload and on a failed setup, so a reload never leaves a second handler behind.
    coordinator.config_entry.async_on_unload(
        async_dispatcher_connect(
            coordinator.hass, SIGNAL_HOMEKIT_UPDATE.format(home_id=coordinator.home_id), _on_homekit_update,
        ),
    )


async def async_start_homekit(
    coordinator: TadoDataUpdateCoordinator,
    provider: Any,
    homekit_client: HomeKitClient,
    on_reconnect: Callable[[], Awaitable[None]],
) -> None:
    """Register the re-subscribe callback, then activate, then register the drop fetch and the app-off listener."""
    homekit_client.add_reconnect_callback(on_reconnect)

    # Activation must degrade to cloud-only, never abort the entry.
    try:
        await async_activate_homekit(coordinator, provider, homekit_client)
    except Exception as e:
        _LOGGER.warning("HomeKit activation failed, degrading to cloud-only: %s", e)

    # After activation so a setup-time reconnect loop does not fetch; after the re-subscribe callback.
    register_homekit_drop_fetch(coordinator, homekit_client)
    register_homekit_off_latch_listener(coordinator)


def register_bridge_devices(
    hass: HomeAssistant,
    entry_id: str,
    home_id: str,
    zones_info: list[dict[str, Any]],
    home_devices: list[dict[str, Any]] | None = None,
) -> None:
    """Pre-register each Tado bridge as its own device-registry entry, nested under the hub.

    Checks `home_devices` too: a relay-only zone never lists the bridge
    among its own devices, only at the home level. Call after
    `register_hub_device` so the hub's cached device id exists for
    `via_device_id` parenting.
    """
    from homeassistant.helpers import device_registry as dr

    from .const import TADO_BRIDGE_MODELS
    from .device_manager import get_bridge_device_info

    device_registry = dr.async_get(hass)
    seen_serials: set[str] = set()

    def _register(device_type: str, serial: str, device: dict[str, Any]) -> None:
        if device_type in TADO_BRIDGE_MODELS and serial and serial.upper() not in seen_serials:
            seen_serials.add(serial.upper())
            device_info = get_bridge_device_info(serial, home_id)
            device_info["model"] = device_type
            device_info["sw_version"] = device.get("currentFwVersion")
            device_registry.async_get_or_create(config_entry_id=entry_id, **device_info)
            _LOGGER.debug(
                "Setup: pre-registered Tado bridge %s (%s)",
                mask_serial(serial), device_type,
            )

    for zone in zones_info:
        for device in zone.get("devices") or []:
            _register(device.get("deviceType", ""), device.get("shortSerialNo", ""), device)
    for device in home_devices or []:
        if isinstance(device, dict):
            _register(device.get("deviceType", ""), device.get("shortSerialNo", ""), device)


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
