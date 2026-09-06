"""Tado CE HomeKit client: pairing, connection lifecycle, exponential-backoff reconnect."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import TYPE_CHECKING, Any, Final

from aiohomekit.exceptions import (
    AccessoryDisconnectedError,
    AccessoryNotFoundError,
    AlreadyPairedError,
    AuthenticationError,
    BackoffError,
    BusyError,
    HomeKitException,
    MaxPeersError,
    MaxTriesError,
    TransportNotSupportedError,
    UnavailableError,
)
from aiohomekit.exceptions import (
    TimeoutError as HKTimeoutError,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .helpers import mask_home_id
from .repair_helpers import (
    async_create_homekit_pairing_invalid_issue,
    async_dismiss_homekit_pairing_invalid_issue,
)
from .write_optimizer import _log_task_exception

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant

    from .config_flow_options import TadoCEOptionsFlow

_LOGGER = logging.getLogger(__name__)

# Reconnection backoff schedule (seconds)
_BACKOFF_SCHEDULE: Final[tuple[float, ...]] = (5.0, 10.0, 30.0, 60.0, 300.0)

# HomeKit characteristic UUIDs (short form)
CHAR_CURRENT_TEMPERATURE: Final = "11"
CHAR_CURRENT_HUMIDITY: Final = "10"
CHAR_TARGET_TEMPERATURE: Final = "35"
CHAR_CURRENT_HEATING_STATE: Final = "0F"
CHAR_TARGET_HEATING_STATE: Final = "33"
CHAR_SERIAL_NUMBER: Final = "30"
CHAR_MODEL: Final = "21"
CHAR_IDENTIFY: Final = "14"

_STORE_VERSION = 1


def _is_tado_bridge(category: Any, name: str) -> bool:
    """Return True when a discovered HomeKit device is a tado Internet Bridge.

    The pairing scan must target the tado bridge specifically. A home can have
    other unpaired HomeKit accessories (bulbs, plugs) advertising themselves at
    the same time; firing the tado PIN at one of those fails as a wrong-code
    error. A tado bridge reports the bridge category and a name beginning
    "tado", which together exclude both other-category devices and other-vendor
    bridges.
    """
    from aiohomekit.model.categories import Categories

    return category == Categories.BRIDGE and "tado" in (name or "").lower()


def _bridgeless_home(zones_info: list[dict[str, Any]]) -> bool:
    """Return True only when zones positively show devices and none is a Tado bridge.

    Fails open on missing/stale `zones_info` so a heating home isn't
    blocked by data that hasn't loaded yet.
    """
    from .const import TADO_BRIDGE_MODELS

    saw_a_device = False
    for zone in zones_info:
        for device in zone.get("devices") or []:
            saw_a_device = True
            if device.get("deviceType") in TADO_BRIDGE_MODELS:
                return False
    return saw_a_device


async def async_create_controller(hass: HomeAssistant) -> Any:
    """Build and start an aiohomekit Controller on the shared zeroconf instance.

    The aiohomekit import triggers lark grammar loading (blocking file I/O),
    so the import runs off the event loop. The returned controller is already
    started, its _hap browser begins warming the discovery cache immediately.
    """
    from homeassistant.components.zeroconf import async_get_async_instance

    zeroconf = await async_get_async_instance(hass)

    def _create_controller() -> Any:
        from aiohomekit import Controller
        return Controller

    controller_cls = await hass.async_add_executor_job(_create_controller)
    controller = controller_cls(
        async_zeroconf_instance=zeroconf,
        char_cache={},
    )
    await controller.async_start()
    return controller


class HomeKitClient:
    """Per-home HomeKit connection wrapper around aiohomekit's Controller + IpPairing."""

    def __init__(
        self,
        hass: HomeAssistant,
        home_id: str,
        pairing_path: Path | None = None,
        controller: Any | None = None,
    ) -> None:
        """Initialise the client. `pairing_path` is accepted for legacy compatibility but ignored.

        `controller` is an optional shared, already-started aiohomekit
        Controller. When supplied, this client uses it and never builds or
        stops its own, the owner (the entry) manages its lifecycle.
        """
        self._hass = hass
        self._home_id = home_id
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORE_VERSION, f"tado_ce/homekit_pairing_{home_id}",
        )
        from .const import get_data_file

        self._old_pairing_path = get_data_file("homekit_pairing", home_id)
        self._controller: Any | None = controller  # aiohomekit Controller
        # True only when this client built the controller itself and is
        # therefore responsible for stopping it. False for an injected
        # shared controller (the entry owns that one).
        self._owns_controller: bool = controller is None
        self._pairing: Any | None = None  # aiohomekit IpPairing
        self._is_connected = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closing = False
        self._unsub_config_changed: Any | None = None
        self._config_changed_task: asyncio.Task[None] | None = None
        self._spin_recovery_task: asyncio.Task[None] | None = None

        self._serial_to_zone: dict[str, str] = {}
        self._zone_to_aids: dict[str, list[int]] = {}

        self._last_connected: str | None = None
        self._last_disconnected: str | None = None
        self._reconnect_count = 0

        # Reconnect callbacks (e.g. re-subscribe events, reset circuit breaker)
        self._on_reconnect_callbacks: list[Any] = []

    @property
    def is_connected(self) -> bool:
        """Return True when the HomeKit pairing is currently connected."""
        return self._is_connected

    @property
    def pairing(self) -> Any | None:
        """Return the underlying aiohomekit pairing object (None until connected)."""
        return self._pairing

    @property
    def connection_stats(self) -> dict[str, Any]:
        """Return connection metrics for the HomeKit-status sensor."""
        return {
            "last_connected": self._last_connected,
            "last_disconnected": self._last_disconnected,
            "reconnect_count": self._reconnect_count,
        }

    def set_zone_mapping(
        self,
        serial_to_zone: dict[str, str],
        zone_to_aids: dict[str, list[int]],
    ) -> None:
        """Wire up the serial-to-zone and zone-to-aids dictionaries."""
        self._serial_to_zone = serial_to_zone
        self._zone_to_aids = zone_to_aids

    def remove_zone_mapping(self, zone_id: str) -> tuple[dict[str, str], dict[str, list[int]]]:
        """Remove one zone from the live mapping; returns the resulting pair for persistence."""
        self._zone_to_aids.pop(zone_id, None)
        self._serial_to_zone = {
            serial: zid for serial, zid in self._serial_to_zone.items() if zid != zone_id
        }
        return dict(self._serial_to_zone), dict(self._zone_to_aids)

    def add_reconnect_callback(self, callback: Any) -> None:
        """Register an async callback to await after each successful reconnect."""
        self._on_reconnect_callbacks.append(callback)

    @property
    def zone_aid_map(self) -> dict[str, list[int]]:
        """Return a copy of the zone-to-accessory-IDs mapping."""
        return dict(self._zone_to_aids)

    def get_aids_for_zone(self, zone_id: str) -> list[int]:
        """Return the accessory IDs mapped to one zone, or [] when none."""
        return self._zone_to_aids.get(zone_id, [])

    async def _ensure_controller(self) -> Any:
        """Return the aiohomekit Controller, building one off the loop only if not injected."""
        if self._controller is None:
            self._controller = await async_create_controller(self._hass)
            self._owns_controller = True
        return self._controller

    async def async_connect(self) -> bool:
        """Connect using stored pairing credentials, returning success."""
        pairing_data: dict[str, Any] | list[Any] | None = await self._store.async_load()
        if not pairing_data or not isinstance(pairing_data, dict):
            from .storage import async_migrate_json_to_store

            pairing_data = await async_migrate_json_to_store(
                self._hass, self._old_pairing_path, self._store,
                label="homekit_pairing",
            )
        if not pairing_data or not isinstance(pairing_data, dict):
            _LOGGER.debug(
                "HomeKit: no pairing credentials stored, staying "
                "disconnected, pair the bridge from Options to enable "
                "local control",
            )
            return False

        try:
            controller = await self._ensure_controller()
            alias = f"tado_ce_{self._home_id}"
            if self._pairing is not None:
                await self._shutdown_invalid_pairing()
            self._pairing = controller.load_pairing(alias, pairing_data)
            if self._pairing is None:
                _LOGGER.warning(
                    "HomeKit: aiohomekit returned no pairing for home %s, "
                    "stored credentials may be corrupt, re-pair to recover",
                    mask_home_id(self._home_id),
                )
                return False

            # Probe with list-accessories so a connection that can't
            # talk to the bridge fails the connect, not the first
            # data read.
            await self._pairing.list_accessories_and_characteristics()
            self._is_connected = True
            self._register_config_changed_listener()
            self._last_connected = dt_util.utcnow().isoformat()
            # A successful probe is proof the pairing is valid again, so
            # clear any stale "pairing invalid" issue too.
            async_dismiss_homekit_pairing_invalid_issue(self._hass, self._home_id)
            _LOGGER.info(
                "HomeKit: connected to bridge for home %s",
                mask_home_id(self._home_id),
            )
            return True
        except AuthenticationError as err:
            await self._handle_homekit_pairing_invalid(err)
            return False
        except Exception:
            # Optional local-control path: any connect failure (network,
            # aiohomekit internals, missing zeroconf) degrades to cloud-only
            # and lets the reconnect loop retry. Broad catch is deliberate;
            # exc_info keeps the cause without crashing the read.
            _LOGGER.warning(
                "HomeKit: could not connect to bridge for home %s, "
                "check the bridge is reachable, will keep retrying in "
                "the background",
                mask_home_id(self._home_id),
            )
            _LOGGER.debug("HomeKit: connect error details", exc_info=True)
            # match _handle_homekit_pairing_invalid's convention: _shutdown_invalid_pairing
            # does not set this itself.
            self._is_connected = False
            await self._shutdown_invalid_pairing()
            return False

    async def async_has_pairing(self) -> bool:
        """Return True when a HomeKit pairing is stored for this home.

        Single source of truth for "is HomeKit paired?", reads the same
        Store the pairing is written to, so callers never drift from the
        real location (the bug this method exists to kill).
        """
        data = await self._store.async_load()
        return bool(data) and isinstance(data, dict)

    async def async_disconnect(self) -> None:
        """Disconnect, cancel any reconnect loop, and release the pairing."""
        self._closing = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Broad: cancellation-cleanup must not fail the disconnect.
                _LOGGER.debug(
                    "HomeKit: error while cancelling the reconnect task, "
                    "proceeding with disconnect anyway",
                    exc_info=True,
                )
            self._reconnect_task = None

        if self._config_changed_task and not self._config_changed_task.done():
            self._config_changed_task.cancel()
            try:
                await self._config_changed_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Broad: cancellation-cleanup must not fail the disconnect.
                _LOGGER.debug(
                    "HomeKit: error while cancelling the config-changed rebuild "
                    "task, proceeding with disconnect anyway",
                    exc_info=True,
                )
            self._config_changed_task = None

        if self._spin_recovery_task and not self._spin_recovery_task.done():
            self._spin_recovery_task.cancel()
            try:
                await self._spin_recovery_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Broad: cancellation-cleanup must not fail the disconnect.
                _LOGGER.debug(
                    "HomeKit: error while cancelling the spin-recovery task, "
                    "proceeding with disconnect anyway",
                    exc_info=True,
                )
            self._spin_recovery_task = None

        if self._pairing:
            try:
                await self._pairing.close()
            except (AccessoryDisconnectedError, HKTimeoutError, HomeKitException):
                _LOGGER.debug(
                    "HomeKit: error while closing the pairing, proceeding "
                    "with disconnect anyway",
                    exc_info=True,
                )
            self._teardown_config_changed_listener()
            self._pairing = None

        self._is_connected = False
        self._last_disconnected = dt_util.utcnow().isoformat()
        _LOGGER.debug(
            "HomeKit: disconnected from bridge for home %s",
            mask_home_id(self._home_id),
        )

    async def async_stop_controller(self) -> None:
        """Stop the aiohomekit controller, but only if this client owns it.

        An injected shared controller is left running, its owner (the
        entry) stops it. A self-built controller is stopped and cleared.
        """
        if self._controller is not None and self._owns_controller:
            try:
                await self._controller.async_stop()
            except HomeKitException:
                _LOGGER.debug(
                    "HomeKit: error stopping controller, proceeding",
                    exc_info=True,
                )
            self._controller = None

    async def async_reconnect(self) -> None:
        """Schedule a background reconnect with exponential backoff (idempotent)."""
        if self._closing:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Reconnect indefinitely, backing off per `_BACKOFF_SCHEDULE` with jitter."""
        self._is_connected = False
        self._last_disconnected = dt_util.utcnow().isoformat()
        backoff_idx = 0

        while not self._closing:
            max_delay = _BACKOFF_SCHEDULE[min(backoff_idx, len(_BACKOFF_SCHEDULE) - 1)]
            delay = random.uniform(0, max_delay)
            _LOGGER.debug(
                "HomeKit: reconnect attempt %d in %.1fs for home %s",
                backoff_idx + 1, delay, mask_home_id(self._home_id),
            )
            await asyncio.sleep(delay)

            if self._closing:
                return

            try:
                success = await self.async_connect()
                if success:
                    self._reconnect_count += 1
                    _LOGGER.info(
                        "HomeKit: reconnected to bridge for home %s "
                        "after %d attempt(s)",
                        mask_home_id(self._home_id), backoff_idx + 1,
                    )
                    await self._run_reconnect_callbacks()
                    return
            except AuthenticationError as err:
                await self._handle_homekit_pairing_invalid(err)
                return  # stop the loop: retrying is pointless
            except Exception:
                # Detached background task: an uncaught exception would kill
                # it and stop reconnecting forever, so catch to keep it
                # backing off. Broad catch is deliberate; warning (not debug)
                # so a programmer bug still leaves a trace, not a dead task.
                _LOGGER.warning(
                    "HomeKit: reconnect attempt %d failed for home %s, "
                    "will back off and retry",
                    backoff_idx + 1, mask_home_id(self._home_id),
                )
                _LOGGER.debug(
                    "HomeKit: reconnect attempt error details", exc_info=True,
                )

            backoff_idx += 1

    async def _run_reconnect_callbacks(self) -> None:
        """Await every post-reconnect callback, isolating each so one failure can't abort the rest."""
        for cb in self._on_reconnect_callbacks:
            try:
                await cb()
            except Exception:
                # Callbacks are injected by other subsystems, so isolate
                # each: one failing must not abort the rest or the
                # reconnect. Broad catch is deliberate.
                _LOGGER.warning(
                    "HomeKit: post-reconnect setup callback failed, local "
                    "control may degrade until the next reconnect cycle",
                )
                _LOGGER.debug(
                    "HomeKit: post-reconnect error details", exc_info=True,
                )

    async def async_pair(
        self,
        pin: str,
        bridge_hkid: str | None = None,
    ) -> dict[str, Any]:
        """Run the HomeKit two-step pairing flow with `pin`, returning the credentials."""
        controller = await self._ensure_controller()

        if bridge_hkid is None:
            from aiohomekit.model.status_flags import StatusFlags

            saw_paired_bridge = False
            async for discovery in controller.async_discover():
                desc = discovery.description
                if not _is_tado_bridge(desc.category, desc.name):
                    continue
                if desc.status_flags & StatusFlags.UNPAIRED:
                    bridge_hkid = desc.id
                    _LOGGER.info(
                        "HomeKit: found unpaired bridge %s on the network",
                        bridge_hkid,
                    )
                    break
                saw_paired_bridge = True

            # Typed so the options flow maps these onto its "already paired"
            # and "could not reach the bridge" messages.
            if bridge_hkid is None:
                if saw_paired_bridge:
                    raise AlreadyPairedError(  # type: ignore[no-untyped-call]
                        "Found a HomeKit bridge but it is already paired. "
                        "Reset the bridge (hold the reset button until the "
                        "LED flashes) before pairing again.",
                    )
                raise AccessoryNotFoundError(  # type: ignore[no-untyped-call]
                    "No unpaired tado Internet Bridge found. Put the bridge "
                    "in pairing mode (the LED flashes about 5 times after a "
                    "reset) and make sure it is on the same network as Home "
                    "Assistant. Smart AC Control units pair separately and "
                    "are not handled here.",
                )

        discovery = await controller.async_find(bridge_hkid)
        alias = f"tado_ce_{self._home_id}"
        finish_pairing = await discovery.async_start_pairing(alias)
        pairing = await finish_pairing(pin)

        pairing_data = dict(pairing._pairing_data)
        await self._store.async_save(pairing_data)

        self._pairing = pairing
        self._is_connected = True
        self._last_connected = dt_util.utcnow().isoformat()
        _LOGGER.info(
            "HomeKit: pairing successful for home %s, local control enabled",
            mask_home_id(self._home_id),
        )

        return pairing_data

    async def async_unpair(self) -> None:
        """Remove the pairing from the bridge and delete the stored credentials."""
        if self._pairing:
            try:
                pairing_data = getattr(self._pairing, "pairing_data", None) or getattr(
                    self._pairing, "_pairing_data", None
                )
                pairing_id = pairing_data.get("iOSPairingId", "") if pairing_data else ""
                if pairing_id:
                    await self._pairing.remove_pairing(pairing_id)
                    _LOGGER.info(
                        "HomeKit: removed pairing from bridge (home %s)",
                        mask_home_id(self._home_id),
                    )
                elif pairing_data is None:
                    _LOGGER.warning(
                        "HomeKit: could not read pairing data while "
                        "unpairing, the bridge may keep a stale "
                        "pairing entry until you reset the bridge",
                    )
            except HomeKitException:
                _LOGGER.warning(
                    "HomeKit: bridge refused the unpair request, local "
                    "credentials cleared, but the bridge may still list "
                    "this pairing. Reset the bridge to clear it.",
                )
                _LOGGER.debug("HomeKit: unpair error details", exc_info=True)
            finally:
                await self.async_disconnect()

        async_dismiss_homekit_pairing_invalid_issue(self._hass, self._home_id)
        await self._store.async_remove()
        _LOGGER.info(
            "HomeKit: deleted local pairing credentials for home %s",
            mask_home_id(self._home_id),
        )

        from .homekit_mapping import remove_device_mapping

        await remove_device_mapping(self._hass, self._home_id)

    async def async_list_accessories(self) -> list[dict[str, Any]]:
        """Return every accessory the bridge advertises, or [] when unavailable."""
        if not self._pairing:
            return []
        try:
            result = await self._pairing.list_accessories_and_characteristics()
            if isinstance(result, list):
                _LOGGER.debug(
                    "HomeKit: bridge listed %d accessory(ies)", len(result),
                )
                return result
            return []
        except Exception:
            # Broad on purpose, matching every other bridge-touching method
            # here: must degrade, not crash the whole config entry.
            _LOGGER.debug(
                "HomeKit: could not list accessories, connection may "
                "be unhealthy, returning empty list",
                exc_info=True,
            )
            return []

    async def _shutdown_invalid_pairing(self) -> None:
        """Irreversibly release a stale pairing so aiohomekit stops re-arming it.

        A bridge reset/eviction leaves the bridge advertising over zeroconf while
        tado_ce still holds the old pairing. aiohomekit's own connection reconnect
        loop keeps re-arming on every re-advertise (gated on the pairing's
        `_shutdown` flag), retrying pair-verify and failing auth every 1-2s.
        `pairing.shutdown()` sets that flag then closes the connection, which is
        what actually stops the re-arm; only a brand-new pairing object clears it.

        `shutdown()` -> `close()` -> `_stop_connector()` re-awaits the connector
        task that stored the AuthenticationError, so it re-raises that error out of
        shutdown(); the swallow keeps the setup path a degrade, not a crash (the
        flag is set before close() can raise, so the re-arm is stopped regardless).
        Idempotent: a second call after `_pairing` is cleared is a no-op, so the
        reconnect loop's repeated auth failures don't re-await a done connector.
        """
        self._teardown_config_changed_listener()
        if self._pairing is None:
            return
        try:
            await self._pairing.shutdown()
        except HomeKitException:
            _LOGGER.debug(
                "HomeKit: error while shutting down the invalid pairing, "
                "proceeding, the re-arm flag is already set",
                exc_info=True,
            )
        self._pairing = None

    def async_schedule_spin_recovery(self) -> None:
        """Schedule a single-flight cancel-then-shutdown-then-reconnect recovery.

        Called from a sync aiohomekit callback, so schedule rather than await.
        Mirrors `_schedule_config_changed_rebuild`'s shape for the same reason
        it exists.
        """
        if self._closing:
            return
        if self._spin_recovery_task is not None and not self._spin_recovery_task.done():
            return
        self._spin_recovery_task = self._hass.async_create_task(
            self._async_spin_recovery(), eager_start=False,
        )
        self._spin_recovery_task.add_done_callback(_log_task_exception)

    async def _async_spin_recovery(self) -> None:
        """Cancel the in-flight reconnect, shut the pairing down, then reconnect.

        Sequential, not concurrent: cancel-and-await `_reconnect_task` BEFORE
        `_shutdown_invalid_pairing()`, so nothing races the pairing this is
        about to tear down. This removes the race a concurrent version had,
        rather than detecting and reacting to it after the fact.
        """
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        if self._closing:
            # This task's OWN cancellation (e.g. by async_disconnect, parked on
            # the await above) surfaces via the same except clause as the
            # reconnect task's, so stop rather than run recovery after being
            # told to close.
            return
        # match _handle_homekit_pairing_invalid's convention: _shutdown_invalid_pairing
        # does not set this itself. self._closing is deliberately NOT set here.
        self._is_connected = False
        await self._shutdown_invalid_pairing()
        _LOGGER.warning(
            "HomeKit: bridge connection was cycling rapidly for home %s, forced a "
            "clean reconnect", mask_home_id(self._home_id),
        )
        await self.async_reconnect()

    async def _handle_homekit_pairing_invalid(self, err: Exception) -> None:  # noqa: ARG002
        """Tear the stale pairing down and surface a Repair issue when pairing is permanently invalid."""
        from homeassistant.helpers import issue_registry as ir

        from .const import DOMAIN
        issue_id = f"homekit_pairing_invalid_{self._home_id}" if self._home_id else "homekit_pairing_invalid"
        already_raised = ir.async_get(self._hass).async_get_issue(DOMAIN, issue_id) is not None
        if already_raised:
            _LOGGER.debug(
                "HomeKit: pairing still invalid for home %s. Repair issue already active",
                mask_home_id(self._home_id),
            )
        else:
            _LOGGER.warning(
                "HomeKit: pairing is no longer valid for home %s, "
                "bridge may have been factory-reset. Re-pair in "
                "Settings → Tado CE → Configure → Advanced Settings → "
                "HomeKit, using the Pair again option.",
                mask_home_id(self._home_id),
            )
        _LOGGER.debug("HomeKit: pairing invalid error details", exc_info=True)
        self._closing = True
        self._is_connected = False
        await self._shutdown_invalid_pairing()
        async_create_homekit_pairing_invalid_issue(self._hass, self._home_id)

    def _register_config_changed_listener(self) -> None:
        """(Re)arm the config-changed listener on the current live pairing."""
        if self._unsub_config_changed is not None:
            self._unsub_config_changed()
            self._unsub_config_changed = None
        if self._pairing is None:
            return
        self._unsub_config_changed = self._pairing.dispatcher_connect_config_changed(
            self._on_config_changed,
        )

    def _teardown_config_changed_listener(self) -> None:
        """Drop the config-changed listener; safe no-op when none is registered."""
        if self._unsub_config_changed is not None:
            self._unsub_config_changed()
            self._unsub_config_changed = None

    def _on_config_changed(self, config_num: int) -> None:  # noqa: ARG002
        """Handle a bridge config-number bump: log and schedule a single-flight rebuild.

        Sync by aiohomekit contract (invoked from a `for cb in listeners`
        loop); it must not await or mutate the listener set here, so it only
        logs and hands off to a scheduled task.
        """
        _LOGGER.info(
            "HomeKit: bridge reconfigured for home %s, rebuilding the local "
            "device map", mask_home_id(self._home_id),
        )
        self._schedule_config_changed_rebuild()

    def _schedule_config_changed_rebuild(self) -> None:
        """Schedule the rebuild once; a bump arriving mid-rebuild is subsumed.

        aiohomekit refreshes its own accessory model before firing the
        listener, so an in-flight rebuild already fetches the freshest model,
        a duplicate schedule would only race it.
        """
        if self._config_changed_task is not None and not self._config_changed_task.done():
            return
        self._config_changed_task = self._hass.async_create_task(
            self._async_config_changed_rebuild(),
        )

    async def _async_config_changed_rebuild(self) -> None:
        """Rebuild the local iid cache after a config-number bump, reusing the reconnect path."""
        if not self._is_connected:
            return
        await self._run_reconnect_callbacks()


