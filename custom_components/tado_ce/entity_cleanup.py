"""Entity-registry cleanup when feature toggles are disabled in Options Flow (hub device always protected)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import EntityRegistry

    from .coordinator import TadoDataUpdateCoordinator

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .device_manager import _hub_identifier
from .entity_registry import ENTITY_REGISTRY

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FeatureGroupContext: cleanup context driven by EntityMeta.feature_group
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureGroupContext:
    """Represent cleanup context for a feature group."""

    cleanup_flag: str
    feature_group: str
    label: str
    legacy_suffixes: tuple[str, ...]
    # The options-flow toggle that gates this group's entities, or None when
    # the group is always-on (no disable transition).
    toggle_option: str | None = None
    match_mode: str = "suffix"  # "suffix" or "contains"
    platform_filter: str | None = None
    remove_device: str | None = None
    exclude_suffixes: tuple[str, ...] = ()
    zone_only_suffixes: tuple[str, ...] = ()


# Each entry maps a cleanup flag to its feature group and matching rules.

FEATURE_GROUP_CONTEXTS: tuple[FeatureGroupContext, ...] = (
    FeatureGroupContext(
        cleanup_flag="_cleanup_zone_config",
        toggle_option="zone_configuration_enabled",
        feature_group="zone_config",
        label="Zone Configuration",
        legacy_suffixes=(
            # v3.x suffixes (per-zone config entities, not in ENTITY_REGISTRY)
            "_heat_emitter", "_ufh_buffer", "_adaptive_preheat", "_smart_comfort",
            "_window_type", "_window_predicted_sensitivity",
            "_min_temp", "_max_temp", "_temp_offset", "_surface_offset",
            "_external_temp_sensor", "_external_humidity_sensor",
            # v2.x suffixes (pre-migration unique_ids)
            "_heating_type", "_smart_comfort_mode", "_surface_temp_offset",
        ),
        zone_only_suffixes=(
            # These also exist at hub-level, only remove the zone-level variants.
            "_overlay_mode",
            "_overlay_timer",   # v3.x
            "_timer_duration",  # v2.x
        ),
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_thermal_analytics",
        toggle_option="thermal_analytics_enabled",
        feature_group="thermal",
        label="Thermal Analytics",
        legacy_suffixes=(
            "_avg_heating_rate", "_analysis_confidence", "_heating_acceleration",
        ),
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_smart_comfort",
        toggle_option="smart_comfort_enabled",
        feature_group="smart_comfort",
        label="Smart Comfort",
        legacy_suffixes=(
            "_historical_deviation", "_next_schedule_time", "_next_schedule_temp",
            "_smart_comfort_target",
        ),
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_schedule_calendar",
        toggle_option="schedule_calendar_enabled",
        feature_group="schedule_calendar",
        label="Schedule Calendar",
        legacy_suffixes=(),
        exclude_suffixes=(
            # v3.x smart comfort suffixes: protect when smart_comfort still enabled
            "_next_schedule", "_next_sched_temp", "_schedule_deviation",
            # v2.x smart comfort suffixes
            "_next_schedule_time", "_next_schedule_temp",
        ),
        remove_device="heating_schedule",
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_weather",
        toggle_option="weather_enabled",
        feature_group="weather",
        label="Weather",
        legacy_suffixes=(),
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_mobile_devices",
        toggle_option="mobile_devices_enabled",
        feature_group="mobile_devices",
        label="Mobile Devices",
        legacy_suffixes=("_device_",),
        match_mode="contains",
        platform_filter="device_tracker",
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_bridge",
        feature_group="bridge",
        label="Bridge",
        legacy_suffixes=("_bridge_", "_boiler_"),
        match_mode="contains",
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_weather_compensation",
        toggle_option="wc_enabled",
        feature_group="weather_compensation",
        label="Weather Compensation",
        legacy_suffixes=(),
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_homekit",
        toggle_option="homekit_enabled",
        feature_group="homekit",
        label="HomeKit",
        legacy_suffixes=(),
    ),
    FeatureGroupContext(
        cleanup_flag="_cleanup_heating_circuit",
        toggle_option="heating_circuit_enabled",
        feature_group="heating_circuit",
        label="Heating Circuit",
        legacy_suffixes=(),
        platform_filter="select",
    ),
)


# ---------------------------------------------------------------------------
# Pure functions for registry-driven cleanup
# ---------------------------------------------------------------------------


def collect_suffixes_for_group(feature_group: str) -> frozenset[str]:
    """Collect unique_id_suffix values from ENTITY_REGISTRY for a feature group (empty when group has no entries)."""
    return frozenset(
        meta.unique_id_suffix
        for meta in ENTITY_REGISTRY.values()
        if meta.feature_group == feature_group
    )


def build_expected_suffixes(
    registry_suffixes: frozenset[str],
    legacy_suffixes: tuple[str, ...],
) -> frozenset[str]:
    """Merge registry suffixes with legacy suffixes into a single set."""
    return registry_suffixes | frozenset(legacy_suffixes)


# Pre-compiled placeholder patterns for suffix_to_pattern
_PLACEHOLDER_SUBS: tuple[tuple[str, str], ...] = (
    (r"\{zone_id\}", r"\d+"),
    (r"\{duration\}", r"\d+"),
    (r"\{serial\}", r".+"),
    (r"\{device_id\}", r".+"),
)


def suffix_to_pattern(suffix: str) -> re.Pattern[str]:
    r"""Convert a unique_id_suffix with placeholders ({zone_id}/{serial}/...) to a compiled regex anchored at end."""
    escaped = re.escape(suffix)
    for placeholder, replacement in _PLACEHOLDER_SUBS:
        escaped = escaped.replace(placeholder, replacement)
    return re.compile(f".*{escaped}$")


def match_entity_for_cleanup(
    unique_id: str,
    patterns: frozenset[re.Pattern[str]],
    match_mode: str,
    platform_filter: str | None,
    entity_id: str,
    exclude_patterns: frozenset[re.Pattern[str]] | None,
    contains_substrings: tuple[str, ...] = (),
) -> bool:
    """Determine whether an entity should be removed during cleanup (pure function)."""
    if not unique_id.startswith("tado_ce_"):
        return False

    if platform_filter and not entity_id.startswith(f"{platform_filter}."):
        return False

    if match_mode == "contains":
        return any(sub in unique_id for sub in contains_substrings)

    # suffix mode: check excludes first
    if exclude_patterns and any(p.search(unique_id) for p in exclude_patterns):
        return False

    return any(p.search(unique_id) for p in patterns)


def match_zone_only_suffix(unique_id: str, zone_only_patterns: frozenset[re.Pattern[str]]) -> bool:
    """Determine whether a zone-level entity matches zone_only_suffixes (hub-level entities are protected)."""
    if "_zone_" not in unique_id:
        return False
    return any(p.search(unique_id) for p in zone_only_patterns)


# ---------------------------------------------------------------------------
# Feature toggle → cleanup flag mapping, derived from FEATURE_GROUP_CONTEXTS'
# toggle_option (single source, so a toggle can't be created but not cleaned up).
# ---------------------------------------------------------------------------

FEATURE_CLEANUP_MAP: list[tuple[str, str, bool]] = [
    (ctx.toggle_option, ctx.cleanup_flag, False)
    for ctx in FEATURE_GROUP_CONTEXTS
    if ctx.toggle_option is not None
]

# ---------------------------------------------------------------------------
# Internal cleanup application
# ---------------------------------------------------------------------------


def cleanup_orphan_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_suffix: str,
) -> bool:
    """Remove a single named device (e.g. `heating_schedule`) when its feature is disabled."""
    device_registry = dr.async_get(hass)
    home_id = entry.data.get("home_id")

    identifier = f"tado_ce_{home_id}_{device_suffix}" if home_id else f"tado_ce_{device_suffix}"

    device_entry = device_registry.async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id,
    )
    if device_entry:
        _LOGGER.info(
            "Entity Cleanup: removing orphan device %s (identifier %s)",
            device_entry.name, identifier,
        )
        device_registry.async_remove_device(device_entry.id)
        return True
    return False


def cleanup_orphan_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: TadoDataUpdateCoordinator,
) -> int:
    """Sweep every device with zero remaining entities (hub device always protected)."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    # Hub device identifier: never remove
    hub_identifier = _hub_identifier(str(coordinator.home_id))

    removed = 0
    for device_entry in list(device_registry.devices.values()):
        if entry.entry_id not in device_entry.config_entries:
            continue

        if (DOMAIN, hub_identifier) in device_entry.identifiers:
            continue

        device_entities = er.async_entries_for_device(
            entity_registry, device_entry.id, include_disabled_entities=True,
        )
        if not device_entities:
            _LOGGER.info(
                "Entity Cleanup: removing orphan device %s (id %s) "
                ", it has no remaining entities",
                device_entry.name,
                device_entry.id,
            )
            device_registry.async_remove_device(device_entry.id)
            removed += 1

    return removed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_cleanup_flags(
    prev_options: dict[str, Any],
    new_options: dict[str, Any],
) -> dict[str, bool]:
    """Build the set of cleanup flags for features the user just disabled."""
    flags: dict[str, bool] = {}
    for option_key, cleanup_flag, default in FEATURE_CLEANUP_MAP:
        was_enabled = prev_options.get(option_key, default)
        now_enabled = new_options.get(option_key, default)
        if was_enabled and not now_enabled:
            flags[cleanup_flag] = True
            _LOGGER.info(
                "Entity Cleanup: %s disabled, entity removal "
                "scheduled for next reload",
                option_key,
            )

    # Bridge credentials only count as "enabled" when both serial
    # AND auth_key are present, losing either means we should
    # tear down the bridge entities together.
    had_bridge = bool(prev_options.get("bridge_serial")) and bool(prev_options.get("bridge_auth_key"))
    has_bridge = bool(new_options.get("bridge_serial")) and bool(new_options.get("bridge_auth_key"))
    if had_bridge and not has_bridge:
        flags["_cleanup_bridge"] = True
        _LOGGER.info(
            "Entity Cleanup: bridge credentials removed, bridge "
            "entity removal scheduled for next reload",
        )

    return flags


