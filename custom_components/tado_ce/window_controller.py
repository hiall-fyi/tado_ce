"""Window control: switch heating rooms off while a window is open and put them back when it closes.

One controller per home, because a room set to "house" switches every heating
room off. Each room picks what its own window sensors do (`window_action`):
nothing, switch this room off, or switch every room off. The hub picks when
switched-off rooms come back (`window_resume_mode`): once the windows that
switched them off are closed, or only once no window is open anywhere.

A room is switched off through its climate entity, so the write takes the same
route as a tap on the card (HomeKit when connected), after State Restore has
captured what the room was doing. It comes back through the
`restore_previous_state` action, which puts back that captured schedule or
manual setting. A room someone changes while it is held off is left alone.
The set of rooms held off is saved, so a restart mid-airing still restores them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import HVACMode
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    DOMAIN,
    SERVICE_RESTORE_PREVIOUS_STATE,
    WINDOW_ACTION_HOUSE,
    WINDOW_ACTION_NONE,
    WINDOW_OFF_CAPTURE_SOURCE,
    WINDOW_RESUME_ALL,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

_STORE = "window_control"
# A failed write (Tado unreachable, quota reserve) is tried again after this long.
_RESTORE_RETRY_SECONDS = 300
# A room whose restore keeps failing is let go after this many tries (an hour at the retry interval).
_RESTORE_MAX_ATTEMPTS = 12


@dataclass(frozen=True)
class WindowView:
    """Pure inputs for one decision: which rooms do what, and which windows are open."""

    heating_zones: frozenset[str]
    actions: dict[str, str]  # zone -> window_action, only zones that have sensors
    open_zones: frozenset[str]  # zones with at least one window open now
    triggered: frozenset[str]  # open zones whose off delay has run out


@dataclass
class WindowState:
    """What the controller has done: rooms it holds off, and rooms a person took back."""

    held: set[str] = field(default_factory=set)
    overridden: set[str] = field(default_factory=set)


def targets_of(zone_id: str, action: str, heating_zones: frozenset[str]) -> frozenset[str]:
    """Rooms an open window in `zone_id` switches off."""
    if action == WINDOW_ACTION_HOUSE:
        return heating_zones
    if action == WINDOW_ACTION_NONE or zone_id not in heating_zones:
        return frozenset()
    return frozenset({zone_id})


def holders_of(target: str, view: WindowView) -> list[str]:
    """Triggered rooms whose window is what keeps `target` off."""
    return sorted(
        z for z in view.triggered
        if target in targets_of(z, view.actions.get(z, WINDOW_ACTION_NONE), view.heating_zones)
    )


def decide(view: WindowView, state: WindowState, *, enabled: bool, resume_mode: str) -> tuple[set[str], set[str]]:
    """Return (rooms to switch off, rooms to bring back). Pure, so every rule is testable on its own."""
    want_off: set[str] = set()
    if enabled:
        for zone in view.triggered:
            want_off |= targets_of(zone, view.actions.get(zone, WINDOW_ACTION_NONE), view.heating_zones)

    # A room taken back by hand stays alone until the windows holding it stop asking.
    to_off = want_off - state.held - state.overridden

    to_restore: set[str] = set()
    for zone in state.held - want_off:
        if enabled and resume_mode == WINDOW_RESUME_ALL and view.open_zones:
            continue  # "all": nothing comes back while any window is open
        to_restore.add(zone)
    return to_off, to_restore


class WindowController:
    """Per-home window control: listen to window sensors, switch rooms off, restore them."""

    def __init__(self, hass: HomeAssistant, coordinator: TadoDataUpdateCoordinator) -> None:
        """Initialise; nothing listens until `async_start`."""
        self._hass = hass
        self._coordinator = coordinator
        self._zcm = coordinator.zone_config_manager
        self._config = coordinator.config_manager
        self._state = WindowState()
        self._triggered: set[str] = set()
        self._delay_timers: dict[str, CALLBACK_TYPE] = {}
        self._retry_timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._sensor_unsub: CALLBACK_TYPE | None = None
        self._climate_unsub: CALLBACK_TYPE | None = None
        # Rooms with a write of ours in flight, so its state change isn't read as a person's.
        self._own_write: set[str] = set()
        # Held rooms back on their schedule because our OFF overlay ended (timer / next time block): switch off again.
        self._reassert: set[str] = set()
        # Failed restore attempts per room, so a restore that can never work is given up.
        self._restore_failures: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._stopped = False
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Load the held rooms, subscribe, and decide once Home Assistant has started."""
        data = await self._coordinator.data_loader.async_load_auxiliary(_STORE)
        if isinstance(data, dict):
            self._state.held = {str(z) for z in data.get("held", []) if isinstance(z, (str, int))}
            self._state.overridden = {str(z) for z in data.get("overridden", []) if isinstance(z, (str, int))}
        if self._state.held:
            _LOGGER.info("Window control: %d room(s) were held off before the restart", len(self._state.held))

        self._unsubs.append(self._zcm.add_listener(self._on_zone_config_change))
        self._unsubs.append(self._coordinator.config_entry.add_update_listener(self._on_options_update))
        self._subscribe()

        if self._hass.state is CoreState.running:
            self._on_started()
        else:
            # A @callback, not a lambda: a plain function would run in a worker thread, off the event loop.
            @callback
            def _started(_event: Any) -> None:
                self._on_started()

            self._unsubs.append(self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started))

    @callback
    def _on_started(self) -> None:
        """Sensors have their states now: count windows already open as past their delay, then decide."""
        if self._stopped:
            return
        self._started = True
        # A window open across a restart already waited out its delay before it, so it holds right away.
        self._triggered = {z for z in self._open_zones() if self._action(z) != WINDOW_ACTION_NONE}
        # A held room whose OFF overlay ran out while Home Assistant was down is back on its schedule.
        for zone in self._state.held:
            entity_id = self._climate_entity_id(zone)
            state = self._hass.states.get(entity_id) if entity_id else None
            if state is not None and state.state == HVACMode.AUTO:
                self._reassert.add(zone)
        self._schedule_apply("start")

    def stop(self) -> None:
        """Stop listening; held rooms stay held (saved) for the next start."""
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for pending in (self._sensor_unsub, self._climate_unsub, self._retry_timer):
            if pending is not None:
                pending()
        self._sensor_unsub = self._climate_unsub = self._retry_timer = None
        for timer in self._delay_timers.values():
            timer()
        self._delay_timers.clear()

    def drop_zone(self, zone_id: str) -> bool:
        """Forget everything about a removed or reassigned zone (zone_state_registry owner); True if anything was held."""
        dropped = zone_id in self._state.held or zone_id in self._state.overridden
        if (timer := self._delay_timers.pop(zone_id, None)) is not None:
            timer()
            dropped = True
        dropped |= zone_id in self._triggered or zone_id in self._restore_failures
        self._triggered.discard(zone_id)
        self._reassert.discard(zone_id)
        self._restore_failures.pop(zone_id, None)
        if zone_id in self._state.held or zone_id in self._state.overridden:
            self._state.held.discard(zone_id)
            self._state.overridden.discard(zone_id)
            self._save()
        return dropped

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def _heating_zones(self) -> frozenset[str]:
        zones_info = self._coordinator.data_loader.get_cached("zones_info")
        if not isinstance(zones_info, list):
            return frozenset()
        return frozenset(str(z.get("id")) for z in zones_info if isinstance(z, dict) and z.get("type") == "HEATING")

    def _zone_name(self, zone_id: str) -> str:
        zones_info = self._coordinator.data_loader.get_cached("zones_info")
        if isinstance(zones_info, list):
            for z in zones_info:
                if isinstance(z, dict) and str(z.get("id")) == zone_id:
                    return str(z.get("name") or zone_id)
        return zone_id

    def _action(self, zone_id: str) -> str:
        return self._zcm.get_window_action(zone_id)

    def _sensors_by_zone(self) -> dict[str, list[str]]:
        return {z: s for z in self._heating_zones() if (s := self._zcm.get_window_sensors(z))}

    def _zone_open(self, sensors: list[str]) -> bool:
        for sensor in sensors:
            state = self._hass.states.get(sensor)
            # Unavailable or unknown says nothing about the window, so it never switches a room off.
            if state is not None and state.state == "on":
                return True
        return False

    def _open_zones(self) -> set[str]:
        return {z for z, sensors in self._sensors_by_zone().items() if self._zone_open(sensors)}

    def _view(self) -> WindowView:
        sensors = self._sensors_by_zone()
        open_zones = frozenset(z for z, s in sensors.items() if self._zone_open(s))
        return WindowView(
            heating_zones=self._heating_zones(),
            actions={z: self._action(z) for z in sensors},
            open_zones=open_zones,
            triggered=frozenset(self._triggered & open_zones),
        )

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def _subscribe(self) -> None:
        """(Re)subscribe to every window sensor and to the climate entity of every heating room."""
        if self._sensor_unsub is not None:
            self._sensor_unsub()
            self._sensor_unsub = None
        if self._climate_unsub is not None:
            self._climate_unsub()
            self._climate_unsub = None

        sensors = sorted({s for group in self._sensors_by_zone().values() for s in group})
        if sensors:
            self._sensor_unsub = async_track_state_change_event(self._hass, sensors, self._on_sensor_change)

        climates = [eid for z in self._heating_zones() if (eid := self._climate_entity_id(z))]
        if climates:
            self._climate_unsub = async_track_state_change_event(self._hass, climates, self._on_climate_change)

    @callback
    def _on_zone_config_change(self, zone_id: str, key: str, _value: Any) -> None:
        if self._stopped or key not in ("window_sensor", "window_action"):
            return
        self._subscribe()
        self._on_sensor_change(None)

    async def _on_options_update(self, _hass: HomeAssistant, _entry: Any) -> None:
        """Hub switch or window settings changed: they are read live, so just decide again."""
        if not self._stopped:
            self._schedule_apply("settings changed")

    @callback
    def _on_sensor_change(self, event: Event[EventStateChangedData] | None) -> None:
        """Start or cancel each room's off delay, then decide."""
        if self._stopped or not self._started:
            return
        if event is not None:
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            # An attribute-only update says nothing new about the window.
            if old_state is not None and new_state is not None and old_state.state == new_state.state:
                return
        open_zones = self._open_zones()
        delay = self._config.get_window_off_delay_seconds()

        for zone in list(self._delay_timers):
            if zone not in open_zones:
                self._delay_timers.pop(zone)()
        self._triggered &= open_zones

        for zone in open_zones - self._triggered - set(self._delay_timers):
            if self._action(zone) == WINDOW_ACTION_NONE:
                continue
            if delay <= 0:
                self._triggered.add(zone)
                continue
            _LOGGER.debug(
                "Window control: window open in %s, switching off in %ss unless it closes",
                self._zone_name(zone), delay,
            )
            self._delay_timers[zone] = async_call_later(self._hass, delay, self._delay_elapsed(zone))

        self._schedule_apply("window change")

    def _delay_elapsed(self, zone: str) -> Any:
        @callback
        def _fire(_now: Any) -> None:
            self._delay_timers.pop(zone, None)
            if self._stopped:
                return
            if zone in self._open_zones():
                self._triggered.add(zone)
                self._schedule_apply(f"window open in {self._zone_name(zone)} past the delay")

        return _fire

    @callback
    def _on_climate_change(self, event: Event[EventStateChangedData]) -> None:
        """A held room set to heat by someone else is theirs now; a held room back on its schedule gets switched off again."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state not in (HVACMode.HEAT, HVACMode.AUTO):
            return
        zone = self._zone_for_climate(event.data.get("entity_id", ""))
        if zone is None or zone not in self._state.held or zone in self._own_write:
            return
        if new_state.state == HVACMode.AUTO:
            # The OFF overlay follows the room's overlay mode, so a timer or "until next time block" ends it and
            # Tado goes back to the schedule. That is not a person's choice: switch it off again.
            if zone not in self._reassert:
                self._reassert.add(zone)
                _LOGGER.info(
                    "Window control: %s went back to its schedule while held off, switching it off again",
                    self._zone_name(zone),
                )
                self._schedule_apply("off overlay ended")
            return
        self._reassert.discard(zone)
        self._state.held.discard(zone)
        self._state.overridden.add(zone)
        self._save()
        _LOGGER.info(
            "Window control: %s was switched back on by hand while its window was open, leaving it as set",
            self._zone_name(zone),
        )
        self._create_task(self._async_drop_capture(zone), "drop capture")
        self._refresh_entity(zone)

    # ------------------------------------------------------------------
    # Decide and act
    # ------------------------------------------------------------------

    def _create_task(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        """Run a task owned by the config entry: unload waits for it, so a write never ends half recorded."""
        self._coordinator.config_entry.async_create_task(self._hass, coro, f"tado_ce window control {name}")

    def _schedule_apply(self, reason: str) -> None:
        if not self._stopped:
            self._create_task(self._async_apply(reason), "apply")

    async def _async_apply(self, reason: str) -> None:
        async with self._lock:
            if self._stopped:
                return
            view = self._view()
            # A person's choice only lasts while the windows that wanted the room off still do.
            asked: set[str] = set()
            for zone in view.triggered:
                asked |= targets_of(zone, view.actions.get(zone, WINDOW_ACTION_NONE), view.heating_zones)
            if self._state.overridden - asked:
                self._state.overridden &= asked
                self._save()

            to_off, to_restore = decide(
                view, self._state,
                enabled=self._config.get_window_control_enabled(),
                resume_mode=self._config.get_window_resume_mode(),
            )
            self._reassert &= self._state.held
            if not to_off and not to_restore and not self._reassert:
                return
            _LOGGER.debug(
                "Window control: %s, switching off %s, restoring %s, switching off again %s",
                reason, sorted(to_off), sorted(to_restore), sorted(self._reassert - to_restore),
            )
            for zone in sorted(to_off):
                await self._async_switch_off(zone, holders_of(zone, view))
            for zone in sorted(to_restore):
                await self._async_restore(zone)
            # After the restores: a room that came back needs no second OFF.
            for zone in sorted(self._reassert & self._state.held):
                await self._async_reassert_off(zone)

    async def _async_switch_off(self, zone: str, holders: list[str]) -> None:
        entity = self._climate_entity(zone)
        if entity is None:
            _LOGGER.warning("Window control: no climate entity for zone %s, cannot switch it off", zone)
            return
        if entity.hvac_mode == HVACMode.OFF:
            _LOGGER.debug("Window control: %s is already off, nothing to hold", self._zone_name(zone))
            return

        self._own_write.add(zone)
        try:
            await self._coordinator.async_capture_state(zone, entity.entity_type, WINDOW_OFF_CAPTURE_SOURCE)
            await entity.async_set_hvac_mode(HVACMode.OFF)
        except Exception as err:
            # WHY broad: the write can fail with any client error (timeout, aiohttp, quota); one room failing
            # must not stop the rest of the pass or leave our capture behind.
            _LOGGER.warning(
                "Window control: could not switch %s off (%s), will try again on the next window change",
                self._zone_name(zone), err,
            )
            await self._async_drop_capture(zone)
            return
        finally:
            self._own_write.discard(zone)

        if entity.hvac_mode != HVACMode.OFF:
            # The write was rolled back; the room is still running, so there is nothing to hold.
            _LOGGER.warning("Window control: switching %s off did not take, leaving it as it is", self._zone_name(zone))
            await self._async_drop_capture(zone)
            return

        self._state.held.add(zone)
        self._save()
        _LOGGER.info(
            "Window control: switched %s off, window open in %s",
            self._zone_name(zone), ", ".join(self._zone_name(h) for h in holders) or "another room",
        )
        self._refresh_entity(zone)

    async def _async_reassert_off(self, zone: str) -> None:
        """Switch a held room off again after its OFF overlay ended; the capture from the first OFF is kept."""
        entity = self._climate_entity(zone)
        if entity is None or entity.hvac_mode == HVACMode.OFF:
            self._reassert.discard(zone)
            return

        self._own_write.add(zone)
        try:
            await entity.async_set_hvac_mode(HVACMode.OFF)
        except Exception as err:
            # WHY broad: as in _async_switch_off, any client error; the room stays held and is tried again.
            _LOGGER.warning(
                "Window control: could not switch %s off again (%s), trying again in %s minutes",
                self._zone_name(zone), err, _RESTORE_RETRY_SECONDS // 60,
            )
            self._arm_retry()
            return
        finally:
            self._own_write.discard(zone)

        if entity.hvac_mode != HVACMode.OFF:
            _LOGGER.warning(
                "Window control: switching %s off again did not take, trying again in %s minutes",
                self._zone_name(zone), _RESTORE_RETRY_SECONDS // 60,
            )
            self._arm_retry()
            return
        self._reassert.discard(zone)
        self._refresh_entity(zone)

    async def _async_restore(self, zone: str) -> None:
        entity_id = self._climate_entity_id(zone)
        if entity_id is None:
            _LOGGER.warning("Window control: no climate entity for zone %s, cannot restore it", zone)
            self._forget_held(zone)
            return

        self._own_write.add(zone)
        try:
            await self._hass.services.async_call(
                DOMAIN, SERVICE_RESTORE_PREVIOUS_STATE, {"entity_id": entity_id}, blocking=True,
            )
        except Exception as err:
            # WHY broad: the restore can fail with any client error; it is retried, then given up, never raised.
            failures = self._restore_failures.get(zone, 0) + 1
            if failures >= _RESTORE_MAX_ATTEMPTS:
                _LOGGER.warning(
                    "Window control: could not switch %s back on after %d tries (%s), giving up; set it by hand",
                    self._zone_name(zone), failures, err,
                )
                self._forget_held(zone)
                await self._async_drop_capture(zone)
                return
            self._restore_failures[zone] = failures
            _LOGGER.warning(
                "Window control: could not switch %s back on (%s), trying again in %s minutes",
                self._zone_name(zone), err, _RESTORE_RETRY_SECONDS // 60,
            )
            self._arm_retry()
            return
        finally:
            self._own_write.discard(zone)

        self._forget_held(zone)
        _LOGGER.info("Window control: windows closed, put %s back as it was", self._zone_name(zone))
        self._refresh_entity(zone)

    def _forget_held(self, zone: str) -> None:
        """Stop holding a room and save."""
        self._state.held.discard(zone)
        self._reassert.discard(zone)
        self._restore_failures.pop(zone, None)
        self._save()

    def _arm_retry(self) -> None:
        if self._retry_timer is not None or self._stopped:
            return

        @callback
        def _retry(_now: Any) -> None:
            self._retry_timer = None
            self._schedule_apply("retrying a failed write")

        self._retry_timer = async_call_later(self._hass, _RESTORE_RETRY_SECONDS, _retry)

    async def _async_drop_capture(self, zone: str) -> None:
        """Forget our own State Restore capture for a room; another feature's capture is left alone."""
        entity = self._climate_entity(zone)
        if entity is None:
            return
        captured = await self._coordinator.async_peek_state(zone, entity.entity_type)
        if captured is not None and captured.source == WINDOW_OFF_CAPTURE_SOURCE:
            await self._coordinator.async_clear_captured_state(zone, entity.entity_type)

    def _save(self) -> None:
        self._coordinator.data_loader.save_auxiliary(
            _STORE, {"held": sorted(self._state.held), "overridden": sorted(self._state.overridden)},
        )

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def _climate_entity_id(self, zone: str) -> str | None:
        unique_id = f"tado_ce_{self._coordinator.home_id}_zone_{zone}_climate"
        return er.async_get(self._hass).async_get_entity_id("climate", DOMAIN, unique_id)

    def _zone_for_climate(self, entity_id: str) -> str | None:
        for zone in self._heating_zones():
            if self._climate_entity_id(zone) == entity_id:
                return zone
        return None

    def _climate_entity(self, zone: str) -> Any:
        entity_id = self._climate_entity_id(zone)
        if entity_id is None:
            return None
        for platform in async_get_platforms(self._hass, DOMAIN):
            if platform.domain == "climate":
                entity = platform.entities.get(entity_id)
                if entity is not None:
                    return entity
        return None

    def _refresh_entity(self, zone: str) -> None:
        entity = self._climate_entity(zone)
        if entity is not None and entity.hass is not None:
            entity.async_write_ha_state()

    def get_attributes(self, zone: str) -> dict[str, Any]:
        """Attributes for the room's climate entity: whether it is held off, and by which rooms' windows."""
        if zone not in self._state.held:
            return {"window_off": False}
        view = self._view()
        return {
            "window_off": True,
            "window_off_by": [self._zone_name(h) for h in holders_of(zone, view)],
        }
