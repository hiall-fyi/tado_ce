"""Tado CE entry lifecycle: per-config-entry component setup + teardown."""

from __future__ import annotations

import functools
import inspect
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from .helpers import mask_serial_dict

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api_call_tracker import APICallTracker
    from .config_manager import ConfigurationManager
    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)


async def _async_check_cached_mapping(
    hass: HomeAssistant,
    home_id: str | None,
    data_loader: DataLoader | None,
    *,
    require_zones: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (mapping valid against the zones file, mapping stale only by device serials); require_zones refuses an unchecked mapping."""
    from .const import get_climate_zone_ids
    from .homekit_mapping import load_device_mapping, validate_mapping

    mapping = await load_device_mapping(hass, home_id or "default")
    if not mapping or not mapping.get("serial_to_zone"):
        return None, None

    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file) if data_loader else None
    if require_zones and not zones_info:
        return None, None
    valid_ids = get_climate_zone_ids(zones_info or []) if zones_info else None
    if validate_mapping(mapping, valid_zone_ids=valid_ids, zones_info=zones_info or None):
        return mapping, None
    _LOGGER.info(
        "Entry Lifecycle: HomeKit cached mapping no longer matches the cloud zone list, rebuilding from scratch",
    )
    # A mapping that is only stale by device serials keeps working if the rebuild finds nothing.
    return None, mapping if validate_mapping(mapping, valid_zone_ids=valid_ids) else None


async def _async_load_valid_cached_mapping(
    hass: HomeAssistant,
    home_id: str | None,
    data_loader: DataLoader | None,
) -> dict[str, Any] | None:
    """Return the saved HomeKit mapping when it still matches the cloud zones, else None."""
    return (await _async_check_cached_mapping(hass, home_id, data_loader, require_zones=True))[0]


def new_homekit_holder(client: Any = None, controller: Any = None) -> SimpleNamespace:
    """Hold what the HomeKit teardown reads until the coordinator exists to hold it."""
    return SimpleNamespace(
        homekit_client=client,
        homekit_controller=controller,
        homekit_provider=None,
        state_reconciler=None,
        cancel_cold_start_retry=lambda: None,
    )


def new_setup_handles(
    homekit: Any, api_tracker: APICallTracker | None = None, coordinator: Any = None, preheat: Any = None,
) -> SimpleNamespace:
    """Collect what setup starts so one teardown can release it, filled in as setup progresses."""
    return SimpleNamespace(
        homekit=homekit, api_tracker=api_tracker, coordinator=coordinator, preheat=preheat, torn_down=False,
    )


async def async_create_entry_components(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_manager: ConfigurationManager,
    home_id: str | None,
    data_loader: DataLoader | None = None,
) -> dict[str, Any]:
    """Build API tracker + client + optional HomeKit client; returns dict for the coordinator constructor."""
    started = new_homekit_holder()
    try:
        return await _async_build_entry_components(hass, entry, config_manager, home_id, data_loader, started)
    except BaseException:
        # Setup has no handle on a half-built result, so whatever it started is stopped here, cancellation included.
        try:
            await async_teardown_homekit(started)  # type: ignore[arg-type]
        except Exception:
            # The original failure is the one the caller must see.
            _LOGGER.warning("Entry Lifecycle: could not stop HomeKit after a failed setup", exc_info=True)
        raise


async def _async_build_entry_components(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config_manager: ConfigurationManager,
    home_id: str | None,
    data_loader: DataLoader | None,
    started: SimpleNamespace,
) -> dict[str, Any]:
    """Build the components, recording each HomeKit resource on `started` the moment it exists."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.util import dt as dt_util

    from .api_call_tracker import APICallTracker
    from .api_client import TadoApiClient
    from .cloud_health import CloudHealth
    retention_days = config_manager.get_api_history_retention_days()
    api_tracker = APICallTracker(
        hass, retention_days=retention_days, home_id=home_id, config_manager=config_manager,
    )
    await api_tracker.async_init()
    _LOGGER.debug("Entry Lifecycle: API call tracker ready")

    session = async_get_clientsession(hass)
    api_client = TadoApiClient(
        session,
        hass,
        home_id=home_id,
        refresh_token=entry.data.get("refresh_token", ""),
        api_tracker=api_tracker,
        data_loader=data_loader,
        config_entry=entry,
        cloud_health=CloudHealth(home_id, clock=dt_util.utcnow),
    )
    _LOGGER.debug("Entry Lifecycle: API client ready")

    # Load version early to avoid race conditions in device_manager
    from .device_manager import load_version

    await hass.async_add_executor_job(load_version)

    homekit_client = None
    homekit_controller = None
    if config_manager.get_homekit_enabled():
        from .homekit_client import HomeKitClient, async_create_controller

        # Build ONE long-lived controller for the entry and start it now,
        # so its _hap browser warms the discovery cache from setup (the
        # runtime client and the later pair/unpair flows all share it).
        # HomeKit local control is optional, so a controller that can't
        # start (e.g. no zeroconf _hap browser on the host) must degrade
        # to cloud-only, never crash the entry. Broad catch is deliberate.
        try:
            homekit_controller = await async_create_controller(hass)
        except Exception:
            # exc_info keeps the traceback visible at the default log level
            # so a genuine bug doesn't vanish into the cloud-only fallback.
            _LOGGER.warning(
                "Entry Lifecycle: HomeKit controller failed to start, "
                "continuing with cloud-only state. Local control is off "
                "until this is resolved (most often a missing zeroconf "
                "component on the host).",
                exc_info=True,
            )
            homekit_controller = None
        started.homekit_controller = homekit_controller
    else:
        from .repair_helpers import async_dismiss_homekit_pairing_invalid_issue

        async_dismiss_homekit_pairing_invalid_issue(hass, home_id or "default")

    if homekit_controller is not None:
        homekit_client = HomeKitClient(
            hass, home_id or "default", controller=homekit_controller,
        )
        started.homekit_client = homekit_client
        connected = await homekit_client.async_connect()
        if connected:
            _LOGGER.info("Entry Lifecycle: HomeKit bridge connected")
            from .homekit_mapping import (
                async_rebuild_and_save_mapping,
            )

            mapping, fallback_mapping = await _async_check_cached_mapping(
                hass, home_id, data_loader,
            )
            serial_to_zone = mapping.get("serial_to_zone", {}) if mapping else {}

            if not serial_to_zone:
                _LOGGER.debug(
                    "Entry Lifecycle: HomeKit mapping empty, "
                    "rebuilding from bridge accessories + cloud zones",
                )
                # zones_info: load from disk, coordinator not created yet
                if data_loader:
                    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)
                else:
                    zones_info = []
                mapping = await async_rebuild_and_save_mapping(
                    hass, homekit_client, home_id or "default", zones_info or [],
                )
                serial_to_zone = mapping.get("serial_to_zone", {})
                if not serial_to_zone and fallback_mapping is not None:
                    mapping = fallback_mapping
                    serial_to_zone = mapping.get("serial_to_zone", {})
                    # {} (not None) marks a legacy mapping stale so the per-poll retry rebuilds it
                    mapping.setdefault("cloud_serials", {})
                    _LOGGER.info(
                        "Entry Lifecycle: HomeKit rebuild found no accessories, "
                        "keeping the cached mapping until the next poll retries",
                    )

            if serial_to_zone:
                homekit_client.set_zone_mapping(
                    mapping.get("serial_to_zone", {}),  # type: ignore[union-attr]
                    mapping.get("zone_to_aids", {}),  # type: ignore[union-attr]
                    mapping.get("cloud_serials"),  # type: ignore[union-attr]
                )
                _LOGGER.info(
                    "Entry Lifecycle: HomeKit zone mapping loaded "
                    "(%d zone(s))",
                    len(serial_to_zone),
                )
                _LOGGER.debug(
                    "Entry Lifecycle: HomeKit mapping detail, "
                    "serial_to_zone=%s, zone_to_aids=%s",
                    mask_serial_dict(mapping.get("serial_to_zone", {})),  # type: ignore[union-attr]
                    mapping.get("zone_to_aids", {}),  # type: ignore[union-attr]
                )

                from .const import get_climate_zone_ids

                zi = await hass.async_add_executor_job(data_loader.load_zones_info_file) if data_loader else []
                all_climate_ids = get_climate_zone_ids(zi or [])
                mapped_ids = set(serial_to_zone.values())
                unmapped = all_climate_ids - mapped_ids
                if unmapped:
                    _LOGGER.info(
                        "Entry Lifecycle: HomeKit unmapped zone(s) "
                        "%s, those zones will use cloud-only state",
                        unmapped,
                    )
            else:
                _LOGGER.warning(
                    "Entry Lifecycle: HomeKit connected but no zone "
                    "mapping built yet, scheduling a deferred "
                    "rebuild after the first coordinator poll",
                )

                async def _deferred_homekit_rebuild(
                    coord: TadoDataUpdateCoordinator,
                    _hk_client: Any = homekit_client,
                    _hass: HomeAssistant = hass,
                    _home_id: str | None = home_id,
                ) -> None:
                    """Retry the HomeKit mapping build after the first poll lands."""
                    from .homekit_mapping import (
                        async_rebuild_and_save_mapping,
                        mapping_covers_all_zones,
                    )

                    zi = coord.data.get("zones_info") or []
                    if not zi:
                        _LOGGER.warning(
                            "Entry Lifecycle: HomeKit deferred "
                            "rebuild aborted, coordinator still has "
                            "no zones_info",
                        )
                        return
                    new_mapping = await async_rebuild_and_save_mapping(
                        _hass, _hk_client, _home_id or "default", zi,
                    )
                    s2z = new_mapping.get("serial_to_zone", {})
                    provider = coord.homekit_provider
                    # Activation subscribed while the map was empty, so push events never reached the zones
                    if s2z and provider is not None and provider.is_connected:
                        await provider.async_refresh_accessories()
                        await provider.async_subscribe_events()
                    if mapping_covers_all_zones(_hk_client.zone_aid_map, zi, _hk_client.cloud_serials):
                        _LOGGER.info(
                            "Entry Lifecycle: HomeKit deferred rebuild "
                            "complete: %d zone(s) mapped",
                            len(s2z),
                        )
                    else:
                        _LOGGER.debug(
                            "Entry Lifecycle: HomeKit deferred rebuild "
                            "incomplete, coordinator will retry each poll "
                            "until every zone is mapped",
                        )

                return {
                    "api_tracker": api_tracker,
                    "api_client": api_client,
                    "homekit_client": homekit_client,
                    "homekit_controller": homekit_controller,
                    "_deferred_homekit_rebuild": _deferred_homekit_rebuild,
                }
        else:
            _LOGGER.warning(
                "Entry Lifecycle: HomeKit bridge connection failed "
                ", continuing with cloud-only state (will keep "
                "retrying in the background)",
            )
            saved = await _async_load_valid_cached_mapping(hass, home_id, data_loader)
            if saved is not None:
                homekit_client.set_zone_mapping(
                    saved["serial_to_zone"], saved.get("zone_to_aids", {}), saved.get("cloud_serials"),
                )
                _LOGGER.info(
                    "Entry Lifecycle: HomeKit bridge unreachable, using the saved zone mapping "
                    "(%d zone(s)) until it answers",
                    len(set(saved["serial_to_zone"].values())),
                )
    return {
        "api_tracker": api_tracker,
        "api_client": api_client,
        "homekit_client": homekit_client,
        "homekit_controller": homekit_controller,
    }


