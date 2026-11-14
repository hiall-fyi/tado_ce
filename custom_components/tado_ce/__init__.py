"""Tado CE integration entry point: platform setup, multi-home, options reload."""

from __future__ import annotations

import functools
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv

from .config_manager import ConfigurationManager
from .const import (
    DATA_DIR,
    DOMAIN,
    EVENT_READY,
    SERVICE_NAMES,
    SERVICE_SET_CLIMATE_TIMER,
)
from .coordinator import TadoDataUpdateCoordinator
from .data_loader import DataLoader
from .device_manager import _hub_identifier, clear_cached_hub_device_id
from .entry_lifecycle import async_teardown_entry, new_homekit_holder, new_setup_handles
from .exceptions import TadoAuthError, TadoBlockedError
from .helpers import get_entity_zone_identity, get_entity_zone_ids, mask_home_id
from .migration import (
    async_migrate_entry as async_migrate_entry,
)
from .services import _async_register_services
from .zone_config_manager import ZoneConfigManager

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr

    from .coordinator import TadoConfigEntry

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

BASE_PLATFORMS = [
    Platform.SENSOR,
    Platform.CLIMATE,
    Platform.BINARY_SENSOR,
    Platform.WATER_HEATER,
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.NUMBER,
]
CALENDAR_PLATFORM = Platform.CALENDAR


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Tado CE component."""
    await _async_register_services(hass)
    return True


async def _async_init_data_layer(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_manager: ConfigurationManager,
    home_id: str | None,
) -> tuple[DataLoader, ZoneConfigManager, str, int, dict[str, Any]]:
    """Build the DataLoader, ZoneConfigManager, and per-entry components in one shot."""
    data_loader = DataLoader(home_id or "default", hass=hass)
    await data_loader.async_load_all_to_cache()

    zone_config_manager = ZoneConfigManager(hass, home_id or "default", data_loader)
    await zone_config_manager.async_load()
    _LOGGER.debug(
        "Setup: zone config manager loaded %d zone override(s)",
        len(zone_config_manager.zones),
    )

    overlay_mode = await data_loader.async_load_overlay_mode()
    _LOGGER.debug("Setup: overlay mode loaded: %s", overlay_mode)

    timer_duration = await data_loader.async_load_timer_duration()
    _LOGGER.debug(
        "Setup: timer duration loaded: %d minutes", timer_duration,
    )

    from .entry_lifecycle import async_create_entry_components

    try:
        components = await async_create_entry_components(
            hass, entry, config_manager, home_id, data_loader=data_loader,
        )
    except TadoAuthError as err:
        raise ConfigEntryNotReady(
            "Authentication failed during setup; check credentials",
        ) from err
    except TadoBlockedError as err:
        raise ConfigEntryNotReady(
            "Tado is blocking requests during setup; will retry",
        ) from err
    except (OSError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            "Failed to create entry components; will retry",
        ) from err

    return data_loader, zone_config_manager, overlay_mode, timer_duration, components


async def _async_init_optional_components(
    hass: HomeAssistant,
    config_manager: ConfigurationManager,
    data_loader: DataLoader,
    zone_config_manager: ZoneConfigManager,
    components: dict[str, Any],
    home_id: str | None,
    handles: SimpleNamespace | None = None,
) -> dict[str, Any]:
    """Initialise the four optional feature managers (Smart Comfort / heating cycle / preheat / state restore)."""
    from .setup_entry_helpers import (
        async_init_adaptive_preheat,
        async_init_heating_cycle,
        async_init_smart_comfort,
        async_init_state_restore,
    )

    smart_comfort = await async_init_smart_comfort(hass, data_loader, config_manager, home_id)
    heating_cycle = await async_init_heating_cycle(hass, config_manager, home_id)
    preheat = await async_init_adaptive_preheat(
        hass, config_manager,
        api_client=components["api_client"],
        data_loader=data_loader,
        zone_config_manager=zone_config_manager,
        home_id=home_id or "default",
    )
    # Preheat registered its listeners inside the initialiser, so a later raise must still reach its unload.
    if handles is not None:
        handles.preheat = preheat
    return {
        "smart_comfort_manager": smart_comfort,
        "heating_cycle_coordinator": heating_cycle,
        "adaptive_preheat_manager": preheat,
        "state_restore_manager": await async_init_state_restore(
            hass, config_manager, data_loader,
        ),
    }


async def _async_wire_and_start_coordinator(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    coordinator: TadoDataUpdateCoordinator,
    optional: dict[str, Any],
    overlay_mode: str,
    timer_duration: int,
    home_id: str | None,
    components: dict[str, Any] | None = None,
) -> None:
    """Connect back-refs, load persisted caches, run first poll, then log a setup summary."""
    if optional["adaptive_preheat_manager"] is not None:
        optional["adaptive_preheat_manager"].set_coordinator(coordinator)
    if optional["state_restore_manager"] is not None:
        optional["state_restore_manager"].set_coordinator(coordinator)

    if optional["heating_cycle_coordinator"]:
        from .setup_entry_helpers import schedule_heating_cycle_timeouts

        schedule_heating_cycle_timeouts(hass, coordinator, optional["heating_cycle_coordinator"])

    coordinator.overlay_mode = overlay_mode
    coordinator.timer_duration = timer_duration

    from .setup_entry_helpers import async_load_insight_history

    await async_load_insight_history(coordinator)

    # Outdoor temp history is eager-loaded so Weather
    # Compensation has a usable trace from the first poll.
    loaded_history = await coordinator.data_loader.async_load_outdoor_temp_history()
    coordinator._outdoor_temp_history = loaded_history
    coordinator._outdoor_temp_loaded = True
    _LOGGER.debug(
        "Setup: outdoor temp history loaded: %d reading(s)",
        len(loaded_history),
    )

    # Anomaly timers + humidity history are eager-loaded so the
    # Home Insights duration counters survive HA restarts.
    await coordinator.async_load_insight_runtime_state()
    await coordinator.async_load_zone_identity()
    await coordinator.async_load_zone_type_baseline()

    saved = await coordinator.data_loader.async_load_homekit_savings()
    coordinator.apply_saved_homekit_savings(saved)

    # Loaded before the first poll so that poll's sweep can judge a change still owed across the restart.
    coordinator.write_ledger.from_store(await coordinator.data_loader.async_load_write_ledger())

    # Loads the saved write timing counters when the option is on, deletes them when it is off.
    await coordinator.write_timing.async_load()

    await coordinator.async_config_entry_first_refresh()

    # Initialize Smart Valve Controllers for eligible zones
    await coordinator.async_init_valve_controllers()

    # User-facing summary line, once-per-startup so users know
    # at a glance what's enabled and how often Tado CE polls.
    zones_info = coordinator.data.get("zones_info") or []
    zone_count = len(zones_info)
    hk_status = "connected" if (coordinator.homekit_provider and coordinator.homekit_provider.is_connected) else "off"
    weather_status = "on" if coordinator.config_manager.get_weather_enabled() else "off"
    interval = int(coordinator.update_interval.total_seconds() // 60) if coordinator.update_interval else "?"
    _LOGGER.info(
        "Setup: ready: %d zone(s), HomeKit=%s, weather=%s, "
        "polling every %s minute(s)",
        zone_count, hk_status, weather_status, interval,
    )

    # Deferred HomeKit mapping rebuild (if empty mapping at setup time)
    if components is not None:
        deferred_rebuild = components.get("_deferred_homekit_rebuild")
        if deferred_rebuild is not None:
            await deferred_rebuild(coordinator)

    from .setup_entry_helpers import register_hub_device

    register_hub_device(hass, entry.entry_id, coordinator.home_id)
    entry.async_on_unload(
        functools.partial(clear_cached_hub_device_id, coordinator.home_id),
    )

    zones_info = coordinator.data.get("zones_info") or []
    home_devices = coordinator.data.get("home_devices") or []
    if zones_info or home_devices:
        from .setup_entry_helpers import register_bridge_devices

        register_bridge_devices(
            hass, entry.entry_id, coordinator.home_id, zones_info, home_devices,
        )

    entry.runtime_data = coordinator
    _LOGGER.debug(
        "Setup: coordinator stored on entry %s (home_id=%s)",
        entry.entry_id, mask_home_id(home_id),
    )


async def _async_finalize_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: TadoDataUpdateCoordinator,
    config_manager: ConfigurationManager,
) -> None:
    """Forward platforms, ensure services exist, and wire the options-update listener."""
    platforms_to_load = list(BASE_PLATFORMS)
    if CALENDAR_PLATFORM and config_manager.get_schedule_calendar_enabled():
        platforms_to_load.append(CALENDAR_PLATFORM)
        _LOGGER.debug(
            "Setup: Schedule Calendar enabled, calendar platform "
            "will be loaded",
        )

    coordinator.entity_zone_ids = get_entity_zone_ids(coordinator.data)
    coordinator.entity_zone_identity = get_entity_zone_identity(coordinator.data)
    await hass.config_entries.async_forward_entry_setups(entry, platforms_to_load)
    coordinator.loaded_platforms = frozenset(platforms_to_load)

    if not hass.services.has_service(DOMAIN, SERVICE_SET_CLIMATE_TIMER):
        await _async_register_services(hass)

    _last_options_snapshot[entry.entry_id] = dict(entry.options)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))


async def async_setup_entry(hass: HomeAssistant, entry: TadoConfigEntry) -> bool:
    """Set up one Tado CE config entry, full setup pipeline."""
    _LOGGER.info(
        "Setup: starting entry %s (schema version %s, home_id %s)",
        entry.entry_id,
        entry.version,
        mask_home_id(entry.data.get("home_id")),
    )

    # Check for duplicate entries and remove old ones
    from .migration import async_deduplicate_entries

    should_continue = await async_deduplicate_entries(hass, entry)
    if not should_continue:
        return False

    from .migration import async_migrate_registry

    await async_migrate_registry(hass, entry)

    from .repair_helpers import (
        UNSUPPORTED_UPGRADE_PATH_ISSUE,
        async_dismiss_issue,
        async_dismiss_legacy_sensor_missing_issues,
    )

    async_dismiss_issue(
        hass,
        UNSUPPORTED_UPGRADE_PATH_ISSUE,
        home_id=entry.data.get("home_id"),
    )
    # Repair id gained the home id; clear one left under the old unscoped id.
    async_dismiss_legacy_sensor_missing_issues(hass)

    def _ensure_data_dir() -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            _LOGGER.warning(
                "Setup: could not create DATA_DIR. Tado CE data "
                "files cannot be persisted, will retry on next "
                "reload",
                exc_info=True,
            )

    await hass.async_add_executor_job(_ensure_data_dir)

    config_manager = ConfigurationManager(entry, hass)
    _LOGGER.debug(
        "Setup: configuration loaded: %s",
        config_manager.get_all_config(),
    )

    home_id = entry.data.get("home_id")

    old_coordinator = getattr(entry, "runtime_data", None)
    if isinstance(old_coordinator, TadoDataUpdateCoordinator):
        _LOGGER.warning(
            "Setup: entry %s is restarting, previous session still "
            "active, the new setup will replace it",
            entry.entry_id,
        )

    data_loader, zone_config_manager, overlay_mode, timer_duration, components = (
        await _async_init_data_layer(hass, entry, config_manager, home_id)
    )

    # The client is live from here, so a setup failure before the coordinator exists must still shut it down.
    handles = new_setup_handles(
        new_homekit_holder(components.get("homekit_client"), components.get("homekit_controller")),
        components.get("api_tracker"),
    )

    # A failed setup never reaches async_unload_entry, so HA's unload callback is its only teardown; after a clean unload it does nothing.
    async def _async_release_setup() -> None:
        await async_teardown_entry(handles)

    entry.async_on_unload(_async_release_setup)

    config_cached = data_loader.get_cached("config")
    if not config_cached:
        _LOGGER.debug(
            "Setup: config cache empty for home %s, will populate "
            "on first sync",
            mask_home_id(home_id) if home_id else "default",
        )

    optional = await _async_init_optional_components(
        hass, config_manager, data_loader, zone_config_manager, components, home_id,
        handles,
    )

    coordinator = TadoDataUpdateCoordinator(
        hass,
        entry,
        config_manager=config_manager,
        zone_config_manager=zone_config_manager,
        data_loader=data_loader,
        api_client=components["api_client"],
        api_tracker=components["api_tracker"],
        smart_comfort_manager=optional["smart_comfort_manager"],
        heating_cycle_coordinator=optional["heating_cycle_coordinator"],
        adaptive_preheat_manager=optional["adaptive_preheat_manager"],
        state_restore_manager=optional["state_restore_manager"],
    )
    handles.coordinator = coordinator
    coordinator.setup_handles = handles

    homekit_client = components.get("homekit_client")
    if homekit_client is not None:
        from .homekit_provider import HomeKitLocalProvider
        from .state_reconciler import StateReconciler

        coordinator.homekit_client = homekit_client
        coordinator.homekit_controller = components.get("homekit_controller")
        # Swapped only once it holds the client: a failure before this point still tears down the original holder.
        handles.homekit = coordinator
        provider = HomeKitLocalProvider(homekit_client, hass, coordinator.home_id)
        provider.put_rejection_listener = coordinator.record_homekit_put_rejection
        provider.change_listener = coordinator.note_homekit_change
        coordinator.homekit_provider = provider
        coordinator.state_reconciler = StateReconciler()

        from .setup_entry_helpers import async_start_homekit

        # On bridge reconnect we re-subscribe events and reset the
        # write-health circuit breaker so the integration recovers
        # cleanly from a transient bridge drop.
        async def _on_homekit_reconnect() -> None:
            await provider.async_refresh_accessories()
            await provider.async_subscribe_events()
            coordinator.reset_write_health()
            coordinator._reset_write_metrics()
            coordinator.reset_mapping_retry_budget()
            _LOGGER.info(
                "Setup: HomeKit reconnect handled, events "
                "re-subscribed, write-health circuit breaker reset",
            )

        await async_start_homekit(coordinator, provider, homekit_client, _on_homekit_reconnect)

    await _async_wire_and_start_coordinator(
        hass, entry, coordinator, optional, overlay_mode, timer_duration, home_id,
        components=components,
    )

    await _async_finalize_entry(hass, entry, coordinator, config_manager)

    _async_schedule_ready_event(hass, coordinator, entry)

    _LOGGER.info(
        "Setup: entry %s loaded successfully", entry.entry_id,
    )
    return True


def _async_schedule_ready_event(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    entry: ConfigEntry,
) -> None:
    """Fire `tado_ce_ready` once HA itself is running so boot-time automation triggers can catch it."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState, callback

    fired = False

    @callback
    def _fire(_event: object | None = None) -> None:
        nonlocal fired
        fired = True
        zones_info = (coordinator.data or {}).get("zones_info") or []
        hass.bus.async_fire(EVENT_READY, {
            "home_id": coordinator.home_id,
            "entry_id": entry.entry_id,
            "zone_count": len(zones_info),
        })
        _LOGGER.debug(
            "Fired %s for home %s (%d zones), boot automations can now trigger",
            EVENT_READY,
            mask_home_id(coordinator.home_id),
            len(zones_info),
        )

    if hass.state is CoreState.running:
        _fire()
    else:
        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _fire)

        @callback
        def _release() -> None:
            # HA logs an exception when an already-fired once-listener is removed
            if not fired:
                unsub()

        entry.async_on_unload(_release)


