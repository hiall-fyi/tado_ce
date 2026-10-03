"""Tado CE select platform: Presence Mode, Overlay Mode, Timer Duration.

Three home-level select entities. Presence Mode talks to the
cloud (1 API call per change, with optimistic update + rollback);
Overlay Mode and Timer Duration are local-only: they shape how
*future* manual writes are sent but don't themselves hit the
cloud.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .climate_helpers import inject_presence_state
from .const import (
    DOMAIN,
    OVERLAY_MODE_DEFAULT,
    OVERLAY_MODE_DEFAULT_DISPLAY,
    OVERLAY_MODE_MAP,
    OVERLAY_MODE_OPTIONS,
    OVERLAY_MODE_REVERSE_MAP,
    TIMER_DURATION_DEFAULT,
    TIMER_DURATION_OPTIONS,
)
from .device_manager import get_hub_device_info, get_zone_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .helpers import async_trigger_immediate_refresh
from .optimistic_helpers import (
    OptimisticUpdateResult,
    clear_optimistic_state,
    resolve_optimistic_update,
    set_optimistic_fields,
)
from .ratelimit import async_check_bootstrap_reserve_or_raise as _check_bootstrap_reserve_or_raise

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE select entities from a config entry."""
    _LOGGER.debug("Select: setup starting")
    coordinator = entry.runtime_data
    home_id = coordinator.home_id

    entities: list[SelectEntity] = [
        TadoPresenceModeSelect(coordinator, home_id),
        TadoOverlayModeSelect(coordinator, home_id),
        TadoTimerDurationSelect(coordinator, home_id),
    ]

    # Per-zone heating-circuit select: opt-in, and only when the home actually
    # has a boiler circuit to assign (single-circuit homes still get it for the
    # residual-heat "No heating circuit" option).
    config_manager = coordinator.config_manager
    if config_manager is not None and config_manager.get_heating_circuit_enabled():
        circuits = ((coordinator.data or {}).get("heating_circuits") or {}).get("circuits") or []
        if circuits:
            zones_info = await hass.async_add_executor_job(
                coordinator.data_loader.load_zones_info_file,
            )
            for zone in zones_info or []:
                if zone.get("type") != "HEATING":
                    continue
                entities.append(
                    TadoHeatingCircuitSelect(
                        coordinator,
                        str(zone.get("id")),
                        zone.get("name", f"Zone {zone.get('id')}"),
                        zone.get("type"),
                        home_id,
                    ),
                )

    if entities:
        async_add_entities(entities, True)
        _LOGGER.info(
            "Select: created %d select entity(ies)", len(entities),
        )



class TadoPresenceModeSelect(CoordinatorEntity["TadoDataUpdateCoordinator"], SelectEntity):
    """Switch between auto / home / away presence modes.

    `auto` clears the presence lock so geofencing decides;
    `home` / `away` lock the cloud-reported presence to the
    matching value. Optimistic update + rollback on API failure.
    """

    _attr_has_entity_name = True
    _attr_options: list[str] = ["auto", "home", "away"]
    _attr_translation_key = "presence_mode"

    def __init__(self, coordinator: TadoDataUpdateCoordinator, home_id: str) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["select_presence_mode"]
        self._entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix}"
        if _meta.translation_key is not None:
            self._attr_translation_key = _meta.translation_key
        self._attr_current_option = "auto"
        self._attr_device_info = get_hub_device_info(home_id)

        # State tracking
        self._presence = "HOME"
        self._presence_locked = False

        # Optimistic update tracking
        self._optimistic_set_at: float | None = None
        self._optimistic_sequence: int | None = None
        self._expected_mode: str | None = None

    @property
    def icon(self) -> str | None:
        """Return icon based on current mode."""
        if self._attr_current_option == "auto":
            return "mdi:home-account"
        if self._attr_current_option == "home":
            return "mdi:home"
        # Away
        return "mdi:home-export-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "presence": self._presence,
            "presence_locked": self._presence_locked,
            "automatic_geofencing": not self._presence_locked,
            "api_calls_per_change": 1,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh presence mode from the latest home_state poll."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Reconcile cached `home_state` with any in-flight optimistic write."""
        try:
            coord_data = self.coordinator.data or {}
            home_state = coord_data.get("home_state")
            if not home_state:
                return

            api_presence = home_state.get("presence", "HOME")
            api_locked = home_state.get("presenceLocked", False)

            if not api_locked:
                api_mode = "auto"
            elif api_presence == "HOME":
                api_mode = "home"
            else:
                api_mode = "away"

            result = resolve_optimistic_update(
                self,
                api_values={"mode": api_mode},
                entry_id=self._entry_id,
            )

            if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
                _LOGGER.debug(
                    "Select: presence mode holding optimistic %s, "
                    "cloud still reports %s",
                    self._expected_mode,
                    api_mode,
                )
                return

            self._presence = api_presence
            self._presence_locked = api_locked
            self._attr_current_option = api_mode

        except (AttributeError, TypeError, KeyError) as e:
            _LOGGER.warning(
                "Select: presence mode update failed (%s), keeping "
                "last known mode until the next poll",
                e,
            )

    async def async_select_option(self, option: str) -> None:
        """Apply a new presence mode (`auto` / `home` / `away`)."""
        await _check_bootstrap_reserve_or_raise(self.hass, "Presence Mode", coordinator=self.coordinator)

        old_mode = self._attr_current_option
        old_presence = self._presence
        old_locked = self._presence_locked

        self._attr_current_option = option

        if option == "auto":
            self._presence_locked = False
        else:
            self._presence_locked = True
            self._presence = option.upper()

        await set_optimistic_fields(
            self, self.coordinator,
            expected={"mode": option},
        )

        self.async_write_ha_state()

        option_lower = option.lower()
        client = self.coordinator.api_client
        if option_lower == "auto":
            success = await client.delete_presence_lock()
        else:
            success = await client.set_presence_lock(option.upper())

        if success:
            _LOGGER.debug(
                "Select: set presence mode to %s", option,
            )
            # Inject home_state locally so climate preset_mode flips
            # immediately even when Home State Sync is off. For
            # `auto` we don't yet know HOME vs AWAY (geofencing
            # decides on the next poll), so leave cached presence
            # alone rather than guess HOME.
            if option == "auto":
                await inject_presence_state(self.coordinator, None, locked=False)
            else:
                await inject_presence_state(self.coordinator, option.upper(), locked=True)
            await async_trigger_immediate_refresh(
                self.hass, self.entity_id, f"presence_mode_{option}",
            )
        else:
            _LOGGER.warning(
                "Select: presence mode %s write failed, reverted to "
                "previous mode",
                option,
            )
            self._attr_current_option = old_mode
            self._presence = old_presence
            self._presence_locked = old_locked
            clear_optimistic_state(self)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Set presence mode to {option} failed",
                translation_domain=DOMAIN,
                translation_key="presence_mode_set_failed",
                translation_placeholders={"option": option},
            )