async def async_teardown_homekit(coordinator: TadoDataUpdateCoordinator) -> None:
    """Shut the entry's HomeKit connection down; safe to call twice (a failed setup, then unload)."""
    # A failed setup never reaches the coordinator shutdown, so drop the catch-up timer here too.
    coordinator.cancel_cold_start_retry()

    hkc = getattr(coordinator, "homekit_client", None)
    if hkc is not None:
        from .homekit_client import HomeKitClient

        # Unsubscribe events before the connection goes, otherwise
        # in-flight events can hit a half-disconnected client.
        provider = getattr(coordinator, "homekit_provider", None)
        if provider is not None and hasattr(provider, "unsubscribe_events"):
            provider.unsubscribe_events()

        if isinstance(hkc, HomeKitClient):
            await hkc.async_disconnect()

    # A controller can be running without a client when building the client failed.
    # The client's async_disconnect leaves the shared controller alone because the entry owns it.
    ctrl = getattr(coordinator, "homekit_controller", None)
    if ctrl is not None:
        try:
            await ctrl.async_stop()
        except Exception:
            # Teardown must finish even if the controller stop fails.
            _LOGGER.debug(
                "Entry Lifecycle: error stopping HomeKit controller, proceeding",
                exc_info=True,
            )
        coordinator.homekit_controller = None

    coordinator.homekit_client = None
    coordinator.homekit_provider = None
    coordinator.state_reconciler = None
    _LOGGER.debug("Entry Lifecycle: HomeKit client torn down")