# ---------------------------------------------------------------------------
# Config flow pairing steps (delegated from config_flow_options.py)
# ---------------------------------------------------------------------------


def _shared_controller(flow: TadoCEOptionsFlow) -> Any | None:
    """Return the entry's shared HomeKit controller if one exists, else None.

    When HomeKit is already enabled, the entry holds a warm long-lived
    controller on its coordinator (entry.runtime_data); reusing it means the
    pairing discovery cache is already populated. None when enabling from
    scratch, the client then self-builds and the warm-up loop covers it.
    """
    coordinator = getattr(flow.config_entry, "runtime_data", None)
    return getattr(coordinator, "homekit_controller", None)


# HomeKit setup code: 8 digits, shown as XXX-XX-XXX. Accept the code with or
# without dashes; aiohomekit's check_pin_format requires the dashed form.
_PIN_DIGITS_RE = re.compile(r"^\d{8}$")
_PIN_DASHED_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")


def _normalise_homekit_pin(raw: str) -> str | None:
    """Return the dashed XXX-XX-XXX form, or None if not a valid 8-digit code."""
    candidate = raw.strip()
    if _PIN_DASHED_RE.match(candidate):
        return candidate
    if _PIN_DIGITS_RE.match(candidate):
        return f"{candidate[:3]}-{candidate[3:5]}-{candidate[5:]}"
    return None


