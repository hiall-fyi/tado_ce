"""Tado CE device trackers: Tado mobile-app presence per registered phone.

One tracker per mobile device that has Tado's geo-tracking
enabled in the app. Source-of-truth is the cloud `mobile_devices`
endpoint; the tracker reflects `atHome` plus the relative
distance / bearing for users who want to surface the gradient
rather than just home / not-home.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import (  # type: ignore[attr-defined]
    SourceType,
    TrackerEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .device_manager import get_hub_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .helpers import PerEntityAvailabilityMixin, mask_serial

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE device trackers from a config entry."""
    coordinator = entry.runtime_data
    data_loader = coordinator.data_loader
    home_id = coordinator.home_id
    _LOGGER.debug("Device Tracker: setup starting")
    mobile_devices = await hass.async_add_executor_job(data_loader.load_mobile_devices_file)

    trackers = []

    if mobile_devices:
        for device in mobile_devices:
            device_id = device.get("id")
            device_name = device.get("name", f"Device {device_id}")
            settings = device.get("settings") or {}

            # Only create tracker if geo tracking is enabled
            if settings.get("geoTrackingEnabled", False):
                trackers.append(TadoDeviceTracker(coordinator, device_id, device_name, device, home_id))
            else:
                _LOGGER.debug(
                    "Device Tracker: %s skipped, geo-tracking disabled "
                    "in the Tado app for this device",
                    device_name,
                )

    if trackers:
        async_add_entities(trackers, True)
        _LOGGER.info(
            "Device Tracker: created %d tracker entity(ies)",
            len(trackers),
        )
    else:
        _LOGGER.debug(
            "Device Tracker: no mobile devices have geo-tracking "
            "enabled, no tracker entities created",
        )


class TadoDeviceTracker(PerEntityAvailabilityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], TrackerEntity):
    """Represent a Tado mobile device tracker entity.

    Tado's cloud reports only a binary `atHome` for each phone (plus a
    bearing and relative distance from home), never absolute coordinates.
    Presence is published through `_attr_in_zones` (["zone.home"] when home,
    [] when away, None when the phone hasn't reported), which HA derives into
    home / not_home / unknown. `source_type` is `router` (a presence flag, not
    a GPS fix), and the tracker groups under the Tado CE hub device.
    """

    _attr_has_entity_name = True
    _attr_source_type = SourceType.ROUTER

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        device_id: int,
        device_name: str,
        device_data: dict[str, Any],
        home_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._device_name = device_name
        self._device_data = device_data

        _meta = ENTITY_REGISTRY["device_tracker_mobile"]
        self._attr_name = device_name
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(device_id=device_id)}"
        entity_category = get_entity_category(_meta)
        if entity_category is not None:
            self._attr_entity_category = entity_category
        self._data_present = False
        # Use hub device info for global entities
        self._attr_device_info = get_hub_device_info(home_id)  # type: ignore[assignment]

        self._bearing: float | None = None
        self._relative_distance: float | None = None
        # Surfaces why presence is unknown: geo-tracking is on but the device
        # reported no location block. On Android this is usually the Tado app
        # being denied background location by the OS (battery optimisation, Doze).
        self._location_status: str | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        metadata = self._device_data.get("deviceMetadata") or {}
        return {
            "device_id": mask_serial(str(self._device_id)),
            "platform": metadata.get("platform"),
            "os_version": metadata.get("osVersion"),
            "model": metadata.get("model"),
            "bearing": self._bearing,
            "relative_distance": self._relative_distance,
            "location_status": self._location_status,
        }

    @callback
    def _update_from_coordinator(self) -> None:
        """Refresh presence + bearing + relative distance from the latest poll."""
        try:
            devices = (self.coordinator.data or {}).get("mobile_devices")

            if devices:
                for device in devices:
                    if device.get("id") == self._device_id:
                        self._device_data = device
                        location = device.get("location")

                        if location:
                            at_home = location.get("atHome")
                            if at_home is True:
                                self._attr_in_zones = ["zone.home"]
                            elif at_home is False:
                                self._attr_in_zones = []
                            else:
                                self._attr_in_zones = None
                            self._bearing = (location.get("bearingFromHome") or {}).get("degrees")
                            self._relative_distance = location.get("relativeDistanceFromHomeFence")
                            self._location_status = "reporting"
                        else:
                            # No location block usually means the device's
                            # background location is currently denied,
                            # most commonly Android battery optimisation
                            # killing the Tado app's location service, or
                            # the user denying "always" permission.
                            self._attr_in_zones = None
                            self._location_status = "no_location_reported"

                        self._data_present = True
                        return

            # Device id no longer present in the account: unavailable, and drop
            # the last-known presence so it does not report a stale zone.
            self._attr_in_zones = None
            self._location_status = None
            self._data_present = False
        except (KeyError, TypeError, AttributeError) as err:
            _LOGGER.debug(
                "Device Tracker: %s update failed (%s), marking "
                "unavailable until the next poll",
                self._device_name, err,
            )
            self._attr_in_zones = None
            self._location_status = None
            self._data_present = False