def cancel_heating_cycle_timer(coordinator: TadoDataUpdateCoordinator) -> None:
    """Stop the 60 s heating-cycle timer once; the handle is cleared so a second call is a no-op."""
    cancel_func: Callable[[], None] | None = getattr(coordinator, "_heating_cycle_timeout_cancel", None)
    if cancel_func:
        cancel_func()
        coordinator._heating_cycle_timeout_cancel = None
        _LOGGER.debug("Entry Lifecycle: cancelled heating cycle timeout timer")


def _cancel_refresh_handler(coordinator: TadoDataUpdateCoordinator) -> None:
    """Stop the debounced refresh queue and drop the handler."""
    handler = coordinator.refresh_handler
    if handler is not None:
        handler.cancel()
        coordinator.refresh_handler = None  # type: ignore[assignment]


async def _flush_insight_state(coordinator: TadoDataUpdateCoordinator) -> None:
    """Persist the insight runtime dicts once a poll has run; before that they were never read back and would wipe the saved ones."""
    if coordinator.data is not None:
        await coordinator.async_shutdown_insight_state()


def _drop_api_client(coordinator: TadoDataUpdateCoordinator) -> None:
    """Forget the cloud tokens and let go of the API client."""
    client = coordinator.api_client
    if client is not None:
        client._access_token = None
        client._token_expiry = None
        coordinator.api_client = None  # type: ignore[assignment]


