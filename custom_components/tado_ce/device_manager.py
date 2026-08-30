"""Tado CE device-info builders: hub / zone DeviceInfo + multi-device name suffixing (identifiers are home-id scoped)."""

from __future__ import annotations

from functools import lru_cache
from inspect import signature
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.helpers.entity import DeviceInfo  # type: ignore[attr-defined]

from .const import DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def load_version() -> str:
    """Read the integration version from `manifest.json` (cached); blocking file I/O, call via executor."""
    try:
        manifest_path = Path(__file__).parent / "manifest.json"
        with manifest_path.open() as f:
            manifest = json.load(f)
            return manifest.get("version", "unknown")  # type: ignore[no-any-return]
    except (OSError, ValueError) as e:
        _LOGGER.warning(
            "Device Manager: could not read version from manifest "
            "(%s), falling back to 'unknown'",
            e,
        )
        return "unknown"


def _get_cached_version() -> str:
    """Read the cached version without forcing a manifest load; returns 'unknown' when `load_version()` hasn't run."""
    if load_version.cache_info().currsize == 0:
        return "unknown"
    return load_version()


_hub_device_ids: dict[str, str] = {}


def _hub_identifier(home_id: str) -> str:
    """Single source of truth for the hub's stable identifier string."""
    return f"tado_ce_hub_{home_id}" if home_id != "unknown" else "tado_ce_hub"


def set_cached_hub_device_id(home_id: str, device_id: str) -> None:
    """Record the hub device's registry id for this home, set once per setup by register_hub_device."""
    _hub_device_ids[home_id] = device_id


def clear_cached_hub_device_id(home_id: str) -> None:
    """Drop the cached id on entry unload: a stale id is worse than a miss, not equivalent to one."""
    _hub_device_ids.pop(home_id, None)


def _get_cached_hub_device_id(home_id: str) -> str | None:
    return _hub_device_ids.get(home_id)


@lru_cache(maxsize=1)
def _hub_parenting_kwarg() -> str:
    """Return 'via_device_id' on HA 2026.8+, else the legacy 'via_device' (still accepted below it)."""
    from homeassistant.helpers import device_registry as dr

    params = signature(dr.DeviceRegistry.async_get_or_create).parameters
    return "via_device_id" if "via_device_id" in params else "via_device"


def hub_parent_kwargs(home_id: str) -> DeviceInfo:
    """Return the DeviceInfo parenting kwarg(s) for the hub, on whichever HA the install supports."""
    kwarg = _hub_parenting_kwarg()
    parent: DeviceInfo = {}
    if kwarg == "via_device_id":
        hub_device_id = _get_cached_hub_device_id(home_id)
        if hub_device_id:
            parent = {"via_device_id": hub_device_id}
    else:
        # Will fail mypy --strict (typeddict-unknown-key) once installed HA hits 2026.9 and
        # drops `via_device` from DeviceInfo; expected, keep the branch for the 2025.11 floor.
        parent = {"via_device": (DOMAIN, _hub_identifier(home_id))}
    return parent


def get_hub_device_info(home_id: str) -> DeviceInfo:
    """Build the home-scoped Tado CE hub `DeviceInfo` (parents the global entities)."""
    identifier = _hub_identifier(home_id)

    return DeviceInfo(
        configuration_url="https://app.tado.com",
        identifiers={(DOMAIN, identifier)},
        name="Tado CE Hub",
        manufacturer=MANUFACTURER,
        model="Tado CE Integration",
        sw_version=_get_cached_version(),
    )


def get_zone_device_info(zone_id: str, zone_name: str, zone_type: str, home_id: str) -> DeviceInfo:
    """Build a zone `DeviceInfo` parented to the home's hub."""
    model = get_zone_type_display(zone_type)
    zone_identifier = (
        f"tado_ce_{home_id}_zone_{zone_id}" if home_id != "unknown" else f"tado_ce_zone_{zone_id}"
    )

    parent = hub_parent_kwargs(home_id)

    return DeviceInfo(
        configuration_url=f"https://app.tado.com/en/main/home/zoneV2/{zone_id}",
        identifiers={(DOMAIN, zone_identifier)},
        name=zone_name,
        manufacturer=MANUFACTURER,
        model=model,
        suggested_area=zone_name,
        **parent,
    )


def get_zone_type_display(zone_type: str) -> str:
    """Map Tado zone-type codes to the human display name shown in the device model field."""
    zone_type_map = {
        "HEATING": "Heating Zone",
        "AIR_CONDITIONING": "AC Zone",
        "HOT_WATER": "Hot Water Zone",
    }
    return zone_type_map.get(zone_type, "Unknown Zone")


def get_device_name_suffix(zone_id: str, device_serial: str, device_type: str, zones_info: list[Any]) -> str:
    """Compute a name suffix for multi-device zones: `""` / ` VA02` / ` VA02 (n)` so entities disambiguate."""
    zone = next((z for z in zones_info if str(z.get("id")) == str(zone_id)), None)
    if not zone:
        return ""

    devices = zone.get("devices") or []
    if len(devices) <= 1:
        return ""

    same_type_devices = [d for d in devices if d.get("deviceType") == device_type]

    if len(same_type_devices) > 1:
        try:
            index = next(i + 1 for i, d in enumerate(same_type_devices) if d.get("shortSerialNo") == device_serial)
            return f" {device_type} ({index})"
        except StopIteration:
            return f" {device_type}"
    else:
        return f" {device_type}"
