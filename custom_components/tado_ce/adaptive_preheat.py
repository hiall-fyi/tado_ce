"""Tado CE Adaptive Preheat Manager: local Early Start replacement."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import ENTITY_DATA_PREHEAT_ADVISOR, ENTITY_DATA_PREHEAT_NOW
from .error_dispatch import handle_background_write_error
from .exceptions import TadoAuthError, TadoRateLimitError
from .helpers import build_timer_termination, get_zone_states

if TYPE_CHECKING:
    from datetime import datetime

    from .api_client import TadoApiClient
    from .config_manager import ConfigurationManager
    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader
    from .zone_config_manager import ZoneConfigManager

_LOGGER = logging.getLogger(__name__)

# api_call makes up to three attempts at a 403 inside each try, so three tries is at most nine requests per window.
_MAX_START_TRIES = 3


@dataclass
class _StartMark:
    """A start still owed this window: the overlay signature seen when armed, tries so far, and the zone fetch of the last try."""

    overlay: Any
    tries: int = 0
    fetch_epoch: datetime | None = None


class AdaptivePreheatManager:
    """Manages adaptive preheat automation for heating zones."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_manager: ConfigurationManager,
        api_client: TadoApiClient | None = None,
        data_loader: DataLoader | None = None,
        zone_config_manager: ZoneConfigManager | None = None,
    ) -> None:
        """Initialise the Adaptive Preheat Manager."""
        self._hass = hass
        self._config_manager = config_manager
        self._api_client = api_client
        self._data_loader = data_loader
        self._zone_config_manager = zone_config_manager
        self._coordinator: TadoDataUpdateCoordinator | None = None
        self._enabled = False
        self._enabled_zones: list[str] = []
        self._active_overlays: dict[str, dict[str, Any]] = {}
        self._state_listeners: list[Any] = []
        self._zone_info: dict[str, dict[str, Any]] = {}
        # Zones with a start still owed this window: the overlay seen when it was armed, tries so far, and the zone fetch of the last try.
        self._retry_overlay: dict[str, _StartMark] = {}
        # Zones with a start write in flight, so a second update or the start-up check cannot send another.
        self._starting: set[str] = set()

    def set_coordinator(self, coordinator: TadoDataUpdateCoordinator) -> None:
        """Wire the coordinator back-reference once it has been created.

        Resolves the chicken-and-egg setup order: the manager is built
        before the coordinator exists.
        """
        self._coordinator = coordinator

    async def async_setup(self) -> None:
        """Load zone info and start monitoring if adaptive preheat is enabled."""
        self._enabled = self._config_manager.get_adaptive_preheat_enabled()
        if not self._enabled:
            _LOGGER.debug("Adaptive Preheat: disabled in config, skipping setup")
            return

        if not self._config_manager.get_smart_comfort_enabled():
            _LOGGER.warning(
                "Adaptive Preheat: needs Smart Comfort to be enabled, "
                "turn Smart Comfort on in integration options to use "
                "Adaptive Preheat",
            )
            self._enabled = False
            return

        if self._data_loader is not None:
            zones_info = await self._hass.async_add_executor_job(self._data_loader.load_zones_info_file)
        else:
            _LOGGER.warning(
                "Adaptive Preheat: zone data not available yet, skipping setup",
            )
            return

        if not zones_info:
            _LOGGER.warning("Adaptive Preheat: no zones found in zone data, skipping setup")
            return

        for zone in zones_info:
            if zone.get("type") != "HEATING":
                continue

            zone_id = str(zone.get("id"))
            zone_name = zone.get("name", f"Zone {zone_id}")

            if self._zone_config_manager:
                zone_cfg = self._zone_config_manager.get_zone_config(zone_id)
                preheat_mode = zone_cfg.get("adaptive_preheat", "off")
                if preheat_mode not in ("active", "passive"):
                    continue
            else:
                continue

            zone_slug = slugify(zone_name)
            self._zone_info[zone_id] = {
                "name": zone_name,
                "slug": zone_slug,
                "preheat_now_entity": f"binary_sensor.{zone_slug}_preheat_now",
                "preheat_advisor_entity": f"sensor.{zone_slug}_preheat_advisor",
                "climate_entity": f"climate.{zone_slug}",
            }
            self._enabled_zones.append(zone_id)

        if not self._enabled_zones:
            _LOGGER.debug("Adaptive Preheat: no zones configured for adaptive preheat")
            return

        _LOGGER.info(
            "Adaptive Preheat: enabled for %d zone(s): %s",
            len(self._enabled_zones),
            ", ".join(self._zone_info[z]["name"] for z in self._enabled_zones),
        )

        await self._start_monitoring()

    async def _start_monitoring(self) -> None:
        """Subscribe to state changes on each zone's preheat_now binary sensor."""
        entities_to_monitor = [
            self._zone_info[zone_id]["preheat_now_entity"]
            for zone_id in self._enabled_zones
        ]

        @callback
        def _state_change_handler(event: Event) -> None:
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")

            if not new_state:
                return

            zone_id = None
            for zid, info in self._zone_info.items():
                if info["preheat_now_entity"] == entity_id:
                    zone_id = zid
                    break

            if not zone_id:
                return

            old_is_on = old_state and old_state.state == "on"
            new_is_on = new_state.state == "on"

            if new_is_on and (not old_is_on or zone_id in self._retry_overlay):
                self._hass.async_create_task(
                    self._trigger_preheat(zone_id),
                )
            elif not new_is_on and old_is_on:
                self._hass.async_create_task(
                    self._check_clear_overlay(zone_id),
                )

        cancel = async_track_state_change_event(
            self._hass,
            entities_to_monitor,
            _state_change_handler,  # type: ignore[arg-type]
        )
        self._state_listeners.append(cancel)

        _LOGGER.debug(
            "Adaptive Preheat: monitoring preheat_now for %d zone(s)",
            len(entities_to_monitor),
        )

        await self._check_initial_preheat_states()

    async def _check_initial_preheat_states(self) -> None:
        """Catch zones whose preheat_now is already ON at setup time.

        State listeners pick up future transitions, so this only matters
        for the moment of setup. Falls back silently when the
        coordinator isn't ready yet.
        """
        if not self._coordinator:
            return

        if self._is_home_away():
            _LOGGER.debug(
                "Adaptive Preheat: home is in away mode, skipping initial preheat check",
            )
            return

        for zone_id in self._enabled_zones:
            preheat_data = self._coordinator.get_entity_data(zone_id, ENTITY_DATA_PREHEAT_NOW)
            if preheat_data and preheat_data.get("state") == "on":
                _LOGGER.info(
                    "Adaptive Preheat: %s already needs preheat at startup, "
                    "triggering now",
                    self._zone_info[zone_id]["name"],
                )
                await self._trigger_preheat(zone_id)

    def _is_home_away(self) -> bool:
        """Return True when the home presence is anything other than HOME."""
        if not self._coordinator:
            return False
        home_state = (self._coordinator.data or {}).get("home_state")
        return bool(home_state and home_state.get("presence") != "HOME")

    def _get_zone_state(self, zone_id: str) -> dict[str, Any] | None:
        """Return the cached zone state from the coordinator."""
        coord_data = self._coordinator.data or {}  # type: ignore[union-attr]
        zone_states = get_zone_states(coord_data)
        return zone_states.get(zone_id) or zone_states.get(str(zone_id))

    def _should_suppress_passive(self, zone_id: str, zone_name: str) -> bool:
        """Return True when passive mode should defer to an existing overlay."""
        zd = self._get_zone_state(zone_id)
        if zd and zd.get("overlay") is not None:
            _LOGGER.info(
                "Adaptive Preheat: %s skipped in passive mode, zone "
                "already has an active overlay (%s)",
                zone_name,
                zd["overlay"].get("type", "unknown"),
            )
            return True
        return False

    def _get_target_temp(self, zone_id: str, zone_name: str) -> float | None:
        """Read and validate the next-block target from preheat advisor data."""
        preheat_data = self._coordinator.get_entity_data(zone_id, ENTITY_DATA_PREHEAT_ADVISOR)  # type: ignore[union-attr]
        if not preheat_data:
            _LOGGER.warning(
                "Adaptive Preheat: %s preheat advisor data not available, "
                "skipping this trigger",
                zone_name,
            )
            return None

        raw = preheat_data.get("target_temperature")
        if not raw:
            _LOGGER.warning(
                "Adaptive Preheat: %s next schedule block has no heating "
                "target, skipping this trigger",
                zone_name,
            )
            return None

        try:
            return float(raw)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Adaptive Preheat: %s could not read target temperature "
                "(value was %r), skipping this trigger",
                zone_name, raw,
            )
            return None

    def _is_already_at_target(self, zone_id: str, zone_name: str, target_temp: float) -> bool:
        """Return True when the zone is already within 0.5°C of the target."""
        zone_data = self._get_zone_state(zone_id)
        if not zone_data:
            return False
        if self._coordinator is not None:
            from .helpers import merge_homekit_into_zone_data

            zone_data = merge_homekit_into_zone_data(zone_data, zone_id, self._coordinator)
        sensor_data = zone_data.get("sensorDataPoints") or {}
        current_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
        if current_temp is not None and current_temp >= target_temp - 0.5:
            _LOGGER.debug(
                "Adaptive Preheat: %s already at target (%.1f°C ≥ %.1f°C), "
                "no preheat needed",
                zone_name, current_temp, target_temp,
            )
            return True
        return False

    def _overlay_signature(self, zone_id: str) -> Any:
        """Return the zone overlay's type, setting and termination type, or None; a timer countdown is left out."""
        if self._coordinator is None:
            return None
        overlay = (self._get_zone_state(zone_id) or {}).get("overlay")
        if not isinstance(overlay, dict):
            return overlay
        termination = overlay.get("termination")
        return (
            overlay.get("type"),
            overlay.get("setting"),
            termination.get("type") if isinstance(termination, dict) else None,
        )

    def _zone_data_newer_than(self, epoch: datetime | None) -> bool:
        """Return True once a zone fetch newer than the given epoch has landed."""
        current = self._coordinator.last_zone_fetch_ts() if self._coordinator is not None else None
        return current is not None and (epoch is None or current > epoch)

    def _give_up_if_spent(self, zone_id: str) -> None:
        """Drop the mark of a zone that used all its tries, with one WARNING."""
        mark = self._retry_overlay.get(zone_id)
        if mark is None or mark.tries < _MAX_START_TRIES:
            return
        del self._retry_overlay[zone_id]
        _LOGGER.warning(
            "Adaptive Preheat: %s could not start heating after %d tries, so this preheat is skipped",
            self._zone_info[zone_id]["name"], mark.tries,
        )

    async def _trigger_preheat(self, zone_id: str) -> None:
        """Start the zone's preheat unless a start write is already in flight for it."""
        if zone_id in self._starting:
            return
        self._starting.add(zone_id)
        try:
            await self._attempt_preheat(zone_id)
        finally:
            self._starting.discard(zone_id)
            self._give_up_if_spent(zone_id)

    async def _attempt_preheat(self, zone_id: str) -> None:
        """Set a heating overlay so the zone reaches its next-block target on time."""
        zone_info = self._zone_info.get(zone_id)
        if not zone_info:
            return

        zone_name = zone_info["name"]

        mark = self._retry_overlay.pop(zone_id, None)
        signature = self._overlay_signature(zone_id)
        if mark is not None and signature != mark.overlay:
            _LOGGER.info(
                "Adaptive Preheat: %s overlay changed since the preheat was held, "
                "not starting it",
                zone_name,
            )
            return
        if mark is not None and mark.tries and not self._zone_data_newer_than(mark.fetch_epoch):
            _LOGGER.debug(
                "Adaptive Preheat: %s waiting for Tado's next zone update before trying again",
                zone_name,
            )
            self._retry_overlay[zone_id] = mark
            return

        # Defence-in-depth: preheat must not fire while the home is
        # AWAY, even if a stale preheat_now signal slipped through.
        if self._is_home_away():
            _LOGGER.info(
                "Adaptive Preheat: %s skipped, home is in away mode",
                zone_name,
            )
            return

        if zone_id in self._active_overlays:
            _LOGGER.debug(
                "Adaptive Preheat: %s already preheating, leaving the "
                "current overlay in place",
                zone_name,
            )
            return

        preheat_mode = "active"
        if self._zone_config_manager:
            zone_cfg = self._zone_config_manager.get_zone_config(zone_id)
            preheat_mode = zone_cfg.get("adaptive_preheat", "off")

        if preheat_mode == "passive" and self._should_suppress_passive(zone_id, zone_name):
            return

        target_temp = self._get_target_temp(zone_id, zone_name)
        if target_temp is None:
            return

        if self._is_already_at_target(zone_id, zone_name, target_temp):
            return

        # Optional spend: wait out a cloud backoff and the quota reserve; the next preheat update tries again.
        if self._coordinator is not None and (
            self._coordinator.is_cloud_backoff_active() or self._coordinator.is_quota_spend_held()
        ):
            _LOGGER.debug(
                "Adaptive Preheat: %s preheat held. Tado quota is at the "
                "reserve or a backoff is active, will retry on the next preheat update",
                zone_name,
            )
            self._retry_overlay[zone_id] = mark or _StartMark(signature)
            return

        # Armed before the spend, so an error anywhere below keeps the retry and the try is counted once.
        mark = mark or _StartMark(signature)
        mark.tries += 1
        mark.fetch_epoch = self._coordinator.zone_fetch_epoch_for_write() if self._coordinator is not None else None
        self._retry_overlay[zone_id] = mark

        _LOGGER.info(
            "Adaptive Preheat: starting %s preheat to %.1f°C (will stop "
            "at the next schedule block)",
            zone_name, target_temp,
        )

        await self._set_preheat_overlay(
            zone_id, zone_name, target_temp, capture_state=(preheat_mode == "active"),
        )

    def _retry_note(self, zone_id: str) -> str:
        """Return the retry promise for a failed try, or nothing when the zone has no tries left."""
        mark = self._retry_overlay.get(zone_id)
        if mark is None or mark.tries >= _MAX_START_TRIES:
            return ""
        return ", will retry after Tado's next zone update"

    def _is_preheat_now_off(self, zone_id: str) -> bool:
        """Return True when the zone's preheat_now entity exists and is no longer on."""
        state = self._hass.states.get(self._zone_info[zone_id]["preheat_now_entity"])
        return state is not None and state.state != "on"

    async def _set_preheat_overlay(
        self, zone_id: str, zone_name: str, target_temp: float, *, capture_state: bool = False,
    ) -> None:
        """Send the heating overlay to Tado and record it for later cleanup."""
        try:
            client = self._api_client
            if client is None:
                _LOGGER.warning(
                    "Adaptive Preheat: %s preheat skipped. Tado API client not available%s",
                    zone_name, self._retry_note(zone_id),
                )
                return

            setting = {
                "type": "HEATING",
                "power": "ON",
                "temperature": {"celsius": target_temp},
            }
            termination = build_timer_termination(overlay="next_time_block")

            success = await client.set_zone_overlay(zone_id, setting, termination)

            if success:
                self._retry_overlay.pop(zone_id, None)
                self._active_overlays[zone_id] = {
                    "target_temp": target_temp,
                    "triggered_at": dt_util.utcnow(),
                    "termination": "TADO_MODE",
                }
                _LOGGER.debug(
                    "Adaptive Preheat: %s heating overlay accepted by Tado",
                    zone_name,
                )
                # Captured on success like the other overlay writers, so a rejected write leaves no snapshot behind.
                if capture_state and self._coordinator is not None:
                    await self._coordinator.async_capture_state(zone_id, "climate_heating", "preheat")
                # The window can end while the write is in flight; its OFF event found nothing to drop.
                if self._is_preheat_now_off(zone_id):
                    await self._check_clear_overlay(zone_id)
            else:
                _LOGGER.warning(
                    "Adaptive Preheat: %s could not start heating. Tado rejected the overlay%s",
                    zone_name, self._retry_note(zone_id),
                )

        except (TadoAuthError, TadoRateLimitError) as err:
            if self._coordinator is not None:
                handle_background_write_error(
                    err, self._coordinator.config_entry, self._hass, self._coordinator,
                    f"Adaptive Preheat: {zone_name} overlay write failed, "
                    f"starting recovery{self._retry_note(zone_id)}",
                )
        except Exception:
            # A failed write must not end the task silently; the armed mark keeps the retry.
            _LOGGER.exception(
                "Adaptive Preheat: %s overlay write raised an exception%s",
                zone_name, self._retry_note(zone_id),
            )

    async def _check_clear_overlay(self, zone_id: str) -> None:
        """Drop our overlay record when preheat_now turns OFF.

        We only ever cleared our own overlays; with TADO_MODE
        termination Tado auto-clears the overlay at the next schedule
        block, so the cleanup here is purely housekeeping.
        """
        # A held start dies with its window.
        self._retry_overlay.pop(zone_id, None)
        zone_info = self._zone_info.get(zone_id)
        if not zone_info:
            return

        zone_name = zone_info["name"]

        if zone_id not in self._active_overlays:
            _LOGGER.debug(
                "Adaptive Preheat: %s preheat_now turned off but no overlay "
                "was tracked, nothing to clean up",
                zone_name,
            )
            return

        del self._active_overlays[zone_id]
        _LOGGER.info(
            "Adaptive Preheat: %s preheat ended, heating will stop at the "
            "next schedule block",
            zone_name,
        )

    async def async_unload(self) -> None:
        """Cancel state listeners during integration unload."""
        for cancel in self._state_listeners:
            cancel()
        self._state_listeners.clear()
        self._retry_overlay.clear()

        _LOGGER.debug("Adaptive Preheat: unloaded")