def _save_smart_comfort(coordinator: TadoDataUpdateCoordinator) -> None:
    """Save Smart Comfort's debounced store now: HA's final write covers a shutdown but not a reload."""
    manager = coordinator.smart_comfort_manager
    if manager is not None:
        manager.save_to_file()
        coordinator.smart_comfort_manager = None


async def _unload_preheat(handles: SimpleNamespace) -> None:
    """Cancel Adaptive Preheat's listeners and timers."""
    if handles.preheat is not None:
        await handles.preheat.async_unload()
        if handles.coordinator is not None:
            handles.coordinator.adaptive_preheat_manager = None


async def _flush_stores(coordinator: TadoDataUpdateCoordinator) -> None:
    """Write every debounced store now (a reload skips HA's final write), then let go of the data loader."""
    if coordinator.data_loader is not None:
        await coordinator.async_flush_all_stores()
        coordinator.data_loader = None  # type: ignore[assignment]


async def async_teardown_entry(handles: SimpleNamespace) -> None:
    """Stop and flush everything setup started; a second call after a complete run does nothing."""
    if handles.torn_down:
        return
    coordinator = handles.coordinator
    api_tracker = handles.api_tracker
    # The lambdas defer the attribute lookup into the per-step try below, so a missing method is isolated too.
    steps: list[tuple[str, Callable[[], Any]]] = []
    if coordinator is not None:
        steps += [
            # First, so a call that finishes while the poll drains below cannot create an issue for an unloading entry.
            ("CloudHealth listener", lambda: coordinator.detach_cloud_health()),
            ("poll timer", lambda: coordinator.async_shutdown()),
            # Scheduled, debounced and refresh-handler polls all hold the refresh lock and a refresh after shutdown does nothing, so this returns once any poll in flight has finished.
            ("poll in flight", lambda: coordinator.async_refresh()),
            # After the drain: cancelling the handler first would cancel the poll it is awaiting.
            ("refresh handler", functools.partial(_cancel_refresh_handler, coordinator)),
        ]
    # Before any slow step below, so a start timer cannot send a PUT during teardown.
    steps.append(("Adaptive Preheat", functools.partial(_unload_preheat, handles)))
    if coordinator is not None:
        steps += [
            ("cold-start retry", lambda: coordinator.cancel_cold_start_retry()),
            ("write debouncer", lambda: coordinator.action_debouncer.cancel_all()),
            # Before the API client is dropped, so no held resend or resend task outlives the unload.
            ("held resends", lambda: coordinator.write_ledger.shutdown()),
            ("bridge poll", lambda: coordinator.cancel_bridge_poll()),
            ("device sync queue", lambda: coordinator.device_sync_queue.shutdown()),
            ("insight history", lambda: coordinator.insight_history.async_flush()),
            ("valve controllers", lambda: coordinator.async_shutdown_valve_controllers()),
        ]
    # After the valve hand-back, so its cloud calls are recorded.
    if api_tracker is not None:
        steps.append(("API call tracker", lambda: api_tracker.async_save_if_dirty()))
    if coordinator is not None:
        steps += [
            ("insight runtime state", functools.partial(_flush_insight_state, coordinator)),
            # Before the loader and client are released, so no HomeKit event can reach them half torn down.
            ("HomeKit", functools.partial(async_teardown_homekit, handles.homekit)),
            ("heating-cycle timer", functools.partial(cancel_heating_cycle_timer, coordinator)),
            ("API client", functools.partial(_drop_api_client, coordinator)),
            ("Smart Comfort", functools.partial(_save_smart_comfort, coordinator)),
        ]
    if coordinator is not None:
        steps += [
            # Its own step so a failure here still lets the store flush and the loader release run.
            ("mechanism counters", lambda: coordinator.flush_mechanism_counters()),
            ("stores", functools.partial(_flush_stores, coordinator)),
        ]
    if coordinator is None:
        steps.append(("HomeKit", functools.partial(async_teardown_homekit, handles.homekit)))
    for name, step in steps:
        try:
            result = step()
            if inspect.isawaitable(result):
                await result
        except Exception:
            # One failing step must not strand the others, and nothing can retry a teardown afterwards.
            _LOGGER.warning("Entry Lifecycle: error tearing down %s, continuing", name, exc_info=True)
    # Set only after a complete run, so a run cancelled midway is finished by the next call.
    handles.torn_down = True