async def async_step_homekit_pairing(
    flow: TadoCEOptionsFlow,
    user_input: dict[str, Any] | None = None,
) -> ConfigFlowResult:
    """Drive the options-flow HomeKit pairing sub-step."""
    from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
    import voluptuous as vol

    errors: dict[str, str] = {}

    zones_info = flow.config_entry.runtime_data.data.get("zones_info") or []
    if _bridgeless_home(zones_info):
        if user_input is not None:
            # Acknowledged: cancel pairing the same way an empty PIN does,
            # since there's no bridge for this home to pair with.
            if flow._pending_general_options:
                flow._pending_general_options["homekit_enabled"] = False
                return flow.async_create_entry(title="", data=flow._pending_general_options)
            return await flow.async_step_init()
        return flow.async_show_form(
            step_id="homekit_pairing",
            data_schema=vol.Schema({}),
            errors={"base": "homekit_no_bridge_detected"},
        )

    if user_input is not None:
        raw = user_input.get("homekit_pin", "").strip()
        if not raw:
            # Empty PIN: cancel pairing and revert the homekit_enabled
            # flag so the options form reflects the actual state.
            if flow._pending_general_options:
                flow._pending_general_options["homekit_enabled"] = False
                return flow.async_create_entry(title="", data=flow._pending_general_options)
            return await flow.async_step_init()

        pin = _normalise_homekit_pin(raw)
        if pin is None:
            errors["homekit_pin"] = "homekit_pin_format"
        else:
            try:
                from .homekit_mapping import async_rebuild_and_save_mapping

                home_id = flow.config_entry.data.get("home_id") or "default"
                client = HomeKitClient(
                    flow.hass, home_id, controller=_shared_controller(flow),
                )

                try:
                    await client.async_pair(pin)

                    zones_info = flow.config_entry.runtime_data.data.get("zones_info") or []
                    await async_rebuild_and_save_mapping(
                        flow.hass, client, home_id, zones_info,
                    )
                    async_dismiss_homekit_pairing_invalid_issue(
                        flow.hass,
                        home_id=flow.config_entry.data.get("home_id") or "default",
                    )
                finally:
                    await client.async_disconnect()

                _LOGGER.info(
                    "HomeKit: pairing complete and mapping saved",
                )

                if flow._pending_general_options:
                    # First enable: writing the options flips homekit_enabled,
                    # and the update listener reloads the entry for us.
                    return flow.async_create_entry(title="", data=flow._pending_general_options)

                # Re-pair: pairing happened on a throwaway client, so the
                # entry's live client still holds whatever state it had, and
                # after a pairing-invalid teardown that state is permanently
                # inert (`_closing` latched, no pairing). This route returns to
                # the menu without writing options, so no update listener fires;
                # reload explicitly or the user keeps cloud-only state despite
                # valid new credentials.
                flow.hass.config_entries.async_schedule_reload(
                    flow.config_entry.entry_id,
                )
                return await flow.async_step_init()

            except AuthenticationError:
                errors["homekit_pin"] = "homekit_wrong_pin"
            except (MaxPeersError, UnavailableError, AlreadyPairedError):
                errors["homekit_pin"] = "homekit_already_paired"
            except AccessoryNotFoundError:
                errors["homekit_pin"] = "homekit_network_error"
            except HKTimeoutError:
                errors["homekit_pin"] = "homekit_timeout"
            except MaxTriesError:
                errors["homekit_pin"] = "homekit_max_tries"
            except (BackoffError, BusyError):
                errors["homekit_pin"] = "homekit_busy"
            except TransportNotSupportedError:
                # No _hap zeroconf browser on this host, so pairing can't
                # start. Name the real cause (missing zeroconf) instead of a
                # generic "pairing failed".
                errors["homekit_pin"] = "homekit_no_zeroconf"
                _LOGGER.warning(
                    "HomeKit: pairing unavailable, no zeroconf _hap browser "
                    "on this host. Add `default_config:` (or `zeroconf:`) to "
                    "configuration.yaml and restart.",
                )
            except Exception as err:
                # Final backstop after the typed catches above: a config flow
                # must never crash, it has to show the user a form with an
                # error, so any unmapped exception falls through to the
                # generic key. Broad catch is deliberate.
                errors["homekit_pin"] = "homekit_pairing_failed"
                _LOGGER.warning("HomeKit: pairing failed: %s", err)

    return flow.async_show_form(
        step_id="homekit_pairing",
        data_schema=vol.Schema(
            {
                vol.Required("homekit_pin"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT),
                ),
            },
        ),
        errors=errors,
    )


