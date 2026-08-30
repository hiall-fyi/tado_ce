"""Tado CE button platform: resume schedules, refresh caches, timer, boost."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import TadoDataUpdateCoordinator
from .device_manager import get_device_name_suffix, get_hub_device_info, get_zone_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .helpers import (
    async_trigger_immediate_refresh,
    build_timer_termination,
    get_zone_states,
    mask_serial,
    merge_homekit_into_zone_data,
)
from .ratelimit import async_check_bootstrap_reserve_or_raise as _check_bootstrap_reserve_or_raise
from .services_helpers import run_service_call

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry

_LOGGER = logging.getLogger(__name__)

_MIN_BOOST_HEATING_RATE = 0.1  # °C/h: minimum meaningful rate for smart boost calculation

PARALLEL_UPDATES = 1

# Default timer preset durations (in minutes)
DEFAULT_TIMER_PRESETS = [30, 60, 90]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE buttons from a config entry."""
    _LOGGER.debug("Button: setup starting")
    coordinator = entry.runtime_data
    data_loader = coordinator.data_loader
    home_id = coordinator.home_id
    zones_info = await hass.async_add_executor_job(data_loader.load_zones_info_file)

    buttons: list[ButtonEntity] = []
    # A physical device can appear under more than one zone in zones_info; one
    # identify button per serial, attached to the first zone it's seen in.
    seen_identify_serials: set[str] = set()

    # Add Resume All Schedules button (hub-level)
    buttons.append(TadoResumeAllSchedulesButton(coordinator, home_id))

    # Add Refresh AC Capabilities button (hub-level) - only if there are AC zones
    has_ac_zones = any(z.get("type") == "AIR_CONDITIONING" for z in (zones_info or []))
    if has_ac_zones:
        buttons.append(TadoRefreshACCapabilitiesButton(coordinator, home_id))

    if zones_info:
        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_name = zone.get("name", f"Zone {zone_id}")
            zone_type = zone.get("type")

            # Create timer preset buttons for hot water zones
            if zone_type == "HOT_WATER":
                buttons.extend(
                    TadoWaterHeaterTimerButton(coordinator, zone_id, zone_name, duration, home_id)
                    for duration in DEFAULT_TIMER_PRESETS
                )

            # Create boost buttons for heating zones
            if zone_type == "HEATING":
                # Boost button (official Tado-style: max temp for 30 min)
                buttons.append(
                    TadoBoostButton(coordinator, zone_id, zone_name, home_id),
                )
                # Smart Boost button (calculated duration based on heating rate)
                buttons.append(
                    TadoSmartBoostButton(coordinator, zone_id, zone_name, home_id),
                )

            # Create refresh schedule button for heating and AC zones
            if zone_type in ("HEATING", "AIR_CONDITIONING"):
                buttons.append(
                    TadoRefreshScheduleButton(coordinator, zone_id, zone_name, zone_type, home_id),
                )

            # One identify button per physical device. Multi-TRV zones
            # get one per device so each radiator can be flashed individually.
            # A device shared across zones gets a single button (its serial is
            # the unique ID, so a duplicate would collide).
            for device in zone.get("devices") or []:
                serial = device.get("shortSerialNo")
                if serial and serial not in seen_identify_serials:
                    seen_identify_serials.add(serial)
                    buttons.append(
                        TadoIdentifyButton(
                            coordinator, zone_id, zone_name, zone_type, device, zones_info,
                        ),
                    )

    if buttons:
        async_add_entities(buttons, True)
        _LOGGER.info("Button: created %d button entity(ies)", len(buttons))
    else:
        _LOGGER.debug(
            "Button: no buttons to create, calendar / boost / "
            "AC features all disabled in config",
        )


