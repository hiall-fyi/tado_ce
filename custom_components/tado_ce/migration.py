"""Tado CE config-entry version migration + duplicate cleanup + entity-platform moves.

v5.0.0's minimum supported upgrade path is config-entry
version 13 (v4.1.0 GA onwards): older entries refuse to load
and tell the user to run any v4.1.0+ release once first.

Minor version 2 forward-writes `thermal_analytics_enabled=True` and
`heating_cycle_history_days=30` so existing installs keep their old
effective values after the getters' defaults were corrected.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from .const import DOMAIN
from .repair_helpers import (
    UNSUPPORTED_UPGRADE_PATH_ISSUE,
    async_create_issue,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Migration version constants
_MIN_SUPPORTED_VERSION = 13  # v4.1.0 GA; below this, the v3-era data format we no longer read

# Module-level set to track duplicate cleanup operations (prevents re-running)
_duplicate_cleanup_done: set[str] = set()


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Step a config entry forward to the current schema version.

    Version 13 is both the floor and the current major schema, so the only
    live step is the minor-version forward-write below. A `None` version
    means a corrupted entry of unknown vintage; we refuse to load it because
    snapping forward would wrongly assert it is current (Home Assistant
    declares the field as `int` and never sends `None`).
    """
    initial_version = config_entry.version

    if initial_version is None:
        _LOGGER.error(
            "Migration: config entry has no schema version, which means it "
            "is corrupted or was written by an unknown version. Refusing to "
            "load it rather than assuming it is current. Remove and re-add "
            "the integration, or restore a backup.",
        )
        return False

    if initial_version < _MIN_SUPPORTED_VERSION:
        _LOGGER.error(
            "Migration: config entry version %s is below the v5.0.0 minimum "
            "(%s, which is v4.1.0). Install any Tado CE v4.1.0 or later "
            "release, start Home Assistant once so it updates the stored "
            "data, then upgrade again. Nothing has been changed, so "
            "returning to that release is safe.",
            initial_version,
            _MIN_SUPPORTED_VERSION,
        )
        async_create_issue(
            hass,
            UNSUPPORTED_UPGRADE_PATH_ISSUE,
            home_id=config_entry.data.get("home_id"),
        )
        return False

    if config_entry.minor_version < 2:
        options = dict(config_entry.options)
        options.setdefault("thermal_analytics_enabled", True)
        options.setdefault("heating_cycle_history_days", 30)
        hass.config_entries.async_update_entry(
            config_entry, options=options, minor_version=2,
        )
        _LOGGER.debug("Migration: config entry moved to minor version 2")

    _LOGGER.debug(
        "Migration: config entry already at version %s.%s, no further "
        "migration needed",
        config_entry.version,
        config_entry.minor_version,
    )
    return True


# Stale suffix -> converged suffix, for installs created before the bridge
# naming settled on one shape. Keyed on the OLD value, so an entry already at
# target is simply absent and skips. Nine of the twelve enrichment fields moved;
# the other three were already there via the override map this replaced.
_BRIDGE_SUFFIX_RENAMES: dict[str, str] = {
    "bridge_boiler_outputTemperature_timestamp": "boiler_output_temperature_time",
    "bridge_boiler_flowTemperature_celsius": "boiler_flow_temperature",
    "bridge_boiler_flowTemperature_timestamp": "boiler_flow_temperature_time",
    "bridge_bridgeConnected": "boiler_bridge_connected",
    "bridge_hotWaterZonePresent": "boiler_hot_water_present",
    "bridge_deviceWiredToBoiler_type": "boiler_device_type",
    "bridge_deviceWiredToBoiler_serialNo": "boiler_device_serial",
    "bridge_deviceWiredToBoiler_thermInterfaceType": "boiler_interface_type",
    "bridge_deviceWiredToBoiler_connected": "boiler_device_connected",
}


# Renames that are not part of the bridge naming convergence but share the same
# one-shot registry walk. The refresh button was renamed when the capability
# cache stopped being AC-only.
_OTHER_SUFFIX_RENAMES: dict[str, str] = {
    "refresh_ac": "refresh_zone_capabilities",
}


