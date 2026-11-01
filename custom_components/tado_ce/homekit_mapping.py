"""Tado CE HomeKit mapping: link HomeKit accessories to cloud zone IDs by serial."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .helpers import mask_home_id, mask_serial
from .homekit_client import CHAR_MODEL, CHAR_SERIAL_NUMBER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .homekit_client import HomeKitClient

_LOGGER = logging.getLogger(__name__)

# Bridge model identifier: skip in mapping (not a zone device)
_BRIDGE_MODEL: str = "IB01"

_STORE_VERSION = 1


def device_serials(device: Any) -> set[str]:
    """Return the non-empty string serialNo and shortSerialNo of a cloud device."""
    if not isinstance(device, dict):
        return set()
    return {
        value
        for value in (device.get("serialNo"), device.get("shortSerialNo"))
        if isinstance(value, str) and value
    }


def build_serial_mapping(
    accessories: list[dict[str, Any]],
    cloud_zones_info: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the serial-to-zone and zone-to-aids mapping from HomeKit + cloud data."""
    empty_result: dict[str, Any] = {
        "serial_to_zone": {},
        "zone_to_aids": {},
        "last_updated": dt_util.utcnow().isoformat(),
    }

    if not accessories:
        _LOGGER.warning(
            "HomeKit: no accessories returned by the bridge, cannot "
            "build the serial-to-zone mapping, local control will stay "
            "off until the next pairing fetch",
        )
        return empty_result

    if not cloud_zones_info:
        _LOGGER.warning(
            "HomeKit: no cloud zone data available, cannot build the "
            "serial-to-zone mapping, will retry after the next cloud sync",
        )
        return empty_result

    # Skip zones with no usable id (Tado IDs start from 1).
    zone_serials: dict[str, set[str]] = {}
    for zone in cloud_zones_info:
        raw_id = zone.get("id")
        if not raw_id:
            _LOGGER.debug(
                "HomeKit: skipping zone with no usable id (%r)", raw_id,
            )
            continue
        zone_id = str(raw_id)
        devices = zone.get("devices") or []
        zone_serials[zone_id] = set().union(*(device_serials(d) for d in devices))

    _LOGGER.debug(
        "HomeKit: cloud zone serial counts: %s",
        {zid: len(serials) for zid, serials in zone_serials.items()},
    )

    serial_to_zone: dict[str, str] = {}
    zone_to_aids: dict[str, list[int]] = {}

    for acc in accessories:
        aid: int | None = acc.get("aid")
        serial = model = None
        for svc in acc.get("services", []):
            for char in svc.get("characteristics", []):
                # aiohomekit normalises type UUIDs to the full UUID
                # form, so strip the suffix and compare as ints so
                # leading zeros round-trip cleanly.
                raw_type = char.get("type", "")
                if "-" in raw_type:
                    ctype = raw_type.split("-")[0].upper()
                else:
                    ctype = raw_type.upper()
                try:
                    ctype_int = int(ctype, 16)
                except (ValueError, TypeError):
                    continue
                if ctype_int == int(CHAR_SERIAL_NUMBER, 16):
                    serial = char.get("value")
                elif ctype_int == int(CHAR_MODEL, 16):
                    model = char.get("value")

        if model == _BRIDGE_MODEL:
            _LOGGER.debug(
                "HomeKit: skipping IB01 bridge accessory (not a zone device)",
            )
            continue

        if not serial or aid is None:
            _LOGGER.debug(
                "HomeKit: accessory aid=%s has no serial, skipping",
                aid,
            )
            continue

        matched = False
        for zone_id, serials in zone_serials.items():
            if serial in serials:
                serial_to_zone[serial] = zone_id
                zone_to_aids.setdefault(zone_id, []).append(aid)
                matched = True
                break

        if not matched:
            _LOGGER.warning(
                "HomeKit: accessory serial %s does not match any cloud "
                "zone, local control unavailable for this device",
                mask_serial(serial),
            )

    _LOGGER.info(
        "HomeKit: mapped %d accessory(ies) to %d zone(s)",
        len(serial_to_zone),
        len(zone_to_aids),
    )

    return {
        "serial_to_zone": serial_to_zone,
        "zone_to_aids": zone_to_aids,
        "last_updated": dt_util.utcnow().isoformat(),
    }


