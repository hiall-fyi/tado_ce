"""Tado CE switch platform: early start, child lock, hub toggles.

Per-zone Early Start, per-device Child Lock, and the home-level
Quota Reserve toggle. Early Start / Child Lock writes go through
the device-sync queue so the API rate-limit logic gets a single
serialised stream of writes; Quota Reserve is a config-entry
option (no cloud call).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .device_manager import get_hub_device_info, get_zone_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .helpers import PerEntityAvailabilityMixin, async_trigger_immediate_refresh, mask_serial
from .optimistic_helpers import OptimisticUpdateResult, clear_optimistic_state, resolve_optimistic_update
from .ratelimit import async_check_bootstrap_reserve_or_raise as _check_bootstrap_reserve_or_raise
from .write_optimizer import DeviceOperation

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
    """Set up Tado CE switches from a config entry."""
    _LOGGER.debug("Switch: setup starting")
    coordinator = entry.runtime_data
    data_loader = coordinator.data_loader
    home_id = coordinator.home_id
    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)

    switches: list[SwitchEntity] = []

    # Device controls (Early Start, Child Lock)
    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_name = zone.get("name", f"Zone {zone.get('id')}")
            zone_type = zone.get("type")

            # Early Start switch (for heating zones that support it)
            if zone_type == "HEATING":
                early_start = zone.get("earlyStart") or {}
                if early_start.get("supported", True):  # Default to supported
                    switches.append(
                        TadoEarlyStartSwitch(
                            coordinator,
                            zone_id,
                            zone_name,
                            zone_type,
                            early_start.get("enabled", False),
                            home_id,
                        ),
                    )

            # `devices` can come back as null on a fresh zone;
            # `or []` covers both null and missing-key cases.
            for device in zone.get("devices") or []:
                if "childLockEnabled" in device:
                    serial = device.get("shortSerialNo")
                    device_type = device.get("deviceType", "unknown")
                    switches.append(
                        TadoChildLockSwitch(
                            coordinator,
                            zone_id,
                            serial,
                            zone_name,
                            zone_type,
                            device_type,
                            device.get("childLockEnabled", False),
                            zones_info,
                            home_id,
                        ),
                    )

    if switches:
        async_add_entities(switches, True)
        _LOGGER.info(
            "Switch: created %d zone / device switch(es)", len(switches),
        )
    else:
        _LOGGER.debug(
            "Switch: no zone / device switches created, device "
            "controls are disabled in config",
        )

    # Hub-level toggles always exist regardless of feature flags so
    # users can flip Quota Reserve even when device controls are off.
    hub_switches: list[SwitchEntity] = [
        TadoHubToggleSwitch(
            coordinator, home_id, "switch_quota_reserve", "quota_reserve_enabled", "mdi:shield-check", "mdi:shield-off",
        ),
    ]
    async_add_entities(hub_switches, True)


class TadoEarlyStartSwitch(CoordinatorEntity["TadoDataUpdateCoordinator"], SwitchEntity):
    """Toggle Tado's per-zone Early Start preheat.

    Early Start tells the Tado cloud to start heating ahead of a
    schedule block so the room reaches target on time. The cloud
    is the source of truth, so we surface the cached value and
    optimistically reflect the user's flip while the write goes
    through the device-sync queue.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str,
        initial_state: bool,
        home_id: str,
    ) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["switch_early_start"]
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._entry_id = coordinator.config_entry.entry_id

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_icon = _meta.icon
        self._attr_is_on = initial_state
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)

        # Optimistic update tracking
        self._optimistic_set_at: float | None = None

    @property
    def icon(self) -> str | None:
        """Return icon based on state."""
        return "mdi:clock-fast" if self._attr_is_on else "mdi:clock-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "zone_id": self._zone_id,
            "zone": self._zone_name,
            "description": "Pre-heats the room to reach target temperature on time",
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reconcile Early Start state on every coordinator poll."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Hold the optimistic value during its window; otherwise leave state unchanged.

        Early Start isn't part of the cached zone snapshot, so a
        normal poll has no fresh API value to compare against, so
        we hold the last known state until the next user toggle.
        """
        result = resolve_optimistic_update(
            self,
            api_values={},
            entry_id=self._entry_id,
        )
        if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
            _LOGGER.debug(
                "Switch: zone %s Early Start holding optimistic "
                "state, still inside the protection window",
                self._zone_name,
            )
            return

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Early Start for this zone."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"Early Start {self._zone_name}", coordinator=self.coordinator)

        old_is_on = self._attr_is_on

        self._attr_is_on = True
        self._optimistic_set_at = time.monotonic()
        self.async_write_ha_state()

        async def _execute() -> bool:
            return await self._async_set_early_start(True)

        accepted, done = await self.coordinator.device_sync_queue.enqueue(
            DeviceOperation(
                device_serial=self._zone_id,
                operation_name="early_start_on",
                callback=_execute,
                entity_id=self.entity_id,
            ),
        )
        if not accepted:
            _LOGGER.warning(
                "Switch: zone %s Early Start ON rejected, device "
                "sync queue full, will retry next time",
                self._zone_name,
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Early Start {self._zone_name}: device sync queue full",
                translation_domain=DOMAIN,
                translation_key="early_start_queue_full",
                translation_placeholders={"zone_name": self._zone_name},
            )

        await async_trigger_immediate_refresh(self.hass, self.entity_id, "early_start_on")

        # Roll back the optimistic UI if the queued write later
        # failed. Without this the dashboard shows the wrong
        # value until the next successful refresh, which may
        # itself be rate-limited.
        if not await done:
            _LOGGER.warning(
                "Switch: zone %s Early Start ON write failed, "
                "reverted UI to previous state",
                self._zone_name,
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Early Start for this zone."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"Early Start {self._zone_name}", coordinator=self.coordinator)

        old_is_on = self._attr_is_on

        self._attr_is_on = False
        self._optimistic_set_at = time.monotonic()
        self.async_write_ha_state()

        async def _execute() -> bool:
            return await self._async_set_early_start(False)

        accepted, done = await self.coordinator.device_sync_queue.enqueue(
            DeviceOperation(
                device_serial=self._zone_id,
                operation_name="early_start_off",
                callback=_execute,
                entity_id=self.entity_id,
            ),
        )
        if not accepted:
            _LOGGER.warning(
                "Switch: zone %s Early Start OFF rejected, device "
                "sync queue full, will retry next time",
                self._zone_name,
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Early Start {self._zone_name}: device sync queue full",
                translation_domain=DOMAIN,
                translation_key="early_start_queue_full",
                translation_placeholders={"zone_name": self._zone_name},
            )

        await async_trigger_immediate_refresh(self.hass, self.entity_id, "early_start_off")

        if not await done:
            _LOGGER.warning(
                "Switch: zone %s Early Start OFF write failed, "
                "reverted UI to previous state",
                self._zone_name,
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()

    async def _async_set_early_start(self, enabled: bool) -> bool:
        """Send the Early Start enable / disable to the cloud."""
        client = self.coordinator.api_client

        endpoint = f"zones/{self._zone_id}/earlyStart"
        result = await client.api_call(endpoint, method="PUT", data={"enabled": enabled})

        if result is not None:
            state_str = "enabled" if enabled else "disabled"
            _LOGGER.debug(
                "Switch: zone %s Early Start %s", self._zone_name, state_str,
            )
            self._attr_is_on = enabled
            self.async_write_ha_state()
            return True

        _LOGGER.warning(
            "Switch: zone %s Early Start write failed, keeping "
            "previous state",
            self._zone_name,
        )
        return False




class TadoChildLockSwitch(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], SwitchEntity):
    """Toggle child lock on a single Tado device.

    Each Tado device that supports child lock (TRVs, smart
    thermostats) gets its own switch. Reads the current value
    from the cached zone data and writes via the device-sync
    queue.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        serial: str,
        zone_name: str,
        zone_type: str,
        device_type: str,
        initial_state: bool,
        zones_info: list[Any],
        home_id: str,
    ) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["switch_child_lock"]
        self._zone_id = zone_id
        self._serial = serial
        self._zone_name = zone_name
        self._zone_type = zone_type
        self._device_type = device_type
        self._entry_id = coordinator.config_entry.entry_id

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(serial=serial)}"
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_icon = _meta.icon
        self._attr_is_on = initial_state
        self._data_present = True
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)

        # Optimistic update tracking
        self._optimistic_set_at: float | None = None

    @property
    def icon(self) -> str | None:
        """Return icon based on state."""
        return "mdi:lock" if self._attr_is_on else "mdi:lock-open"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "serial": self._serial,
            "device_type": self._device_type,
            "zone": self._zone_name,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reconcile child lock state on every coordinator poll."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Pick up the cloud's `childLockEnabled` for this device, respecting optimistic writes."""
        result = resolve_optimistic_update(
            self,
            api_values={},
            entry_id=self._entry_id,
        )
        if result == OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
            _LOGGER.debug(
                "Switch: zone %s child lock (%s) holding optimistic "
                "state, still inside the protection window",
                self._zone_name, mask_serial(self._serial),
            )
            return

        try:
            zones_info = (self.coordinator.data or {}).get("zones_info")

            if zones_info:
                for zone in zones_info:
                    for device in zone.get("devices") or []:
                        if device.get("shortSerialNo") == self._serial:
                            if "childLockEnabled" in device:
                                self._attr_is_on = device.get("childLockEnabled", False)
                                self._data_present = True
                                return

            # Device serial (or its childLockEnabled field) no longer present:
            # unavailable, and drop the last-known state so nothing stale lingers.
            self._attr_is_on = None
            self._data_present = False
        except Exception:
            _LOGGER.debug(
                "Switch: zone %s child lock (%s) update failed, "
                "marking unavailable until the next poll",
                self._zone_name, mask_serial(self._serial),
                exc_info=True,
            )
            self._attr_is_on = None
            self._data_present = False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Engage child lock on this device."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"Child Lock {self._zone_name}", coordinator=self.coordinator)

        old_is_on = self._attr_is_on

        self._attr_is_on = True
        self._optimistic_set_at = time.monotonic()
        self.async_write_ha_state()

        async def _execute() -> bool:
            return await self._async_set_child_lock(True)

        accepted, done = await self.coordinator.device_sync_queue.enqueue(
            DeviceOperation(
                device_serial=self._serial,
                operation_name="child_lock_on",
                callback=_execute,
                entity_id=self.entity_id,
            ),
        )
        if not accepted:
            _LOGGER.warning(
                "Switch: zone %s child lock (%s) ON rejected, "
                "device sync queue full, will retry next time",
                self._zone_name, mask_serial(self._serial),
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Child Lock {self._zone_name}: device sync queue full",
                translation_domain=DOMAIN,
                translation_key="child_lock_queue_full",
                translation_placeholders={"zone_name": self._zone_name},
            )

        await async_trigger_immediate_refresh(self.hass, self.entity_id, "child_lock_on")

        if not await done:
            _LOGGER.warning(
                "Switch: zone %s child lock (%s) ON write failed, "
                "reverted UI to previous state",
                self._zone_name, mask_serial(self._serial),
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Release child lock on this device."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"Child Lock {self._zone_name}", coordinator=self.coordinator)

        old_is_on = self._attr_is_on

        self._attr_is_on = False
        self._optimistic_set_at = time.monotonic()
        self.async_write_ha_state()

        async def _execute() -> bool:
            return await self._async_set_child_lock(False)

        accepted, done = await self.coordinator.device_sync_queue.enqueue(
            DeviceOperation(
                device_serial=self._serial,
                operation_name="child_lock_off",
                callback=_execute,
                entity_id=self.entity_id,
            ),
        )
        if not accepted:
            _LOGGER.warning(
                "Switch: zone %s child lock (%s) OFF rejected, "
                "device sync queue full, will retry next time",
                self._zone_name, mask_serial(self._serial),
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Child Lock {self._zone_name}: device sync queue full",
                translation_domain=DOMAIN,
                translation_key="child_lock_queue_full",
                translation_placeholders={"zone_name": self._zone_name},
            )

        await async_trigger_immediate_refresh(self.hass, self.entity_id, "child_lock_off")

        if not await done:
            _LOGGER.warning(
                "Switch: zone %s child lock (%s) OFF write failed, "
                "reverted UI to previous state",
                self._zone_name, mask_serial(self._serial),
            )
            self._attr_is_on = old_is_on
            clear_optimistic_state(self)
            self.async_write_ha_state()

    async def _async_set_child_lock(self, enabled: bool) -> bool:
        """Send the child lock enable / disable to the cloud."""
        return bool(await self.coordinator.api_client.set_child_lock(self._serial, enabled))


class TadoHubToggleSwitch(CoordinatorEntity["TadoDataUpdateCoordinator"], SwitchEntity):
    """Persist a hub-level config toggle (e.g. Quota Reserve) on the config entry.

    No cloud call: flipping the switch updates the config-entry
    options dict, which `ConfigurationManager` reads in real time
    so the change applies on the next poll cycle.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        home_id: str,
        registry_key: str,
        option_key: str,
        icon_on: str,
        icon_off: str,
    ) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY[registry_key]
        self._option_key = option_key
        self._icon_on = icon_on
        self._icon_off = icon_off

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix}"
        self._attr_device_info = get_hub_device_info(home_id)
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_is_on = self._read_option()

    def _read_option(self) -> bool:
        """Read current option value from config entry."""
        entry = self.coordinator.config_entry
        result = entry.options.get(self._option_key, self._attr_is_on)
        return bool(result)

    @property
    def icon(self) -> str | None:
        """Return icon based on state."""
        return self._icon_on if self._attr_is_on else self._icon_off

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update."""
        self._attr_is_on = self._read_option()
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the toggle."""
        await self._async_set_option(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the toggle."""
        await self._async_set_option(False)

    async def _async_set_option(self, value: bool) -> None:
        """Write the new toggle value to the config entry's options dict."""
        entry = self.coordinator.config_entry
        new_options = {**entry.options, self._option_key: value}
        self.hass.config_entries.async_update_entry(entry, options=new_options)
        self._attr_is_on = value
        self.async_write_ha_state()
        _LOGGER.info(
            "Switch: %s set to %s", self._option_key, value,
        )