def _apply_cleanup_context(
    entity_registry: EntityRegistry,
    hass: HomeAssistant,
    entry: ConfigEntry,
    ctx: FeatureGroupContext,
) -> int:
    """Run one cleanup pass for a single feature group, returning the entity count removed."""
    removed = 0

    registry_suffixes = collect_suffixes_for_group(ctx.feature_group)
    all_suffixes = build_expected_suffixes(registry_suffixes, ctx.legacy_suffixes)
    patterns = frozenset(suffix_to_pattern(s) for s in all_suffixes)

    exclude_patterns: frozenset[re.Pattern[str]] | None = None
    if ctx.exclude_suffixes:
        exclude_patterns = frozenset(
            suffix_to_pattern(s) for s in ctx.exclude_suffixes
        )

    for entity_id, entity_entry in list(entity_registry.entities.items()):
        if entity_entry.platform != DOMAIN:
            continue
        unique_id = entity_entry.unique_id or ""
        if match_entity_for_cleanup(
            unique_id=unique_id,
            patterns=patterns,
            match_mode=ctx.match_mode,
            platform_filter=ctx.platform_filter,
            entity_id=entity_id,
            exclude_patterns=exclude_patterns,
            contains_substrings=ctx.legacy_suffixes if ctx.match_mode == "contains" else (),
        ):
            _LOGGER.debug(
                "Entity Cleanup: removing entity %s (unique_id %s)",
                entity_id, unique_id,
            )
            entity_registry.async_remove(entity_id)
            removed += 1

    # Zone-only pass: suffixes that also exist on the hub
    # (e.g. overlay_mode), so we only strip the zone-level
    # variants and leave the hub entity alone.
    if ctx.zone_only_suffixes:
        zone_only_patterns = frozenset(
            suffix_to_pattern(s) for s in ctx.zone_only_suffixes
        )
        for entity_id, entity_entry in list(entity_registry.entities.items()):
            if entity_entry.platform != DOMAIN:
                continue
            unique_id = entity_entry.unique_id or ""
            if match_zone_only_suffix(unique_id, zone_only_patterns):
                _LOGGER.debug(
                    "Entity Cleanup: removing zone entity %s "
                    "(unique_id %s)",
                    entity_id, unique_id,
                )
                entity_registry.async_remove(entity_id)
                removed += 1

    if ctx.remove_device:
        cleanup_orphan_device(hass, entry, ctx.remove_device)

    return removed