class TadoOverlayModeSelect(CoordinatorEntity["TadoDataUpdateCoordinator"], SelectEntity):
    """Choose how long manual temperature changes stay in effect.

    Local-only setting (no API call) that shapes future climate
    writes: Tado Mode mirrors the per-device app preference,
    Next Time Block / Timer / Manual override the cloud
    termination explicitly.
    """

    _attr_has_entity_name = True
    _attr_options = OVERLAY_MODE_OPTIONS
    _attr_translation_key = "overlay_mode"

    def __init__(self, coordinator: TadoDataUpdateCoordinator, home_id: str) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["select_overlay_mode"]
        self._entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix}"
        if _meta.translation_key is not None:
            self._attr_translation_key = _meta.translation_key
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_current_option = OVERLAY_MODE_DEFAULT_DISPLAY
        self._attr_device_info = get_hub_device_info(home_id)
        self._attr_icon = _meta.icon

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "description": "Controls how long a temperature change made from Home Assistant lasts",
            "manual_info": "Until you resume the schedule",
            "tado_mode_info": "Until the next automatic change in your Tado schedule",
            "timer_info": "For a set duration (see Timer Duration)",
            "api_calls_per_change": 0,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh overlay mode display from coordinator cache."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Read the cached overlay mode (set by previous user selections)."""
        try:
            overlay_mode = self.coordinator.overlay_mode or OVERLAY_MODE_DEFAULT
            self._attr_current_option = OVERLAY_MODE_REVERSE_MAP.get(overlay_mode, OVERLAY_MODE_DEFAULT_DISPLAY)
        except (AttributeError, TypeError) as e:
            _LOGGER.warning(
                "Select: overlay mode update failed (%s), keeping "
                "last known option until the next poll",
                e,
            )

    async def async_select_option(self, option: str) -> None:
        """Persist a new overlay mode locally (no cloud call)."""
        self._attr_current_option = option
        self.async_write_ha_state()

        api_mode = OVERLAY_MODE_MAP.get(option, OVERLAY_MODE_DEFAULT)
        success = self.coordinator.data_loader.save_overlay_mode(api_mode)

        if success:
            self.coordinator.overlay_mode = api_mode
            _LOGGER.debug(
                "Select: overlay mode set to %s (%s)", option, api_mode,
            )
        else:
            _LOGGER.warning(
                "Select: overlay mode persistence failed for %s, "
                "value applied in memory but won't survive a HA "
                "restart",
                option,
            )


