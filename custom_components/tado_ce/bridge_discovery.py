"""Bridge API dynamic discovery engine: pure functions, zero HA dependency."""

from __future__ import annotations

from dataclasses import dataclass
import re

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredField:
    """Represent a single discovered leaf field from Bridge API response."""

    path: str  # dot-notation, e.g. "boiler.outputTemperature.celsius"
    value: object  # raw value from API
    value_type: str  # "number", "string", "boolean"


@dataclass(frozen=True, slots=True)
class FieldEnrichment:
    """Represent metadata enrichment for a known Bridge API field."""

    device_class: str | None = None
    state_class: str | None = None
    unit_of_measurement: str | None = None
    icon: str | None = None
    entity_category: str | None = "diagnostic"
    translation_key: str | None = None
    enabled_default: bool = True
    value_formatter: str | None = None  # name of format function in format_helpers.py
    platform: str = "sensor"  # "sensor", "binary_sensor", "number"


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    """Represent a fully resolved entity ready for creation."""

    path: str
    value: object
    value_type: str
    # Resolved metadata (from enrichment or inference)
    device_class: str | None
    state_class: str | None
    unit_of_measurement: str | None
    icon: str | None
    entity_category: str | None
    translation_key: str | None
    enabled_default: bool
    display_value: str  # formatted for HA state
    unique_id_suffix: str  # for unique_id construction
    suggested_name: str  # human-readable name
    platform: str  # "sensor", "binary_sensor", "number"
    source: str  # "enrichment" or "inference"


@dataclass(frozen=True, slots=True)
class ResponseDiff:
    """Represent differences between two consecutive Bridge API responses."""

    added: tuple[str, ...]  # new field paths
    removed: tuple[str, ...]  # disappeared field paths
    type_changed: tuple[tuple[str, str, str], ...]  # (path, old_type, new_type)

    @property
    def has_changes(self) -> bool:
        """Return True if any structural change detected."""
        return bool(self.added or self.removed or self.type_changed)

    @property
    def summary(self) -> str:
        """Return human-readable summary of changes."""
        parts: list[str] = []
        if self.added:
            parts.append(f"added={list(self.added)}")
        if self.removed:
            parts.append(f"removed={list(self.removed)}")
        if self.type_changed:
            parts.append(
                f"type_changed={[(p, o, n) for p, o, n in self.type_changed]}",
            )
        return ", ".join(parts) if parts else "no changes"

    def to_change_list(self) -> list[dict[str, str]]:
        """Return list of change dicts for sensor attributes."""
        changes: list[dict[str, str]] = []
        for path in self.added:
            changes.append({"type": "added", "path": path})
        for path in self.removed:
            changes.append({"type": "removed", "path": path})
        for path, old_type, new_type in self.type_changed:
            changes.append(
                {"type": "type_changed", "path": path, "old": old_type, "new": new_type},
            )
        return changes


