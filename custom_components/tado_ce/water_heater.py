"""Tado CE water-heater platform: hot-water entity with timer + manual overlays.

One entity per HOT_WATER zone. Operation modes: AUTO (follow Tado
schedule), HEAT (timer / manual on), OFF (manual off). Temperature
control is exposed conditionally: only on systems where Tado
returns a `temperature.celsius` setting (most combi boilers
don't, solar / store systems do).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MAX_RETRY_ATTEMPTS
from .device_manager import get_zone_device_info
from .entity_registry import ENTITY_REGISTRY
from .format_helpers import format_overlay_type as _format_overlay_type
from .helpers import (
    PerEntityAvailabilityMixin,
    async_retry_with_backoff,
    async_trigger_immediate_refresh,
    build_timer_termination,
    get_zone_state,
)
from .optimistic_helpers import (
    OptimisticUpdateResult,
    clear_optimistic_state,
    resolve_optimistic_update,
    set_optimistic_fields,
)
from .ratelimit import async_check_bootstrap_reserve_or_raise as _check_bootstrap_reserve_or_raise
from .water_heater_helpers import api_call_with_rollback_wh

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

# Operation modes for hot water
STATE_AUTO = "auto"  # Follow schedule (no overlay)
STATE_HEAT = "heat"  # Timer or manual heating
OPERATION_MODES = [STATE_AUTO, STATE_HEAT, STATE_OFF]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE water heater from a config entry."""
    _LOGGER.debug("Water Heater: setting up entities")
    coordinator = entry.runtime_data
    data_loader = coordinator.data_loader
    home_id = coordinator.home_id
    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)

    water_heaters = []

    if zones_info:
        _LOGGER.debug("Water Heater: scanning %d zone(s)", len(zones_info))
        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_name = zone.get("name", f"Zone {zone_id}")
            zone_type = zone.get("type")

            if zone_type == "HOT_WATER":
                _LOGGER.debug(
                    "Water Heater: creating entity for zone %s (%s)",
                    zone_id, zone_name,
                )
                water_heaters.append(TadoWaterHeater(coordinator, zone_id, zone_name, home_id))

    if water_heaters:
        async_add_entities(water_heaters, True)
        _LOGGER.info(
            "Water Heater: created %d hot-water entity(ies)", len(water_heaters),
        )
    else:
        _LOGGER.debug("Water Heater: no hot-water zones found in this home")