def cleanup_disabled_feature_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> int:
    """Run every pending cleanup pass and sweep orphan devices afterwards."""
    entity_registry = er.async_get(hass)

    coordinator = getattr(entry, "runtime_data", None)
    pending = getattr(coordinator, "_pending_cleanup", {}) if coordinator else {}
    domain_data = pending.pop(entry.entry_id, {})
    total_removed = 0

    for ctx in FEATURE_GROUP_CONTEXTS:
        if not domain_data.get(ctx.cleanup_flag, False):
            continue

        _LOGGER.info(
            "Entity Cleanup: %s disabled, removing matching entities",
            ctx.label,
        )

        removed = _apply_cleanup_context(entity_registry, hass, entry, ctx)
        total_removed += removed
        _LOGGER.info(
            "Entity Cleanup: removed %d %s entit(ies)",
            removed, ctx.label.lower(),
        )

    if total_removed > 0:
        # domain_data is only non-empty (making total_removed > 0 reachable) when
        # `coordinator` was truthy above, so this is proven non-None here.
        assert coordinator is not None
        orphan_count = cleanup_orphan_devices(hass, entry, coordinator)
        if orphan_count:
            _LOGGER.info(
                "Entity Cleanup: removed %d orphan device(s)",
                orphan_count,
            )

    if total_removed > 0:
        _LOGGER.info(
            "Entity Cleanup: %d entit(ies) removed across all "
            "disabled features",
            total_removed,
        )

    return total_removed