@dataclass(frozen=True, slots=True)
class BridgeCapabilities:
    """Represent wiring capabilities derived from discovered fields."""

    wiring_type: str  # "OpenTherm", "eBUS", "Relay", "Unknown"
    has_temperature_monitoring: bool
    has_flow_temperature: bool
    has_output_temperature: bool
    has_max_temp_control: bool
    discovered_field_count: int
    device_type: str | None  # e.g. "RU02", "BP02"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _detect_value_type(value: object) -> str:
    """Classify a Python value into number/string/boolean."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _split_camel(name: str) -> str:
    """Split camelCase into space-separated words."""
    return _CAMEL_RE.sub(" ", name).strip()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def generate_entity_name(path: str) -> str:
    """Convert dot-notation path to human-readable name."""
    parts: list[str] = []
    for segment in path.split("."):
        expanded = _split_camel(segment)
        parts.append(expanded.title())
    return " ".join(parts)


def flatten_response(data: dict[str, object], prefix: str = "") -> list[DiscoveredField]:
    """Recursively flatten JSON response to leaf-value DiscoveredField list."""
    fields: list[DiscoveredField] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            fields.extend(flatten_response(value, prefix=path))
        else:
            fields.append(
                DiscoveredField(
                    path=path,
                    value=value,
                    value_type=_detect_value_type(value),
                ),
            )
    return fields


def get_unique_id_suffix(
    path: str,
    legacy_map: dict[str, str] | None = None,
) -> str:
    """Get unique_id suffix, respecting legacy mappings for backward compat."""
    if legacy_map:
        for legacy_suffix, legacy_path in legacy_map.items():
            if path == legacy_path:
                return legacy_suffix
    return f"bridge_{path.replace('.', '_')}"


def resolve_entities(
    fields: list[DiscoveredField],
    enrichment: dict[str, FieldEnrichment],
    legacy_map: dict[str, str] | None = None,
    skip_paths: frozenset[str] = frozenset(),
) -> list[ResolvedEntity]:
    """Apply enrichment + type inference to produce ResolvedEntity list."""
    # Lazy import to avoid circular dependency: inference is a separate module
    from .bridge_type_inference import format_display_value, infer_metadata

    resolved: list[ResolvedEntity] = []
    for f in fields:
        if f.path in skip_paths:
            continue

        uid_suffix = get_unique_id_suffix(f.path, legacy_map)
        display = format_display_value(f.value, f.value_type, f.path)

        if f.path in enrichment:
            e = enrichment[f.path]
            resolved.append(
                ResolvedEntity(
                    path=f.path,
                    value=f.value,
                    value_type=f.value_type,
                    device_class=e.device_class,
                    state_class=e.state_class,
                    unit_of_measurement=e.unit_of_measurement,
                    icon=e.icon,
                    entity_category=e.entity_category,
                    translation_key=e.translation_key,
                    enabled_default=e.enabled_default,
                    display_value=display,
                    unique_id_suffix=uid_suffix,
                    suggested_name=generate_entity_name(f.path),
                    platform=e.platform,
                    source="enrichment",
                ),
            )
        else:
            meta = infer_metadata(f)
            resolved.append(
                ResolvedEntity(
                    path=f.path,
                    value=f.value,
                    value_type=f.value_type,
                    device_class=meta.device_class,
                    state_class=meta.state_class,
                    unit_of_measurement=meta.unit_of_measurement,
                    icon=meta.icon,
                    entity_category="diagnostic",
                    translation_key=None,
                    enabled_default=False,
                    display_value=display,
                    unique_id_suffix=uid_suffix,
                    suggested_name=generate_entity_name(f.path),
                    platform="sensor",
                    source="inference",
                ),
            )
    return resolved


def diff_responses(
    old_fields: list[DiscoveredField],
    new_fields: list[DiscoveredField],
) -> ResponseDiff:
    """Compare two flattened responses, return added/removed/type_changed."""
    old_map = {f.path: f.value_type for f in old_fields}
    new_map = {f.path: f.value_type for f in new_fields}

    old_paths = set(old_map)
    new_paths = set(new_map)

    added = tuple(sorted(new_paths - old_paths))
    removed = tuple(sorted(old_paths - new_paths))

    type_changed: list[tuple[str, str, str]] = []
    for path in sorted(old_paths & new_paths):
        if old_map[path] != new_map[path]:
            type_changed.append((path, old_map[path], new_map[path]))

    return ResponseDiff(
        added=added,
        removed=removed,
        type_changed=tuple(type_changed),
    )


def extract_capabilities(fields: list[DiscoveredField]) -> BridgeCapabilities:
    """Derive wiring capabilities from discovered fields."""
    paths = {f.path for f in fields}

    has_flow = any(p.startswith("boiler.flowTemperature") for p in paths)
    has_output = any(p.startswith("boiler.outputTemperature") for p in paths)
    has_max_temp = "boilerMaxOutputTemperatureInCelsius" in paths
    has_temp_monitoring = has_flow or has_output

    state_field = next((f for f in fields if f.path == "state"), None)
    state_val = str(state_field.value) if state_field else ""

    device_type_field = next(
        (f for f in fields if f.path == "deviceWiredToBoiler.type"), None,
    )
    device_type = str(device_type_field.value) if device_type_field else None

    wiring_type = _infer_wiring_type(paths, state_val)

    return BridgeCapabilities(
        wiring_type=wiring_type,
        has_temperature_monitoring=has_temp_monitoring,
        has_flow_temperature=has_flow,
        has_output_temperature=has_output,
        has_max_temp_control=has_max_temp,
        discovered_field_count=len(fields),
        device_type=device_type,
    )


def _infer_wiring_type(paths: set[str], state_val: str) -> str:
    """Infer wiring type from field paths and state value."""
    has_boiler = any(p.startswith("boiler.") for p in paths)

    if not has_boiler:
        return "Relay"

    has_flow = any(p.startswith("boiler.flowTemperature") for p in paths)
    has_output = any(p.startswith("boiler.outputTemperature") for p in paths)

    if has_flow and not has_output:
        return "eBUS"
    if has_output:
        return "OpenTherm"

    return "Unknown"
