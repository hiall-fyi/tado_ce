"""Tado CE number platform: boiler max output temperature.

Single optional entity, only created when bridge credentials are
configured AND the bridge response actually carries the
`boilerMaxOutputTemperatureInCelsius` field. OpenTherm-only:
on/off boilers don't expose a max-output setting.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bridge_api import FLOW_TEMP_STEP, MAX_FLOW_TEMP, MIN_FLOW_TEMP
from .const import DOMAIN
from .device_manager import get_hub_device_info
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .exceptions import TadoBridgeApiError
from .helpers import PerEntityAvailabilityMixin

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE number entities from a config entry."""
    coordinator: TadoDataUpdateCoordinator = entry.runtime_data
    entities = []

    # Bridge number entity (optional: only when bridge credentials configured
    # AND enabled AND the bridge response actually contains the temperature field)
    bridge_serial = entry.options.get("bridge_serial")
    bridge_auth_key = entry.options.get("bridge_auth_key")
    if bridge_serial and bridge_auth_key and coordinator.config_manager.get_bridge_enabled():
        bridge_data = coordinator.data.get("bridge")
        if isinstance(bridge_data, dict) and "boilerMaxOutputTemperatureInCelsius" in bridge_data:
            entities.append(TadoBoilerMaxOutputTemperatureNumber(coordinator))
            _LOGGER.debug(
                "Number: bridge exposes boiler max output temperature "
                ", creating boiler max temp number entity",
            )
        else:
            _LOGGER.debug(
                "Number: bridge configured but boiler max output "
                "temperature not in the response, boiler max temp "
                "number entity not created (requires an OpenTherm "
                "boiler)",
            )

    async_add_entities(entities, True)


class TadoBoilerMaxOutputTemperatureNumber(
    PerEntityAvailabilityMixin,
    CoordinatorEntity["TadoDataUpdateCoordinator"],
    NumberEntity,
):
    """Set the boiler's maximum output temperature on OpenTherm bridges.

    Optimistic on success: the entity reflects the requested
    value immediately and the next coordinator poll either
    confirms it or corrects it from the bridge.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = MIN_FLOW_TEMP
    _attr_native_max_value = MAX_FLOW_TEMP
    _attr_native_step = FLOW_TEMP_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_icon = "mdi:thermometer-water"

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        _meta = ENTITY_REGISTRY["number_boiler_max_output_temp"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix}"
        if _meta.icon:
            self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_device_info = get_hub_device_info(coordinator.home_id)
        self._data_present = False
        self._attr_native_value: float | None = None

    async def async_set_native_value(self, value: float) -> None:
        """Send the new boiler max output temperature to the bridge."""
        client = self.coordinator.bridge_api_client
        if client is None:
            msg = "Bridge API client not available"
            raise HomeAssistantError(
                msg,
                translation_domain=DOMAIN,
                translation_key="bridge_client_not_available",
            )
        try:
            await client.async_set_max_output_temperature(value)
        except TadoBridgeApiError as err:
            _LOGGER.warning(
                "Number: boiler max output temperature write failed "
                "(%s), keeping the previous value",
                err,
            )
            msg = "Failed to set boiler max output temperature"
            raise HomeAssistantError(
                msg,
                translation_domain=DOMAIN,
                translation_key="boiler_max_output_temp_failed",
            ) from err
        # Quantise to the bridge's 0.5°C step before reflecting back.
        # The bridge would round anyway, so showing the user's raw
        # request would briefly disagree with the next poll.
        self._attr_native_value = round(value * 2) / 2
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh boiler max output temperature from the latest bridge poll."""
        bridge = self.coordinator.data.get("bridge")
        if not bridge:
            self._data_present = False
            self.async_write_ha_state()
            return
        temp = bridge.get("boilerMaxOutputTemperatureInCelsius")
        if temp is not None:
            self._attr_native_value = float(temp)
            self._data_present = True
        else:
            self._data_present = False
        self.async_write_ha_state()