def build_bridge_rekey_callback(
    hass: HomeAssistant,
    home_id: str,
) -> Callable[[Any], dict[str, str] | None]:
    """Build the entity-registry callback for this integration's unique_id renames.

    Returns None for anything needing no write, which covers a fresh install, a
    second run, another home's entities, a suffix runtime inference minted
    rather than the enrichment table, and a target id already in use.
    """
    from homeassistant.helpers import entity_registry as er

    prefix = f"tado_ce_{home_id}_"
    renames = {**_BRIDGE_SUFFIX_RENAMES, **_OTHER_SUFFIX_RENAMES}

    def _rekey(entry: Any) -> dict[str, str] | None:
        if not entry.unique_id.startswith(prefix):
            return None
        new_suffix = renames.get(entry.unique_id[len(prefix):])
        if new_suffix is None:
            return None

        new_unique_id = f"{prefix}{new_suffix}"
        registry = er.async_get(hass)
        if registry.async_get_entity_id(entry.domain, entry.platform, new_unique_id):
            # Home Assistant raises ValueError on a duplicate unique_id, and the
            # migration walks entries with no transaction, so raising here would
            # leave the registry half-renamed and abort setup again on every
            # later load. Skip instead and let the user remove the stale entity.
            _LOGGER.warning(
                "Migration: cannot re-key %s to %s, an entity already uses the "
                "new id, leaving it in place",
                entry.unique_id,
                new_unique_id,
            )
            return None

        _LOGGER.info(
            "Migration: re-keying %s from %s to %s",
            entry.entity_id,
            entry.unique_id,
            new_unique_id,
        )
        return {"new_unique_id": new_unique_id}

    return _rekey