class TadoTimerDurationSelect(CoordinatorEntity["TadoDataUpdateCoordinator"], SelectEntity):
    """Set the Timer-overlay duration in minutes.

    Only takes effect when Overlay Mode is `Timer`. Local-only:
    persisted alongside Overlay Mode so the next manual write
    picks up the new duration.
    """

    _attr_has_entity_name = True
    _attr_options = TIMER_DURATION_OPTIONS
    _attr_translation_key = "timer_duration"

    def __init__(self, coordinator: TadoDataUpdateCoordinator, home_id: str) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["select_timer_duration"]
        self._entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix}"
        if _meta.translation_key is not None:
            self._attr_translation_key = _meta.translation_key
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_current_option = str(TIMER_DURATION_DEFAULT)
        self._attr_device_info = get_hub_device_info(home_id)
        self._attr_icon = _meta.icon
        self._attr_unit_of_measurement = "min"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "description": "Duration for Timer overlay mode",
            "unit": "minutes",
            "api_calls_per_change": 0,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh timer duration display from coordinator cache."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Read the cached timer duration (set by previous user selections)."""
        try:
            duration = self.coordinator.timer_duration or TIMER_DURATION_DEFAULT
            self._attr_current_option = str(duration)
        except (AttributeError, TypeError) as e:
            _LOGGER.warning(
                "Select: timer duration update failed (%s), keeping "
                "last known value until the next poll",
                e,
            )

    async def async_select_option(self, option: str) -> None:
        """Persist a new timer duration locally (no cloud call)."""
        self._attr_current_option = option
        self.async_write_ha_state()

        duration = int(option)
        success = self.coordinator.data_loader.save_timer_duration(duration)

        if success:
            self.coordinator.timer_duration = duration
            _LOGGER.debug(
                "Select: timer duration set to %s minutes", duration,
            )
        else:
            _LOGGER.warning(
                "Select: timer duration persistence failed for %s, "
                "value applied in memory but won't survive a HA "
                "restart",
                option,
            )


NO_HEATING_CIRCUIT_OPTION = "no_heating_circuit"


class TadoHeatingCircuitSelect(CoordinatorEntity["TadoDataUpdateCoordinator"], SelectEntity):
    """Assign a heating zone to a boiler circuit, or to "No heating circuit".

    "No heating circuit" leaves the zone's valves free to open for residual
    heat when the boiler is already firing for another room, without the zone
    itself ever calling for heat. Identity keys on the circuit's stable
    ``number``; the serial (`driverShortSerialNo`) is shown for display only,
    since it changes when a circuit's driver is re-paired.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "heating_circuit"

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str,
        home_id: str,
    ) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["select_heating_circuit"]
        self._zone_id = zone_id
        self._attr_unique_id = f"tado_ce_{home_id}_{zone_id}_{_meta.unique_id_suffix}"
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        self._attr_options = [NO_HEATING_CIRCUIT_OPTION]
        self._attr_current_option = NO_HEATING_CIRCUIT_OPTION
        # Pin entity_id so HA 2026.6 doesn't double the slug on same-named
        # zone/area (mirrors the Identify button).
        self.entity_id = f"select.{slugify(f'{zone_name} heating circuit')}"

    def _circuits(self) -> list[dict[str, Any]]:
        """Return the home's circuit list from the cache."""
        return ((self.coordinator.data or {}).get("heating_circuits") or {}).get("circuits") or []

    def _current_number(self) -> int | None:
        """Return this zone's currently-assigned circuit number, or None."""
        control = ((self.coordinator.data or {}).get("heating_circuit_control") or {}).get(
            self._zone_id,
        ) or {}
        return control.get("heatingCircuit")

    @property
    def icon(self) -> str | None:
        """Return the entity icon."""
        return "mdi:radiator-disabled" if (
            self._attr_current_option == NO_HEATING_CIRCUIT_OPTION
        ) else "mdi:radiator"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh options + current selection from the latest cache."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Rebuild options from the circuit list and resolve the current option.

        A live circuit number with no matching serial in the list (a mid-drift
        poll, a re-paired circuit) gets a synthetic ``circuit_<n>`` option so
        the state is always representable and never falsely reads as
        "no heating circuit".
        """
        circuits = self._circuits()
        by_number = {c.get("number"): c.get("driverShortSerialNo") for c in circuits}
        options = [NO_HEATING_CIRCUIT_OPTION]
        options.extend(str(c.get("driverShortSerialNo")) for c in circuits)

        number = self._current_number()
        if number is None:
            current = NO_HEATING_CIRCUIT_OPTION
        elif number in by_number:
            current = str(by_number[number])
        else:
            current = f"circuit_{number}"
            if current not in options:
                options.append(current)

        self._attr_options = options
        self._attr_current_option = current

    async def async_select_option(self, option: str) -> None:
        """Reassign this zone's circuit, keying the write on the stable number."""
        await _check_bootstrap_reserve_or_raise(
            self.hass, "Heating Circuit", coordinator=self.coordinator,
        )
        if option == NO_HEATING_CIRCUIT_OPTION:
            number: int | None = None
        elif option.startswith("circuit_"):
            number = int(option.split("_", 1)[1])
        else:
            by_serial = {
                str(c.get("driverShortSerialNo")): c.get("number") for c in self._circuits()
            }
            number = by_serial.get(option)

        self._attr_current_option = option
        self.async_write_ha_state()

        await self.coordinator.api_client.set_zone_heating_circuit(self._zone_id, number)
        await async_trigger_immediate_refresh(self.hass, self.entity_id, "heating_circuit_change")
