"""Capture-and-restore zone state across overlay operations.

Snapshots the schedule / overlay / mode that was in effect *before*
an overlay operation (open-window mode, timer set, AC manual override
etc.) so the user can restore it cleanly when they're done. Persists
through HA Store, purges entries older than 24 h on load, and fires
a restoration event when an overlay disappears between polls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .helpers import get_zone_states, parse_iso_datetime, prune_zone_keyed_dict
from .storage import async_migrate_json_to_store

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .config_manager import ConfigurationManager
    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)

# Stale state threshold: purge captured states older than this on load
_STALE_THRESHOLD = timedelta(hours=24)

# Storage base name (for old file migration)
_STORAGE_BASE_NAME = "state_restore"

# HA Store constants
STORE_VERSION = 1
SAVE_DELAY = 5

# Defensive gate against transient overlay-cleared events. The Tado
# API can return a zone with overlay=null on a single poll during
# quota-reset windows or partial responses; firing the restoration
# event on a single observation produced false-positive triggers.
# Requiring N consecutive polls before firing filters those out
# without changing steady-state behaviour.
CONSECUTIVE_CLEARED_THRESHOLD = 2


@dataclass
class CapturedState:
    """Snapshot of one zone's state taken before an overlay operation.

    `entity_type` is one of "climate_heating", "climate_ac", or
    "water_heater"; AC-only fields stay None for the others.
    `overlay_type` is the Tado API enum: MANUAL / TIMER / TADO_MODE
    or None when the zone was running its schedule.
    """

    zone_id: str
    entity_type: str
    temperature: float | None = None
    hvac_mode: str | None = None
    power: str | None = None
    overlay_type: str | None = None
    termination: dict[str, Any] | None = None
    fan_mode: str | None = None
    fan_key: str | None = None
    swing_mode: str | None = None
    horizontal_swing_mode: str | None = None
    captured_at: str = ""
    source: str = ""


def _state_to_dict(state: CapturedState) -> dict[str, Any]:
    """Serialise a CapturedState to a JSON-friendly dict."""
    return {
        "zone_id": state.zone_id,
        "entity_type": state.entity_type,
        "temperature": state.temperature,
        "hvac_mode": state.hvac_mode,
        "power": state.power,
        "overlay_type": state.overlay_type,
        "termination": state.termination,
        "fan_mode": state.fan_mode,
        "fan_key": state.fan_key,
        "swing_mode": state.swing_mode,
        "horizontal_swing_mode": state.horizontal_swing_mode,
        "captured_at": state.captured_at,
        "source": state.source,
    }


def _state_from_dict(data: dict[str, Any]) -> CapturedState:
    """Restore a CapturedState from a previously-serialised dict."""
    return CapturedState(
        zone_id=data.get("zone_id", ""),
        entity_type=data.get("entity_type", ""),
        temperature=data.get("temperature"),
        hvac_mode=data.get("hvac_mode"),
        power=data.get("power"),
        overlay_type=data.get("overlay_type"),
        termination=data.get("termination"),
        fan_mode=data.get("fan_mode"),
        fan_key=data.get("fan_key"),
        swing_mode=data.get("swing_mode"),
        horizontal_swing_mode=data.get("horizontal_swing_mode"),
        captured_at=data.get("captured_at", ""),
        source=data.get("source", ""),
    )


def _make_store_key(zone_id: str, entity_type: str) -> str:
    """Build the per-zone-per-entity-type storage key."""
    return f"{zone_id}:{entity_type}"


class StateRestoreManager:
    """Capture and restore one CapturedState per (zone, entity_type) pair."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_manager: ConfigurationManager,
        data_loader: DataLoader,
    ) -> None:
        """Initialise the manager and prepare the HA Store backend."""
        self._hass = hass
        self._config_manager = config_manager
        self._data_loader = data_loader
        self._coordinator: TadoDataUpdateCoordinator | None = None
        self._captured: dict[str, CapturedState] = {}
        self._lock = asyncio.Lock()
        # Tracks whether a zone had an overlay in the previous poll so
        # `on_poll_update` can detect timer expiry (overlay → no overlay).
        self._previous_overlay_states: dict[str, bool] = {}
        # Consecutive overlay=null polls per zone (see CONSECUTIVE_CLEARED_THRESHOLD
        # for why). Reset to 0 when an overlay re-appears or once the event fires.
        self._consecutive_cleared: dict[str, int] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORE_VERSION,
            f"tado_ce/state_restore_{data_loader.home_id}",
        )
        # Older builds wrote a JSON file alongside the Store; the path
        # is kept so async_setup can migrate any leftover data.
        self._old_storage_path = Path(
            hass.config.path(
                f".storage/tado_ce/{_STORAGE_BASE_NAME}_{data_loader.home_id}.json",
            ),
        )

    def set_coordinator(self, coordinator: TadoDataUpdateCoordinator) -> None:
        """Wire the coordinator back-reference once it has been created."""
        self._coordinator = coordinator

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Restore captured states from the Store and drop entries older than 24 h."""
        try:
            raw: dict[str, Any] | list[Any] | None = await self._store.async_load()
        except (HomeAssistantError, OSError, json.JSONDecodeError) as e:
            # A corrupt storage file must not block integration setup.
            # Same pattern as DataLoader.async_load_* auxiliary methods.
            _LOGGER.warning(
                "State Restore: could not load persisted state (%s), "
                "starting fresh, in-progress overlay restorations will be lost",
                e,
            )
            return

        if raw is None:
            try:
                raw = await async_migrate_json_to_store(
                    self._hass, self._old_storage_path, self._store,
                    label="state_restore",
                )
            except (HomeAssistantError, OSError, json.JSONDecodeError) as e:
                _LOGGER.warning(
                    "State Restore: could not migrate legacy JSON storage "
                    "(%s), starting fresh",
                    e,
                )
                return

        if not raw or not isinstance(raw, dict):
            _LOGGER.debug("State Restore: no persisted state, starting fresh")
            return

        now = dt_util.utcnow()
        loaded = 0
        purged = 0
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            state = _state_from_dict(entry)
            if state.captured_at:
                try:
                    captured_dt = parse_iso_datetime(state.captured_at)
                    if now - captured_dt > _STALE_THRESHOLD:
                        purged += 1
                        continue
                except (ValueError, TypeError):
                    # Keep entries whose timestamps can't be parsed:
                    # unexpected, but losing them is worse than keeping them.
                    pass
            self._captured[key] = state
            loaded += 1

        if loaded or purged:
            _LOGGER.debug(
                "State Restore: restored %d captured state(s), "
                "purged %d stale entry(ies)",
                loaded, purged,
            )
        if purged:
            self._schedule_persist()

    async def async_shutdown(self) -> None:
        """Persist state to the Store before HA shutdown / config-entry unload."""
        async with self._lock:
            data = {key: _state_to_dict(state) for key, state in self._captured.items()}
        await self._store.async_save(data)
        _LOGGER.debug("State Restore: state persisted on shutdown")

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def capture(
        self,
        zone_id: str,
        entity_type: str,
        source: str,
    ) -> bool:
        """Snapshot the zone's current state, preserving any existing capture.

        Returns True when a fresh snapshot was stored, False when an
        earlier capture was kept (the original pre-overlay state wins
        so a chain of overlay operations restores back to the
        pre-chain state, not the previous overlay).
        """
        key = _make_store_key(zone_id, entity_type)

        async with self._lock:
            if key in self._captured:
                _LOGGER.debug(
                    "State Restore: %s already captured by %s, "
                    "keeping the original pre-overlay state",
                    key, self._captured[key].source,
                )
                return False

            if not self._coordinator:
                _LOGGER.warning(
                    "State Restore: cannot capture %s, coordinator not "
                    "wired up yet, restoration will be unavailable for "
                    "this overlay",
                    key,
                )
                return False

            coord_data = self._coordinator.data or {}
            zone_states = get_zone_states(coord_data)
            zone_data = zone_states.get(zone_id) or zone_states.get(str(zone_id))

            if not zone_data:
                _LOGGER.debug(
                    "State Restore: no zone data for zone %s yet, "
                    "skipping capture, restoration will be unavailable",
                    zone_id,
                )
                return False

            state = self._extract_state(zone_id, entity_type, zone_data, source)
            self._captured[key] = state
            self._schedule_persist()

            _LOGGER.debug(
                "State Restore: captured %s (temp=%s, power=%s, "
                "overlay=%s, source=%s)",
                key, state.temperature, state.power, state.overlay_type, source,
            )
            return True

    async def restore(
        self,
        zone_id: str,
        entity_type: str,
    ) -> CapturedState | None:
        """Consume and return the captured state for one zone, or None."""
        key = _make_store_key(zone_id, entity_type)

        async with self._lock:
            state = self._captured.pop(key, None)
            if state:
                self._schedule_persist()
                _LOGGER.debug("State Restore: consumed %s for restoration", key)
            return state

    def prune_stale_captures(self, current_zones: frozenset[str]) -> int:
        """Drop captures whose zone_id is no longer in current_zones; return removed count."""
        return prune_zone_keyed_dict(self._captured, current_zones)

    async def prune_reassigned_zones(self, zone_ids: set[str]) -> set[str]:
        """Drop captures for reassigned zone_ids, so a later restore can't apply the old room's snapshot."""
        async with self._lock:
            to_drop = {k for k in self._captured if k.split(":", 1)[0] in zone_ids}
            if not to_drop:
                return set()
            pruned = {k.split(":", 1)[0] for k in to_drop}
            for k in to_drop:
                del self._captured[k]
            self._schedule_persist()
        _LOGGER.info(
            "State Restore: cleared captured state for reassigned zone(s) %s "
            "(the id is now a different room)",
            sorted(pruned),
        )
        return pruned

    async def peek(
        self,
        zone_id: str,
        entity_type: str,
    ) -> CapturedState | None:
        """Return the captured state without consuming it.

        `handle_restore_previous_state` peeks first, attempts the cloud
        write, and only consumes the captured state on success. An
        HTTP 422 / network failure during the write would otherwise
        leave the user with no retry path.
        """
        key = _make_store_key(zone_id, entity_type)
        async with self._lock:
            return self._captured.get(key)

    async def clear(self, zone_id: str, entity_type: str) -> None:
        """Drop the captured state for one zone explicitly."""
        key = _make_store_key(zone_id, entity_type)
        async with self._lock:
            if self._captured.pop(key, None):
                self._schedule_persist()
                _LOGGER.debug("State Restore: cleared %s", key)


    async def clear_all(self) -> None:
        """Drop every captured state (e.g. when the home transitions Away → Home)."""
        async with self._lock:
            if self._captured:
                count = len(self._captured)
                self._captured.clear()
                self._schedule_persist()
                _LOGGER.info(
                    "State Restore: cleared all %d captured state(s), "
                    "no overlay restorations are pending",
                    count,
                )

    def get_diagnostics_summary(self) -> list[dict[str, str]]:
        """Return a privacy-safe summary of captured states for the diagnostics dump."""
        return [
            {
                "zone_id": state.zone_id,
                "entity_type": state.entity_type,
                "captured_at": state.captured_at,
                "source": state.source,
            }
            for state in self._captured.values()
        ]

    # ------------------------------------------------------------------
    # Timer expiry detection (called from coordinator poll cycle)
    # ------------------------------------------------------------------

    def on_poll_update(self, coordinator_data: dict[str, Any]) -> None:
        """Fire a restoration event when an overlay disappears between polls.

        Called from `coordinator._async_update_data` after each poll.
        An overlay vanishing means the timer expired or someone
        cleared the overlay outside HA. Either way the user is back
        on the schedule, so the captured pre-overlay state is no
        longer needed and we surface a restoration event.

        Defensive: fires only after CONSECUTIVE_CLEARED_THRESHOLD (2)
        consecutive polls report overlay=null. A single poll showing
        a missing overlay can be a transient (quota-reset window
        partial response, brief upstream blip) rather than a real
        clear; requiring two consecutive polls filters those out.
        """
        zone_states = get_zone_states(coordinator_data)

        for key, captured in list(self._captured.items()):
            zone_id = captured.zone_id
            zone_data = zone_states.get(zone_id) or zone_states.get(str(zone_id))

            has_overlay = bool(zone_data and zone_data.get("overlay"))
            had_overlay = self._previous_overlay_states.get(zone_id, False)

            if has_overlay:
                # Overlay re-appeared, reset the consecutive-cleared
                # counter so transient blips don't accumulate across
                # unrelated polls.
                self._consecutive_cleared.pop(zone_id, None)
                continue

            if not had_overlay:
                # Steady-state "no overlay": not a transition we care
                # about. Counter stays at 0.
                continue

            count = self._consecutive_cleared.get(zone_id, 0) + 1
            self._consecutive_cleared[zone_id] = count

            if count < CONSECUTIVE_CLEARED_THRESHOLD:
                _LOGGER.debug(
                    "State Restore: zone %s overlay missing on poll %d/%d, "
                    "waiting for confirmation before firing restoration event",
                    zone_id, count, CONSECUTIVE_CLEARED_THRESHOLD,
                )
                continue

            if key in self._captured:
                _LOGGER.info(
                    "State Restore: zone %s overlay cleared (confirmed across "
                    "%d consecutive polls), restoration now available for "
                    "the captured pre-overlay state",
                    zone_id, count,
                )
                self._fire_restoration_event(captured, zone_data)
                self._captured.pop(key, None)
                self._consecutive_cleared.pop(zone_id, None)
                self._schedule_persist()

        for zone_id_str in zone_states:
            zd = zone_states[zone_id_str]
            self._previous_overlay_states[zone_id_str] = bool(zd and zd.get("overlay"))

    def _fire_restoration_event(
        self,
        captured: CapturedState,
        zone_data: dict[str, Any] | None,
    ) -> None:
        """Fire the `tado_ce_state_restoration_available` event for automations to consume."""
        zone_name = ""
        if zone_data:
            zone_name = zone_data.get("name", "")

        self._hass.bus.async_fire(
            "tado_ce_state_restoration_available",
            {
                "zone_id": captured.zone_id,
                "zone_name": zone_name,
                "entity_type": captured.entity_type,
                "captured_temperature": captured.temperature,
                "captured_hvac_mode": captured.hvac_mode,
                "source": captured.source,
            },
        )

    # ------------------------------------------------------------------
    # State extraction from coordinator zone data
    # ------------------------------------------------------------------

    def _extract_state(
        self,
        zone_id: str,
        entity_type: str,
        zone_data: dict[str, Any],
        source: str,
    ) -> CapturedState:
        """Extract a CapturedState from the live zone-data snapshot."""
        setting = zone_data.get("setting") or {}
        overlay = zone_data.get("overlay")
        overlay_type = zone_data.get("overlayType")

        power = setting.get("power")
        temperature = (setting.get("temperature") or {}).get("celsius")

        termination: dict[str, Any] | None = None
        if overlay and isinstance(overlay, dict):
            termination = overlay.get("termination")

        # Heating zones don't carry an explicit hvac_mode field, so we
        # derive it from power + overlay so restore() can write back
        # the same shape the climate entity expects.
        hvac_mode: str | None = None
        if entity_type in ("climate_heating", "climate_ac"):
            if entity_type == "climate_ac":
                hvac_mode = setting.get("mode")
            elif power == "ON":
                hvac_mode = "heat" if overlay_type == "MANUAL" else "auto"
            elif overlay_type == "MANUAL":
                hvac_mode = "off"
            else:
                hvac_mode = "auto"

        fan_mode: str | None = None
        fan_key: str | None = None
        swing_mode: str | None = None
        horizontal_swing_mode: str | None = None
        if entity_type == "climate_ac":
            # The payload field is fanLevel (modern) or fanSpeed (legacy);
            # remember which one this unit uses so the restore writes the
            # same key rather than guessing from the value.
            if setting.get("fanLevel") is not None:
                fan_mode, fan_key = setting["fanLevel"], "fanLevel"
            elif setting.get("fanSpeed") is not None:
                fan_mode, fan_key = setting["fanSpeed"], "fanSpeed"
            swing_mode = setting.get("verticalSwing")
            horizontal_swing_mode = setting.get("horizontalSwing")

        return CapturedState(
            zone_id=zone_id,
            entity_type=entity_type,
            temperature=temperature,
            hvac_mode=hvac_mode,
            power=power,
            overlay_type=overlay_type,
            termination=termination,
            fan_mode=fan_mode,
            fan_key=fan_key,
            swing_mode=swing_mode,
            horizontal_swing_mode=horizontal_swing_mode,
            captured_at=dt_util.utcnow().isoformat(),
            source=source,
        )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _schedule_persist(self) -> None:
        """Queue a debounced save so multiple captures coalesce into one Store write."""
        self._store.async_delay_save(
            lambda: {key: _state_to_dict(s) for key, s in self._captured.items()},
            SAVE_DELAY,
        )