class TadoResumeAllSchedulesButton(CoordinatorEntity[TadoDataUpdateCoordinator], ButtonEntity):
    """Resume scheduled heating for every zone in one press (delete all overlays)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator, home_id: str) -> None:
        """Initialize the TadoResumeAllSchedulesButton."""
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["button_resume_all"]
        # Convenience alias for entry identification
        self._entry_id = coordinator.config_entry.entry_id

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix}"
        self._attr_device_info = get_hub_device_info(home_id)
        self._attr_icon = _meta.icon

    async def async_press(self) -> None:
        """Delete every zone's overlay in sequence, then trigger an immediate refresh."""
        await _check_bootstrap_reserve_or_raise(self.hass, "Immediate Refresh", coordinator=self.coordinator)

        _LOGGER.debug("Button: Resume All Schedules pressed")

        client = self.coordinator.api_client
        zones_info = await self.hass.async_add_executor_job(self.coordinator.data_loader.load_zones_info_file)

        if not zones_info:
            _LOGGER.warning(
                "Button: Resume All Schedules, no zones available "
                "yet, will retry on the next poll",
            )
            return

        from .write_optimizer import ResumeGuard

        success_count = 0
        fail_count = 0

        for zone in zones_info:
            zone_id = str(zone.get("id"))
            zone_name = zone.get("name", f"Zone {zone_id}")

            # Skip the cloud call for zones already on schedule (no overlay).
            if ResumeGuard.should_skip_resume(self.coordinator, zone_id):
                _LOGGER.debug(
                    "Button: %s (zone %s) already on schedule, skipping",
                    zone_name, zone_id,
                )
                success_count += 1
                continue

            try:
                if await client.delete_zone_overlay(zone_id):
                    _LOGGER.debug(
                        "Button: resumed schedule for %s (zone %s)",
                        zone_name, zone_id,
                    )
                    success_count += 1
                else:
                    # API returns False when no overlay exists, which
                    # is the desired end state, count as success.
                    _LOGGER.debug(
                        "Button: %s (zone %s) had no overlay, "
                        "schedule already active",
                        zone_name, zone_id,
                    )
                    success_count += 1
            except Exception:
                _LOGGER.warning(
                    "Button: Resume All Schedules failed for %s, "
                    "the zone will need to be resumed manually",
                    zone_name,
                    exc_info=True,
                )
                fail_count += 1

        if fail_count == 0:
            _LOGGER.info(
                "Button: Resume All Schedules complete: %d zone(s) "
                "processed",
                success_count,
            )
        else:
            _LOGGER.warning(
                "Button: Resume All Schedules: %d succeeded, %d "
                "failed",
                success_count, fail_count,
            )

        await async_trigger_immediate_refresh(
            self.hass,
            self.entity_id,
            "resume_all_schedules",
            force=True,
            skip_debounce=True,
        )


