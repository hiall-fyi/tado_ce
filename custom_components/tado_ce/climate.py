"""Tado CE climate platform: instantiate one entity per heating or AC zone.

`TadoClimate` covers heating zones (TRVs, smart thermostats);
`TadoACClimate` covers air-conditioning zones, which need extra
capability metadata (supported modes, fan / swing options) sourced
from the cloud `/capabilities` endpoint.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .climate_ac import TadoACClimate
from .climate_heating import TadoClimate
from .helpers import get_zone_states

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TadoConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE climate from a config entry."""
    coordinator = entry.runtime_data
    home_id = coordinator.home_id
    # Build zone data from coordinator
    zones_info = coordinator.data.get("zones_info") or []
    zone_names = {str(z.get("id")): z.get("name", f"Zone {z.get('id')}") for z in zones_info}
    zone_types = {str(z.get("id")): z.get("type") or "" for z in zones_info}

    # Build zone capabilities (AC zones need detailed caps)
    ac_caps = coordinator.data.get("ac_capabilities") or {}
    zone_caps: dict[str, dict[str, Any]] = {}
    for z in zones_info:
        zid = str(z.get("id"))
        ztype = z.get("type")
        if ztype == "AIR_CONDITIONING" and zid in ac_caps:
            zone_caps[zid] = {"type": ztype, "ac_capabilities": ac_caps[zid]}
        else:
            zone_caps[zid] = {"type": ztype, "capabilities": z.get("capabilities") or {}}

    climates = []
    try:
        zone_states = get_zone_states(coordinator.data)
        for zone_id in zone_states:
            zone_type = zone_types.get(zone_id, "")
            zone_name = zone_names.get(zone_id, f"Zone {zone_id}")
            caps = zone_caps.get(zone_id, {})

            if zone_type == "HEATING":
                climates.append(TadoClimate(coordinator, zone_id, zone_name, home_id))
            elif zone_type == "AIR_CONDITIONING":
                climates.append(TadoACClimate(coordinator, zone_id, zone_name, caps, home_id))  # type: ignore[arg-type]
    except (KeyError, TypeError, AttributeError):
        _LOGGER.warning(
            "Climate: could not parse zone list from coordinator data, "
            "no climate entities created this cycle, will retry on next "
            "poll",
            exc_info=True,
        )

    async_add_entities(climates, True)
    _LOGGER.info("Climate: created %d climate entity(ies)", len(climates))