async def async_deduplicate_entries(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Drop duplicate entries that share a unique_id, keeping the highest version.

    Returns True when the *current* entry is the keeper (or is
    the only entry for its unique_id) so setup can continue;
    False when this entry is the duplicate and should abort.
    Multi-home users with different unique_ids per home keep
    all their entries.
    """
    from collections import defaultdict

    all_entries = hass.config_entries.async_entries(DOMAIN)

    entries_by_uid: dict[str | None, list[Any]] = defaultdict(list)
    for e in all_entries:
        entries_by_uid[e.unique_id].append(e)

    my_group = entries_by_uid.get(entry.unique_id, [entry])

    if len(my_group) > 1:
        _LOGGER.warning(
            "Migration: %d config entries share unique_id %r, "
            "running duplicate cleanup",
            len(my_group),
            entry.unique_id,
        )
        _LOGGER.debug(
            "Migration: entries in duplicate group: %s",
            [(e.entry_id, e.version) for e in my_group],
        )

        # Highest version wins; ties resolved by entry_id for
        # deterministic behaviour across HA restarts.
        entries_by_version = sorted(
            my_group,
            key=lambda e: (getattr(e, "version", 0), e.entry_id),
            reverse=True,
        )

        keeper_entry_id = entries_by_version[0].entry_id
        _LOGGER.info(
            "Migration: keeping config entry %s as the canonical "
            "entry for unique_id %r",
            keeper_entry_id, entry.unique_id,
        )

        if entry.entry_id != keeper_entry_id:
            _LOGGER.warning(
                "Migration: aborting setup of entry %s (version %s) "
                ", it is a duplicate of unique_id %r and will be "
                "removed by the keeper entry",
                entry.entry_id,
                entry.version,
                entry.unique_id,
            )
            return False

        cleanup_key = f"duplicate_cleanup_{keeper_entry_id}"
        if cleanup_key not in _duplicate_cleanup_done:
            all_removed = True

            _LOGGER.info(
                "Migration: keeper entry %s removing %d duplicate "
                "entries for unique_id %r",
                keeper_entry_id,
                len(entries_by_version) - 1,
                entry.unique_id,
            )

            for old_entry in entries_by_version[1:]:
                _LOGGER.warning(
                    "Migration: removing duplicate entry %s "
                    "(version %s, unique_id %r)",
                    old_entry.entry_id,
                    getattr(old_entry, "version", "unknown"),
                    old_entry.unique_id,
                )
                try:
                    await hass.config_entries.async_remove(old_entry.entry_id)
                    _LOGGER.info(
                        "Migration: removed duplicate entry %s",
                        old_entry.entry_id,
                    )
                except Exception:
                    # Broad on purpose: any removal failure must leave the key out so the next setup retries.
                    all_removed = False
                    _LOGGER.warning(
                        "Migration: could not remove duplicate "
                        "entry %s, leaving it in place, will retry "
                        "on the next reload",
                        old_entry.entry_id,
                        exc_info=True,
                    )

            if all_removed:
                _duplicate_cleanup_done.add(cleanup_key)
                _LOGGER.info(
                    "Migration: duplicate cleanup complete for "
                    "unique_id %r, keeper is %s",
                    entry.unique_id,
                    keeper_entry_id,
                )
    elif len(all_entries) > 1:
        _LOGGER.debug(
            "Migration: %d Tado CE entries with %d distinct "
            "unique_ids, multi-home setup, no duplicates",
            len(all_entries),
            len(entries_by_uid),
        )

    return True


def _is_legacy_platform_entry(entry: Any) -> bool:
    """Return True for a pre-v4.1 `sensor.*` row that is a binary_sensor now.

    Home Assistant cannot move an entity between platforms, so the old row has
    to go and let the binary_sensor platform create it fresh with the right
    device class (CONNECTIVITY / POWER).
    """
    if entry.platform != DOMAIN or entry.domain != "sensor":
        return False

    uid = entry.unique_id
    # Connection sensor: tado_ce_{home_id}_device_{serial}_connection
    if "_connection" in uid and "device_" in uid:
        return True
    # Hot water power sensor: tado_ce_{home_id}_zone_{zone_id}_power.
    # Heating and AC power stay as sensors, they report a percentage.
    return (
        "_power" in uid
        and "zone_" in uid
        and "heating" not in uid
        and "ac" not in uid
    )


def _is_retired_window_predicted_entry(entry: Any, home_id: str) -> bool:
    """Return True for this home's Predicted Window row, which v5.0.0 no longer creates."""
    if entry.platform != DOMAIN or entry.domain != "binary_sensor":
        return False
    pattern = rf"{DOMAIN}_{re.escape(home_id)}_zone_\d+_window_predicted"
    return re.fullmatch(pattern, entry.unique_id) is not None


async def async_migrate_registry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Run every entity-registry migration this integration owns, in one walk.

    Home Assistant's own `async_migrate_entries` iterates the config entry's
    registry entries and applies whatever dict the callback returns, skipping
    any entry it returns None for. Both migrations compose into that one walk,
    and returning None for an entry already in its final shape is what makes
    the whole thing idempotent on every load with no version gate.

    Runs from `async_setup_entry`, not `async_migrate_entry`: HA short-circuits
    the latter without importing it whenever an entry already sits at the
    current version/minor tuple, which every healthy install does. See
    `exception_topology.md` for the full rationale.
    """
    from homeassistant.helpers import entity_registry as er

    home_id = str(entry.data.get("home_id"))
    rekey = build_bridge_rekey_callback(hass, home_id)
    registry = er.async_get(hass)
    removed = 0
    retired = 0

    def _migrate(registry_entry: Any) -> dict[str, str] | None:
        nonlocal removed, retired
        if _is_retired_window_predicted_entry(registry_entry, home_id):
            _LOGGER.debug(
                "Migration: removing retired Predicted Window row %s",
                registry_entry.entity_id,
            )
            registry.async_remove(registry_entry.entity_id)
            retired += 1
            return None
        if _is_legacy_platform_entry(registry_entry):
            # A removal is not expressible as an update dict, so do it here and
            # report nothing to change. Upstream integrations use the same idiom.
            _LOGGER.debug(
                "Migration: removing legacy sensor %s, binary_sensor platform "
                "will recreate it",
                registry_entry.entity_id,
            )
            registry.async_remove(registry_entry.entity_id)
            removed += 1
            return None
        return rekey(registry_entry)

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)

    if removed:
        _LOGGER.info(
            "Migration: removed %d legacy sensor entit(ies) so they "
            "can be recreated as binary_sensors",
            removed,
        )
    if retired:
        _LOGGER.info(
            "Migration: removed %d Predicted Window entit(ies), the sensor is "
            "retired in v5.0.0",
            retired,
        )