class TadoRefreshACCapabilitiesButton(CoordinatorEntity[TadoDataUpdateCoordinator], ButtonEntity):
    """Drop the cached AC capabilities and re-fetch from the cloud.

    Useful when a unit's reported fan / swing options change
    (firmware bump, replaced AC), the cache normally only
    refreshes on integration setup.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator, home_id: str) -> None:
        """Initialize the TadoRefreshACCapabilitiesButton."""
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["button_refresh_ac"]
        self._entry_id = coordinator.config_entry.entry_id

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix}"
        self._attr_device_info = get_hub_device_info(home_id)
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_icon = _meta.icon

    async def async_press(self) -> None:
        """Mark the `ac_capabilities` cache dirty and re-fetch from the cloud."""
        _LOGGER.debug("Button: Refresh AC Capabilities pressed")

        self.coordinator.data_loader.mark_cache_dirty("ac_capabilities")

        client = self.coordinator.api_client
        zones_info = self.coordinator.data_loader.get_cached("zones_info")

        if not zones_info or not isinstance(zones_info, list):
            _LOGGER.warning(
                "Button: Refresh AC Capabilities, no zones available "
                "yet, will retry after the next zone fetch",
            )
            return

        try:
            await client._sync_ac_capabilities(zones_info)
            _LOGGER.info(
                "Button: AC capabilities refreshed, entities will "
                "pick up the new shape on the next coordinator update",
            )

            await self.coordinator.async_request_refresh()
            _LOGGER.debug(
                "Button: triggered coordinator refresh after AC "
                "capability re-fetch",
            )
        except Exception:
            _LOGGER.warning(
                "Button: AC capabilities refresh failed, keeping "
                "the previous capability cache, will retry on the "
                "next press",
                exc_info=True,
            )


class TadoWaterHeaterTimerButton(CoordinatorEntity[TadoDataUpdateCoordinator], ButtonEntity):
    """Set the water heater on a timer with a preset duration (30 / 60 / 90 minutes)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, duration: int, home_id: str,
    ) -> None:
        """Initialise one timer-preset button for the given zone + duration."""
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["button_timer"]
        self._entry_id = coordinator.config_entry.entry_id
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._duration = duration

        self._attr_name = f"{duration}min Timer"
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id, duration=duration)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HOT_WATER", home_id)
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_icon = _meta.icon

    async def async_press(self) -> None:
        """Apply the preset timer to the matching water heater entity."""
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers import entity_registry as er

        _LOGGER.debug(
            "Button: timer pressed: %s for %s minutes",
            self._zone_name, self._duration,
        )

        await self.coordinator.async_capture_state(
            self._zone_id, "water_heater", "set_timer",
        )

        # Look the entity up by unique_id rather than constructing
        # `water_heater.<slug>`, HA adds `_2` / `_3` suffixes when
        # entity-id collisions occur, and the unique_id is stable.
        registry = er.async_get(self.hass)
        unique_id = f"tado_ce_{self.coordinator.home_id}_zone_{self._zone_id}_water_heater"
        entry = registry.async_get_entity_id("water_heater", DOMAIN, unique_id)

        if entry:
            water_heater_entity_id = entry
        else:
            # Fallback for entries created before unique_id became stable.
            water_heater_entity_id = f"water_heater.{slugify(self._zone_name)}"

        if not self.hass.states.get(water_heater_entity_id):
            _LOGGER.warning(
                "Button: timer failed, water heater %s not found in "
                "the entity registry (was the integration reloaded?)",
                water_heater_entity_id,
            )
            raise HomeAssistantError(
                f"Water heater entity not found: {water_heater_entity_id}",
                translation_domain=DOMAIN,
                translation_key="water_heater_not_found",
                translation_placeholders={"entity_id": water_heater_entity_id},
            )

        hours = self._duration // 60
        minutes = self._duration % 60
        time_period = f"{hours:02d}:{minutes:02d}:00"

        _LOGGER.debug(
            "Button: calling set_water_heater_timer on %s with %s",
            water_heater_entity_id, time_period,
        )

        try:
            await self.hass.services.async_call(
                "tado_ce",
                "set_water_heater_timer",
                {
                    "entity_id": water_heater_entity_id,
                    "time_period": time_period,
                },
                blocking=True,
            )

            _LOGGER.debug(
                "Button: timer set: %s for %s minutes",
                self._zone_name, self._duration,
            )

        except HomeAssistantError:
            raise
        except Exception as e:
            _LOGGER.warning(
                "Button: %d-minute timer for %s failed unexpectedly "
                "(%s), wrapping as HomeAssistantError so the UI "
                "surfaces the failure",
                self._duration, self._zone_name, e,
                exc_info=True,
            )
            raise HomeAssistantError(
                f"Failed to set {self._duration}min timer for {self._zone_name}: {e}",
                translation_domain=DOMAIN,
                translation_key="timer_set_failed",
                translation_placeholders={
                    "duration": str(self._duration),
                    "zone_name": self._zone_name,
                    "error": str(e),
                },
            ) from e