async def async_step_homekit_unpair(
    flow: TadoCEOptionsFlow,
    user_input: dict[str, Any] | None = None,
) -> ConfigFlowResult:
    """Drive the options-flow HomeKit unpairing sub-step.

    Performs the unpair and clears local credentials, even if the
    bridge can't be contacted (so a permanently-offline bridge
    doesn't trap the user with no recovery path).
    """
    import voluptuous as vol

    if user_input is not None:
        try:
            home_id = flow.config_entry.data.get("home_id") or "default"
            client = HomeKitClient(
                flow.hass, home_id, controller=_shared_controller(flow),
            )

            # Connect first so the unpair can also clear the bridge's
            # side of the pairing, best-effort, falls through on
            # failure.
            await client.async_connect()
            await client.async_unpair()

            _LOGGER.info("HomeKit: unpair successful")
        except HomeKitException:
            _LOGGER.warning(
                "HomeKit: unpair encountered errors, local credentials "
                "have been cleared, but the bridge may still list the "
                "pairing. Reset the bridge if you want to clear it.",
            )
            _LOGGER.debug("HomeKit: unpair error details", exc_info=True)

        new_options = dict(flow.config_entry.options)
        new_options["homekit_enabled"] = False

        coordinator = flow.config_entry.runtime_data
        if coordinator and hasattr(coordinator, "_pending_cleanup"):
            coordinator._pending_cleanup.setdefault(flow.config_entry.entry_id, {})["_cleanup_homekit"] = True

        return flow.async_create_entry(title="", data=new_options)

    return flow.async_show_form(
        step_id="homekit_unpair",
        data_schema=vol.Schema({}),
    )