class TadoWaterHeater(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], WaterHeaterEntity, RestoreEntity):
    """Tado CE Water Heater Entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, home_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = home_id
        self._entry_id = coordinator.config_entry.entry_id

        _meta = ENTITY_REGISTRY["water_heater_hot_water"]
        self._attr_name = None
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_min_temp = 30
        self._attr_max_temp = 65
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HOT_WATER", home_id)

        self._attr_current_operation = None
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._data_present = False

        # Supported features - will be updated based on zone capabilities
        self._supports_temperature = False
        self._attr_supported_features = WaterHeaterEntityFeature.OPERATION_MODE
        self._attr_operation_list = OPERATION_MODES

        self._overlay_type = None
        self._entity_type = "water_heater"

        # Optimistic update tracking
        self._optimistic_set_at: float | None = None

        # Layer 2: Sequence number tracking
        self._optimistic_sequence: int | None = None
        # Layer 3: Expected state confirmation
        self._expected_operation: str | None = None
        self._expected_temperature: float | None = None

    # ------------------------------------------------------------------
    async def async_added_to_hass(self) -> None:
        """Restore the last-known target temperature across an HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get(ATTR_TEMPERATURE) is not None:
            self._attr_target_temperature = last_state.attributes[ATTR_TEMPERATURE]
            _LOGGER.debug(
                "Water Heater: %s restored target %s°C from previous HA state",
                self._zone_name,
                self._attr_target_temperature,
            )
        # No sensible universal default for a water heater, so leave None and
        # let _resolve_on_temperature handle the fallback (last-known, caps.max).

    # Public API (TadoZoneEntity Protocol, see entity_types.py)
    # ------------------------------------------------------------------

    @property
    def zone_id(self) -> str:
        """Return the Tado zone ID as a string."""
        return str(self._zone_id)

    @property
    def zone_type(self) -> str:
        """Return the zone type, always HOT_WATER for this entity."""
        return "HOT_WATER"

    @property
    def entity_type(self) -> str:
        """Return the entity type tag for state-capture routing."""
        return self._entity_type

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "overlay_type": _format_overlay_type(self._overlay_type),
            "zone_id": self._zone_id,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update."""
        self.update()
        self.async_write_ha_state()

    def _extract_zone_data(self) -> dict[str, Any] | None:
        """Return the cached zone data when available + ONLINE, else None."""
        coord_data = self.coordinator.data or {}
        zone_data: dict[str, Any] | None = get_zone_state(coord_data, self._zone_id)
        if not zone_data:
            _LOGGER.debug(
                "Water Heater: %s has no cached zone data, entity will be unavailable",
                self._zone_name,
            )
            return None

        link = zone_data.get("link") or {}
        if link.get("state") != "ONLINE":
            _LOGGER.debug(
                "Water Heater: %s link state %s, entity unavailable until "
                "the zone reconnects",
                self._zone_name, link.get("state"),
            )
            return None

        return zone_data

    def _resolve_api_operation(
        self, overlay: dict[str, Any] | None, api_overlay_type: str | None, power: str | None,
    ) -> str:
        """Determine API operation mode from overlay state.

        Tado's `overlayType` only ever reports that an overlay exists
        ("MANUAL"), not which termination it carries, so a power-ON overlay
        of any termination (including a timer) reads STATE_HEAT here.
        """
        if not overlay or api_overlay_type is None:
            return STATE_AUTO
        if api_overlay_type == "MANUAL":
            return STATE_OFF if power == "OFF" else STATE_HEAT
        return STATE_AUTO

    @callback
    def update(self) -> None:
        """Refresh entity state from coordinator data, honouring the optimistic window."""
        _LOGGER.debug(
            "Water Heater: update() for %s (zone %s)",
            self._zone_name, self._zone_id,
        )

        if self.coordinator.is_entity_fresh(self.entity_id):
            # Safety net for the boot-freshness false-positive: a
            # restart can mark an entity fresh before it has any
            # data, so never skip when there's nothing cached yet.
            if self._attr_current_operation is not None:
                _LOGGER.debug(
                    "Water Heater: %s skipping update, entity already fresh",
                    self._zone_name,
                )
                return
            _LOGGER.debug(
                "Water Heater: %s marked fresh but has no cached state yet "
                ", updating anyway",
                self._zone_name,
            )

        try:
            zone_data = self._extract_zone_data()
            if not zone_data:
                self._data_present = False
                return

            _LOGGER.debug(
                "Water Heater: %s zone is ONLINE, entity available",
                self._zone_name,
            )
            setting = zone_data.get("setting") or {}
            power = setting.get("power")
            overlay = zone_data.get("overlay")
            api_overlay_type = zone_data.get("overlayType")

            temp_data = setting.get("temperature") or {}
            api_target_temp = temp_data.get("celsius")

            # The poll flag is only a cold-boot fallback; the feature set is
            # driven by the capability so the slider shows before any poll.
            if api_target_temp is not None:
                self._supports_temperature = True
            self._attr_supported_features = (
                WaterHeaterEntityFeature.OPERATION_MODE | WaterHeaterEntityFeature.TARGET_TEMPERATURE
                if self._zone_can_set_temperature()
                else WaterHeaterEntityFeature.OPERATION_MODE
            )

            api_operation = self._resolve_api_operation(overlay, api_overlay_type, power)

            result = resolve_optimistic_update(
                self,
                api_values={"operation": api_operation, "temperature": api_target_temp},
                entry_id=self._entry_id,
            )

            if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
                if self._expected_operation is not None:
                    self._attr_current_operation = self._expected_operation
                if self._expected_temperature is not None:
                    self._attr_target_temperature = self._expected_temperature
                _LOGGER.debug(
                    "Water Heater: %s holding optimistic state, "
                    "operation=%s temp=%s",
                    self._zone_name,
                    self._attr_current_operation,
                    self._attr_target_temperature,
                )
            else:
                self._attr_current_operation = api_operation
                self._overlay_type = api_overlay_type
                if api_target_temp is not None:
                    self._attr_target_temperature = api_target_temp
                # OFF tank reports no target, so keep the last-known one rather
                # than clobbering to None, else Heat later falls back to tank max.

            self._data_present = True

        except FileNotFoundError as e:
            _LOGGER.warning(
                "Water Heater: %s data file missing (%s), entity "
                "unavailable until cache rebuilds",
                self.name, e,
            )
            self._data_present = False
        except json.JSONDecodeError as e:
            _LOGGER.warning(
                "Water Heater: %s data file is corrupt JSON (%s), entity "
                "unavailable until next successful poll",
                self.name, e,
            )
            self._data_present = False
        except Exception:
            _LOGGER.warning(
                "Water Heater: %s update failed unexpectedly, entity "
                "marked unavailable, will retry on next poll",
                self.name, exc_info=True,
            )
            self._data_present = False

    async def _execute_operation_mode(self, operation_mode: str) -> bool:
        """Execute a single attempt to set the operation mode via API."""
        client = self.coordinator.api_client
        if operation_mode == STATE_AUTO:
            # A hot-water zone already on schedule has no overlay to clear, so
            # the cloud call is a no-op; skip it to save quota.
            from .write_optimizer import ResumeGuard

            if ResumeGuard.should_skip_resume(self.coordinator, self._zone_id):
                _LOGGER.debug(
                    "Water Heater: %s already on schedule, skipping redundant resume",
                    self._zone_name,
                )
                return True
            success = await client.delete_zone_overlay(self._zone_id)
            if success:
                _LOGGER.debug(
                    "Water Heater: %s schedule resumed", self._zone_name,
                )
                await async_trigger_immediate_refresh(self.hass, self.entity_id, "hot_water_auto")
            return bool(success)

        if operation_mode == STATE_HEAT:
            duration = self._get_timer_duration()
            success = await self._async_set_timer(duration, None)
            if success:
                await async_trigger_immediate_refresh(self.hass, self.entity_id, "hot_water_heat")
            return success

        if operation_mode == STATE_OFF:
            success = await self._async_turn_off()
            if success:
                await async_trigger_immediate_refresh(self.hass, self.entity_id, "hot_water_off")
            return success

        return False

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Set new operation mode with retry logic (async)."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"hot water {self._zone_name}", coordinator=self.coordinator)

        previous_mode = self._attr_current_operation
        previous_overlay = self._overlay_type

        # Optimistic update BEFORE API call
        self._attr_current_operation = operation_mode
        if operation_mode == STATE_AUTO:
            self._overlay_type = None
        elif operation_mode == STATE_HEAT:
            self._overlay_type = "TIMER"  # type: ignore[assignment]
        elif operation_mode == STATE_OFF:
            self._overlay_type = "MANUAL"  # type: ignore[assignment]

        # Set optimistic fields using shared helper
        await set_optimistic_fields(
            self, self.coordinator,
            expected={"operation": operation_mode},
        )

        _LOGGER.debug(
            "Water Heater: %s optimistic state set, operation=%s, seq=%s",
            self._zone_name,
            operation_mode,
            self._optimistic_sequence,
        )

        self.async_write_ha_state()

        success = False
        try:
            async def _try_set_mode() -> bool:
                result = await self._execute_operation_mode(operation_mode)
                if not result:
                    msg = "Operation mode set returned False"
                    raise HomeAssistantError(msg)
                return result

            success = await async_retry_with_backoff(
                _try_set_mode,
                retryable_exceptions=(HomeAssistantError,),
            )
        except HomeAssistantError:
            success = False

        if not success:
            _LOGGER.warning(
                "Water Heater: %s could not switch to %s after %d "
                "attempts, reverting to previous state",
                self._zone_name, operation_mode, MAX_RETRY_ATTEMPTS,
            )
            self._attr_current_operation = previous_mode
            self._overlay_type = previous_overlay
            clear_optimistic_state(self)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Hot water {self._zone_name}: Set {operation_mode} failed after {MAX_RETRY_ATTEMPTS} attempts",
                translation_domain=DOMAIN,
            )

    def set_operation_mode(self, operation_mode: str) -> None:
        """Sync stub kept for back-compat; HA calls async_set_operation_mode directly."""
        # Home Assistant handles async methods automatically

    def _get_timer_duration(self) -> int:
        """Get configured timer duration in minutes (default 60)."""
        try:
            config_manager = self.coordinator.config_manager
            if config_manager:
                return int(config_manager.get_hot_water_timer_duration())
        except (AttributeError, TypeError) as e:
            _LOGGER.debug(
                "Water Heater: could not read timer duration from config "
                "(%s), using 60-minute default",
                e,
            )

        # Default to 60 minutes
        return 60


    async def _async_turn_off(self) -> bool:
        """Set the zone overlay to power=OFF, returning success."""
        if not self._home_id:
            _LOGGER.warning(
                "Water Heater: %s has no home_id configured, cannot "
                "turn off, re-authenticate the integration to fix",
                self._zone_name,
            )
            return False

        client = self.coordinator.api_client

        setting = {"type": "HOT_WATER", "power": "OFF"}
        termination = build_timer_termination(overlay="manual")

        success = await client.set_zone_overlay(self._zone_id, setting, termination)
        if success:
            await self.coordinator.async_capture_state(
                self._zone_id, self._entity_type, "manual_override",
            )
            _LOGGER.debug(
                "Water Heater: %s turned off", self._zone_name,
            )
            self._attr_current_operation = STATE_OFF
        return bool(success)

    async def _async_set_timer(self, duration_minutes: int, temperature: float | None = None) -> bool:
        """Set a timer overlay turning the zone on for `duration_minutes`."""
        if not self._home_id:
            _LOGGER.warning(
                "Water Heater: %s has no home_id configured, cannot "
                "set timer, re-authenticate the integration to fix",
                self._zone_name,
            )
            return False

        client = self.coordinator.api_client

        setting: dict[str, Any] = {"type": "HOT_WATER", "power": "ON"}

        # Temperature-capable DHW zones (solar / store systems) require a target
        # on an ON overlay; combi (ON/OFF-only) zones reject one. Gate on the
        # authoritative capability, not the poll-derived flag.
        if self._zone_can_set_temperature():
            setting["temperature"] = {"celsius": self._resolve_on_temperature(temperature)}

        termination = build_timer_termination(duration_minutes=duration_minutes)

        success = await client.set_zone_overlay(self._zone_id, setting, termination)
        if success:
            await self.coordinator.async_capture_state(
                self._zone_id, self._entity_type, "set_timer",
            )
            sent_temp = setting.get("temperature", {}).get("celsius")
            temp_str = f" at {sent_temp}°C" if sent_temp is not None else ""
            _LOGGER.debug(
                "Water Heater: %s timer set for %d min%s",
                self._zone_name, duration_minutes, temp_str,
            )
            self._attr_current_operation = STATE_HEAT
        return bool(success)

    def _zone_can_set_temperature(self) -> bool:
        """Return whether this DHW zone's overlay accepts a target temperature.

        Reads the authoritative `canSetTemperature` capability (survives OFF /
        restart, unlike the poll-derived `_supports_temperature`), falling back
        to that flag only while the capability cache is not yet populated.
        """
        caps = (self.coordinator.data or {}).get("ac_capabilities") or {}
        zone_caps = caps.get(self._zone_id)
        if isinstance(zone_caps, dict) and "canSetTemperature" in zone_caps:
            return bool(zone_caps["canSetTemperature"])
        return self._supports_temperature

    def _resolve_on_temperature(self, explicit: float | None) -> float:
        """Pick the ON-overlay celsius: explicit, then last-known, then caps max, then 65."""
        if explicit is not None:
            return explicit
        if self._attr_target_temperature is not None:
            return self._attr_target_temperature
        caps = (self.coordinator.data or {}).get("ac_capabilities") or {}
        zone_caps = caps.get(self._zone_id) or {}
        cap_max = ((zone_caps.get("temperatures") or {}).get("celsius") or {}).get("max")
        if isinstance(cap_max, (int, float)):
            return float(cap_max)
        return float(self._attr_max_temp)

    async def async_set_timer(self, duration_minutes: int, temperature: float | None = None) -> bool:
        """Public async method to set timer (for service calls)."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"hot water {self._zone_name}", coordinator=self.coordinator)

        success = await self._async_set_timer(duration_minutes, temperature)
        if success:
            await async_trigger_immediate_refresh(self.hass, self.entity_id, "hot_water_timer")
        return success

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Apply a target temperature with optimistic update + rollback."""
        temperature = kwargs.get("temperature")
        if temperature is None:
            _LOGGER.warning(
                "Water Heater: %s set-temperature called without a "
                "temperature value, service call ignored",
                self._zone_name,
            )
            return

        if not self._zone_can_set_temperature():
            _LOGGER.warning(
                "Water Heater: %s does not support temperature control "
                "(combi system), set-temperature ignored",
                self._zone_name,
            )
            return

        if not self._home_id:
            _LOGGER.warning(
                "Water Heater: %s has no home_id configured, cannot "
                "set temperature, re-authenticate the integration to fix",
                self._zone_name,
            )
            return

        await _check_bootstrap_reserve_or_raise(self.hass, f"hot water {self._zone_name}", coordinator=self.coordinator)

        self._overlay_type = "MANUAL"  # type: ignore[assignment]

        client = self.coordinator.api_client
        setting = {
            "type": "HOT_WATER",
            "power": "ON",
            "temperature": {"celsius": temperature},
        }
        termination = build_timer_termination(overlay="manual")

        await api_call_with_rollback_wh(
            self,
            client.set_zone_overlay(self._zone_id, setting, termination),
            operation=STATE_HEAT,
            target_temp=temperature,
            reason=f"set hot water to {temperature}°C",
            capture_source="manual_override",
        )