class TadoRefreshScheduleButton(CoordinatorEntity[TadoDataUpdateCoordinator], ButtonEntity):
    """Re-fetch a single zone's schedule and notify its calendar entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str,
        home_id: str,
    ) -> None:
        """Initialise one Refresh Schedule button for a heating or AC zone."""
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["button_refresh_schedule"]
        self._entry_id = coordinator.config_entry.entry_id
        self._zone_id = zone_id
        self._zone_name = zone_name

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, home_id)
        # No entity_category = Controls section (action button, not config)
        self._attr_icon = _meta.icon

    async def async_press(self) -> None:
        """Pull the latest schedule for this zone and write it through the cache."""
        await _check_bootstrap_reserve_or_raise(
            self.hass, f"Refresh Schedule {self._zone_name}", coordinator=self.coordinator,
        )

        _LOGGER.debug(
            "Button: Refresh Schedule pressed for %s (zone %s)",
            self._zone_name, self._zone_id,
        )

        client = self.coordinator.api_client

        try:
            schedule_data = await client.get_zone_schedule(self._zone_id)

            if not schedule_data:
                _LOGGER.warning(
                    "Button: schedule fetch for %s returned no data, "
                    "calendar will keep the previous schedule",
                    self._zone_name,
                )
                return

            cached = self.coordinator.data_loader.get_cached("schedules")
            schedules: dict[str, Any] = dict(cached) if isinstance(cached, dict) else {}

            schedules[self._zone_id] = {
                "name": self._zone_name,
                "type": schedule_data.get("type", "ONE_DAY"),
                "blocks": schedule_data.get("blocks") or {},
                "failed_days": schedule_data.get("failed_days") or [],
            }

            await self.coordinator.data_loader.async_update_store("schedules", schedules)

            _LOGGER.debug(
                "Button: schedule refreshed for %s, cache updated",
                self._zone_name,
            )

            self.hass.bus.async_fire(
                f"{DOMAIN}_schedule_updated",
                {"zone_id": self._zone_id, "zone_name": self._zone_name},
            )

        except Exception:
            _LOGGER.warning(
                "Button: schedule refresh for %s failed, calendar "
                "will keep the previous schedule until the next press",
                self._zone_name,
                exc_info=True,
            )


# Boost button constants
BOOST_TEMPERATURE = 25.0  # Maximum temperature for boost
BOOST_DURATION_MINUTES = 30  # Default boost duration

# Smart Boost constants
SMART_BOOST_MIN_DURATION = 15  # Minimum duration in minutes
SMART_BOOST_MAX_DURATION = 180  # Maximum duration in minutes (3 hours)
SMART_BOOST_DEFAULT_RATE = 1.0  # Default heating rate if unknown (°C/h)


class TadoBoostButton(CoordinatorEntity[TadoDataUpdateCoordinator], ButtonEntity):
    """Mirror the Tado app boost: 25°C for 30 minutes, then back to schedule."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, home_id: str) -> None:
        """Initialize the TadoBoostButton."""
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["button_boost"]
        self._entry_id = coordinator.config_entry.entry_id
        self._zone_id = zone_id
        self._zone_name = zone_name

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HEATING", home_id)
        self._attr_icon = _meta.icon

    async def async_press(self) -> None:
        """Apply the 25°C / 30-minute boost overlay to this zone."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"Boost {self._zone_name}", coordinator=self.coordinator)

        _LOGGER.debug("Button: Boost pressed for %s", self._zone_name)

        client = self.coordinator.api_client

        setting = {
            "type": "HEATING",
            "power": "ON",
            "temperature": {"celsius": BOOST_TEMPERATURE},
        }
        termination = build_timer_termination(duration_minutes=BOOST_DURATION_MINUTES)

        api_success = await run_service_call(
            hass=self.hass,
            coordinator=self.coordinator,
            zone_id=self._zone_id,
            entity_type="climate_heating",
            api_coro=client.set_zone_overlay(self._zone_id, setting, termination),
            capture_source="boost",
            refresh_entity_id=self.entity_id,
            reason="boost",
        )

        if api_success:
            _LOGGER.info(
                "Button: boosted %s to %s°C for %s minutes",
                self._zone_name,
                BOOST_TEMPERATURE,
                BOOST_DURATION_MINUTES,
            )
        else:
            _LOGGER.warning(
                "Button: boost for %s could not be activated, "
                "schedule remains unchanged",
                self._zone_name,
            )


class TadoSmartBoostButton(CoordinatorEntity[TadoDataUpdateCoordinator], ButtonEntity):
    """Boost a zone for a duration calculated from current vs. target temperature.

    Uses the zone's measured heating rate to size the timer so
    the boost lands exactly when the schedule target is reached,
    capped between 15 minutes and 3 hours.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, home_id: str) -> None:
        """Initialize the TadoSmartBoostButton."""
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["button_smart_boost"]
        self._entry_id = coordinator.config_entry.entry_id
        self._zone_id = zone_id
        self._zone_name = zone_name

        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, "HEATING", home_id)
        self._attr_icon = _meta.icon

    def _get_heating_rate(self) -> float:
        """Pick the best available heating rate for duration sizing.

        Prefers the heating-cycle coordinator's measured rate
        over Smart Comfort's running average, falling back to a
        sensible default when neither has enough data yet.
        """
        heating_cycle_coordinator = self.coordinator.heating_cycle_coordinator
        if heating_cycle_coordinator:
            zone_data = heating_cycle_coordinator.get_zone_data(self._zone_id)
            if zone_data and zone_data.get("heating_rate") is not None:
                rate = zone_data.get("heating_rate")
                if rate > _MIN_BOOST_HEATING_RATE:  # type: ignore[operator]
                    _LOGGER.debug(
                        "Button: smart boost using heating-cycle "
                        "coordinator rate %.2f°C/h",
                        rate,
                    )
                    return rate  # type: ignore[return-value]

        smart_comfort_manager = self.coordinator.smart_comfort_manager
        if smart_comfort_manager:
            rate = smart_comfort_manager.get_heating_rate(self._zone_id)
            if rate is not None and rate > _MIN_BOOST_HEATING_RATE:
                _LOGGER.debug(
                    "Button: smart boost using Smart Comfort rate "
                    "%.2f°C/h",
                    rate,
                )
                return rate

        _LOGGER.debug(
            "Button: smart boost using default rate %s°C/h "
            ", measured rate not available yet",
            SMART_BOOST_DEFAULT_RATE,
        )
        return SMART_BOOST_DEFAULT_RATE

    async def async_press(self) -> None:
        """Apply a smart boost: target temp from schedule, duration from heating rate."""
        await _check_bootstrap_reserve_or_raise(self.hass, f"Smart Boost {self._zone_name}", coordinator=self.coordinator)

        _LOGGER.debug("Button: Smart Boost pressed for %s", self._zone_name)

        coord_data = self.coordinator.data or {}
        zone_states = get_zone_states(coord_data)
        zone_data = zone_states.get(self._zone_id) or zone_states.get(str(self._zone_id))

        if not zone_data:
            _LOGGER.warning(
                "Button: smart boost for %s skipped, no zone data "
                "yet, will work after the next coordinator poll",
                self._zone_name,
            )
            return

        zone_data = merge_homekit_into_zone_data(zone_data, self._zone_id, self.coordinator)
        sensor_data = zone_data.get("sensorDataPoints") or {}
        current_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
        if current_temp is None:
            _LOGGER.warning(
                "Button: smart boost for %s skipped, current "
                "temperature not available, will work after the next "
                "sensor reading",
                self._zone_name,
            )
            return

        setting = zone_data.get("setting") or {}
        setting_temp = (setting.get("temperature") or {}).get("celsius")
        target_temp = setting_temp
        if target_temp is None or target_temp <= current_temp:
            # No usable schedule target: pick a sensible default
            # so the button still does something useful.
            target_temp = min(current_temp + 3.0, 25.0)
            _LOGGER.debug(
                "Button: smart boost defaulting target to %s°C "
                "(current + 3°C), schedule target unavailable or "
                "already reached",
                target_temp,
            )

        heating_rate = self._get_heating_rate()

        temp_diff = target_temp - current_temp
        if temp_diff <= 0:
            _LOGGER.info(
                "Button: smart boost not needed: %s already at "
                "%s°C (target %s°C)",
                self._zone_name, current_temp, target_temp,
            )
            return

        duration_hours = temp_diff / heating_rate
        duration_minutes = int(duration_hours * 60)

        duration_minutes = max(SMART_BOOST_MIN_DURATION, min(duration_minutes, SMART_BOOST_MAX_DURATION))

        _LOGGER.debug(
            "Button: smart boost calculation: %s°C → %s°C at "
            "%s°C/h ⇒ %s minutes",
            current_temp,
            target_temp,
            heating_rate,
            duration_minutes,
        )

        client = self.coordinator.api_client

        setting = {
            "type": "HEATING",
            "power": "ON",
            "temperature": {"celsius": target_temp},
        }
        termination = build_timer_termination(duration_minutes=duration_minutes)

        api_success = await run_service_call(
            hass=self.hass,
            coordinator=self.coordinator,
            zone_id=self._zone_id,
            entity_type="climate_heating",
            api_coro=client.set_zone_overlay(self._zone_id, setting, termination),
            capture_source="smart_boost",
            refresh_entity_id=self.entity_id,
            reason="smart_boost",
        )

        if api_success:
            _LOGGER.info(
                "Button: smart boosted %s to %s°C for %s minutes "
                "(rate %s°C/h)",
                self._zone_name,
                target_temp,
                duration_minutes,
                heating_rate,
            )
        else:
            _LOGGER.warning(
                "Button: smart boost for %s could not be activated "
                ", schedule remains unchanged",
                self._zone_name,
            )