def validate_mapping(
    mapping: dict[str, Any],
    valid_zone_ids: set[str] | None = None,
) -> bool:
    """Return True when the cached mapping looks healthy, False to force a rebuild.

    Drops mappings containing zone "0" (a known corruption shape from
    earlier builds) and any zone IDs not present in the current cloud
    zone set.
    """
    serial_to_zone = mapping.get("serial_to_zone", {})
    zone_to_aids = mapping.get("zone_to_aids", {})

    if "0" in zone_to_aids or "0" in serial_to_zone.values():
        _LOGGER.info(
            "HomeKit: cached mapping contains invalid zone '0'. "
            "rebuilding from current bridge + cloud data",
        )
        return False

    if valid_zone_ids is not None:
        mapped_zone_ids = set(serial_to_zone.values())
        invalid_zones = mapped_zone_ids - valid_zone_ids
        if invalid_zones:
            _LOGGER.warning(
                "HomeKit: cached mapping references zone(s) %s that no "
                "longer exist in the cloud zone list, rebuilding from "
                "current data",
                invalid_zones,
            )
            return False

    return True


def mapping_covers_all_zones(
    zone_aid_map: dict[str, list[int]],
    zones_info: list[dict[str, Any]],
) -> bool:
    """Return True when every climate zone that owns a device serial is mapped.

    A home with no serial-bearing climate zones is trivially complete.
    """
    from .const import get_climate_zone_ids

    climate_ids = get_climate_zone_ids(zones_info)
    mappable = {
        str(z.get("id"))
        for z in zones_info
        if str(z.get("id")) in climate_ids
        and any(device_serials(d) for d in (z.get("devices") or []))
    }
    return mappable.issubset(set(zone_aid_map))


async def load_device_mapping(
    hass: HomeAssistant,
    home_id: str,
) -> dict[str, Any] | None:
    """Load the cached mapping from HA Store, falling back to legacy JSON migration."""
    store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"tado_ce/homekit_device_map_{home_id}")
    data = await store.async_load()
    if data and isinstance(data, dict):
        return data

    from .const import get_data_file
    from .storage import async_migrate_json_to_store

    old_path = get_data_file("homekit_device_map", home_id)
    migrated = await async_migrate_json_to_store(hass, old_path, store, label="homekit_device_map")
    if migrated and isinstance(migrated, dict):
        return migrated
    return None


async def save_device_mapping(
    hass: HomeAssistant,
    home_id: str,
    mapping: dict[str, Any],
) -> None:
    """Persist the mapping to HA Store."""
    store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"tado_ce/homekit_device_map_{home_id}")
    await store.async_save(mapping)
    _LOGGER.debug(
        "HomeKit: saved device mapping for home %s", mask_home_id(home_id),
    )


async def async_rebuild_and_save_mapping(
    hass: HomeAssistant,
    homekit_client: HomeKitClient,
    home_id: str,
    zones_info: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild the HomeKit serial-to-zone mapping from the live bridge + cloud zones.

    Lists the bridge's accessories, matches their serials against the cloud
    zones_info, and on a non-empty result saves the mapping to the Store and
    installs it on the client. Returns the mapping; serial_to_zone is empty when
    the bridge returned no accessories, zones_info was empty, or nothing matched;
    the caller decides whether to retry.
    """
    empty: dict[str, Any] = {"serial_to_zone": {}, "zone_to_aids": {}}
    accessories = await homekit_client.async_list_accessories()
    if not accessories or not zones_info:
        return empty

    mapping = build_serial_mapping(accessories, zones_info)
    if mapping.get("serial_to_zone"):
        await save_device_mapping(hass, home_id, mapping)
        homekit_client.set_zone_mapping(
            mapping.get("serial_to_zone", {}),
            mapping.get("zone_to_aids", {}),
        )
    return mapping


async def remove_device_mapping(
    hass: HomeAssistant,
    home_id: str,
) -> None:
    """Remove the cached mapping from HA Store (e.g. when unpairing the bridge)."""
    store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"tado_ce/homekit_device_map_{home_id}")
    await store.async_remove()
    _LOGGER.debug(
        "HomeKit: removed device mapping for home %s", mask_home_id(home_id),
    )