# Tracks options per entry to distinguish real options changes from data-only mutations
_last_options_snapshot: dict[str, dict[str, Any]] = {}


# Keys that take effect at runtime via config_manager real-time reads.
# Changes to ONLY these keys skip the full integration reload.
_RUNTIME_ONLY_KEYS: frozenset[str] = frozenset({"quota_reserve_enabled"})


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry only when options actually changed.

    HA fires the update listener for any ConfigEntry mutation
    (token rotation included), so we compare against the
    snapshot taken at setup. Runtime-only keys
    (`quota_reserve_enabled`) are read live by ConfigManager
    and don't justify the expensive full reload.
    """
    prev_options = _last_options_snapshot.get(entry.entry_id)
    current_options = dict(entry.options)

    if prev_options is not None and prev_options == current_options:
        _LOGGER.debug(
            "Reload: entry data updated but options unchanged "
            "(probably token rotation), skipping reload",
        )
        return

    _last_options_snapshot[entry.entry_id] = current_options

    changed_keys: set[str] = set()
    if prev_options is not None:
        all_keys = set(prev_options) | set(current_options)
        for key in all_keys:
            if prev_options.get(key) != current_options.get(key):
                changed_keys.add(key)

    if changed_keys and changed_keys <= _RUNTIME_ONLY_KEYS:
        _LOGGER.info(
            "Reload: runtime-only option(s) changed (%s), applied "
            "in memory, full reload skipped",
            ", ".join(sorted(changed_keys)),
        )
        return

    _LOGGER.info("Reload: options changed, reloading entry")

    from .entity_cleanup import cleanup_disabled_feature_entities

    try:
        cleanup_disabled_feature_entities(hass, entry)
    except (KeyError, TypeError, ValueError) as e:
        _LOGGER.warning(
            "Reload: entity cleanup pass failed (%s), reload will "
            "continue but disabled-feature entities may linger until "
            "the next reload",
            e,
        )

    await hass.config_entries.async_reload(entry.entry_id)


def _unregister_all_services(hass: HomeAssistant) -> None:
    """Drop every Tado CE service when the final config entry unloads."""
    for service_name in SERVICE_NAMES:
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)
    _LOGGER.debug("Unload: dropped all Tado CE services")


async def async_unload_entry(hass: HomeAssistant, entry: TadoConfigEntry) -> bool:
    """Unload one Tado CE config entry's platforms and services; HA then runs the teardown registered at setup."""
    _LOGGER.info("Unload: starting for entry %s", entry.entry_id)

    coordinator: TadoDataUpdateCoordinator | None = getattr(entry, "runtime_data", None)

    config_manager = getattr(coordinator, "config_manager", None) if coordinator else None
    platforms_to_unload = list(BASE_PLATFORMS)
    if coordinator and hasattr(coordinator, "loaded_platforms"):
        platforms_to_unload = list(coordinator.loaded_platforms)
    elif CALENDAR_PLATFORM and config_manager and config_manager.get_schedule_calendar_enabled():
        platforms_to_unload.append(CALENDAR_PLATFORM)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms_to_unload)

    if not unload_ok:
        # HA marks the entry FAILED_UNLOAD and refuses any later unload or reload, so everything keeps running until HA restarts; services and the options snapshot stay.
        _LOGGER.warning("Unload: entry %s could not unload its platforms and stays loaded", entry.entry_id)
        return unload_ok

    # Awaited here so the next setup cannot start while teardown runs; HA's later unload callback then finds nothing left to do.
    if coordinator is not None:
        await async_teardown_entry(coordinator.setup_handles)

    other_entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id]
    if len(other_entries) == 0:
        _unregister_all_services(hass)

    _last_options_snapshot.pop(entry.entry_id, None)

    _LOGGER.info(
        "Unload: entry %s unloaded successfully", entry.entry_id,
    )
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up Repair issues when the config entry is removed."""
    from .repair_helpers import async_dismiss_home_issues
    from .write_timing import async_remove_saved_counts

    home_id = entry.data.get("home_id")
    async_dismiss_home_issues(hass, home_id)
    # Last, so a failed delete cannot skip the dismissals above.
    await async_remove_saved_counts(hass, home_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: TadoConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow HA to remove a device only when it doesn't correspond to a live zone or hub.

    Returns False for the hub device (always pinned) and for
    zone devices whose zone_id still appears in the cloud
    response; True for stale zones, schedule devices, and
    bridges so the user can prune them from the registry.
    """
    coordinator: TadoDataUpdateCoordinator | None = getattr(config_entry, "runtime_data", None)
    if coordinator is None:
        return False

    home_id = str(coordinator.home_id)

    # Hub device: never removable while entry exists
    hub_identifier = _hub_identifier(home_id)
    if (DOMAIN, hub_identifier) in device_entry.identifiers:
        return False

    zones_info = coordinator.data.get("zones_info") or []
    active_zone_ids = {str(z.get("id")) for z in zones_info}

    for identifier in device_entry.identifiers:
        if identifier[0] != DOMAIN:
            continue
        value = identifier[1]

        prefix = f"tado_ce_{home_id}_zone_"
        if value.startswith(prefix):
            zone_id = value[len(prefix) :]
            return zone_id not in active_zone_ids  # True = stale zone, safe to remove

    # Schedule device, bridge devices, or any other: allow removal
    return True