class TadoIdentifyButton(CoordinatorEntity[TadoDataUpdateCoordinator], ButtonEntity):
    """Flash a single device's LED so you can tell which physical TRV / thermostat it is.

    One button per physical device. Multi-TRV zones get one button per device,
    all on the same zone card, so each radiator can be flashed individually. The
    button holds the device's full serial, so you never copy it by hand.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        zone_type: str,
        device: dict[str, Any],
        zones_info: list[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the TadoIdentifyButton for one physical device."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._device_serial = device.get("shortSerialNo", "unknown")
        self._device_type = device.get("deviceType", "unknown")

        _meta = ENTITY_REGISTRY["button_identify"]
        self._attr_unique_id = (
            f"tado_ce_{coordinator.home_id}"
            f"_{_meta.unique_id_suffix.format(serial=self._device_serial)}"
        )
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, zone_type, coordinator.home_id)

        # Pin entity_id so HA 2026.6 doesn't double the slug on same-named
        # zone/area. Suffix disambiguates multiple devices in one zone.
        suffix = get_device_name_suffix(zone_id, self._device_serial, self._device_type, zones_info or [])
        self._attr_name = f"Identify{suffix}"
        self.entity_id = f"button.{slugify(f'{zone_name} identify{suffix}')}"

    async def async_press(self) -> None:
        """Flash this device's LED, locally over HomeKit when possible.

        The Tado bridge carries the HomeKit Identify characteristic, so a local
        write costs no cloud API call. Falls back to the cloud identify service
        when HomeKit isn't connected or the local write fails.
        """
        provider = self.coordinator.homekit_provider
        if provider is not None and provider.is_connected:
            if await provider.async_identify(self._device_serial):
                _LOGGER.debug(
                    "Button: identify flashed device %s over HomeKit (no cloud call)",
                    mask_serial(self._device_serial),
                )
                return
            _LOGGER.debug(
                "Button: local identify for %s didn't land, falling back to the cloud call",
                mask_serial(self._device_serial),
            )

        await _check_bootstrap_reserve_or_raise(
            self.hass, f"Identify {self._zone_name}", coordinator=self.coordinator,
        )

        success = await self.coordinator.api_client.identify_device(self._device_serial)
        if success:
            _LOGGER.debug("Button: identify sent to device %s", mask_serial(self._device_serial))
        else:
            _LOGGER.warning(
                "Button: identify for device %s could not be sent, the cloud did "
                "not acknowledge the request, please retry",
                mask_serial(self._device_serial),
            )
