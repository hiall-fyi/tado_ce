"""Tado CE bridge sensors: dynamic field discovery + capabilities + schema tracking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .bridge_discovery import (
    BridgeCapabilities,
    DiscoveredField,
    diff_responses,
    extract_capabilities,
    flatten_response,
)
from .bridge_type_inference import format_display_value
from .device_manager import get_bridge_device_info
from .entity_registry import get_meta
from .helpers import PerEntityAvailabilityMixin, parse_iso_datetime
from .sensor_hub import _ATTR_HISTORY_CAP

if TYPE_CHECKING:
    from datetime import datetime

    from .bridge_discovery import ResolvedEntity
    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named formatter registry: maps value_formatter strings to callables.
# Populated lazily on first use to avoid circular imports.
# ---------------------------------------------------------------------------

_FORMATTER_REGISTRY: dict[str, Any] | None = None


def _get_formatter_registry() -> dict[str, Any]:
    """Build and cache the named formatter registry."""
    global _FORMATTER_REGISTRY
    if _FORMATTER_REGISTRY is None:
        from . import format_helpers as fh

        _FORMATTER_REGISTRY = {
            "format_bridge_wiring_state": fh.format_bridge_wiring_state,
            "format_boolean_connected": fh.format_boolean_connected,
            "format_boolean_yes_no": fh.format_boolean_yes_no,
        }
    return _FORMATTER_REGISTRY


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _resolve_dot_path(data: dict[str, object], path: str) -> object | None:
    """Navigate nested dict by dot-notation path (None on any missing key)."""
    current: object = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _detect_value_type(data: dict[str, object] | None, path: str) -> str:
    """Detect the runtime value type at a dot-path for extra_state_attributes."""
    if data is None:
        return "unknown"
    value = _resolve_dot_path(data, path)
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _format_value(
    value: object,
    path: str,
    value_type: str,
    formatter_name: str | None,
) -> str | float:
    """Format a raw value using a named formatter or type inference fallback."""
    # Named formatter takes priority: defined fields use it; inferred
    # fields fall through to format_display_value.
    if formatter_name:
        registry = _get_formatter_registry()
        fn = registry.get(formatter_name)
        if fn is not None:
            return fn(value)  # type: ignore[no-any-return]
        _LOGGER.debug(
            "Bridge Sensors: formatter %r not in registry, falling back "
            "to type inference",
            formatter_name,
        )

    # Numeric values: return as float for HA native_value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    return format_display_value(value, value_type, path)


# ---------------------------------------------------------------------------
# Dynamic Bridge Sensor
# ---------------------------------------------------------------------------


class TadoDynamicBridgeSensor(
    PerEntityAvailabilityMixin,
    CoordinatorEntity["TadoDataUpdateCoordinator"],
    SensorEntity,
):
    """Generic sensor for any discovered Bridge API field."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        resolved: ResolvedEntity,
        bridge_serial: str,
    ) -> None:
        super().__init__(coordinator)
        self._field_path = resolved.path
        self._value_type = resolved.value_type
        self._value_formatter_name: str | None = None
        self._attr_device_info = get_bridge_device_info(bridge_serial, coordinator.home_id)
        self._data_present = False
        self._attr_native_value = None

        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{resolved.unique_id_suffix}"

        # Translation key (enriched fields have one, inferred fields use suggested_name)
        if resolved.translation_key:
            self._attr_translation_key = resolved.translation_key
        else:
            self._attr_name = resolved.suggested_name

        self._attr_entity_registry_enabled_default = resolved.enabled_default

        if resolved.device_class:
            self._attr_device_class = SensorDeviceClass(resolved.device_class)

        if resolved.state_class:
            self._attr_state_class = SensorStateClass(resolved.state_class)

        if resolved.unit_of_measurement:
            self._attr_native_unit_of_measurement = resolved.unit_of_measurement

        if resolved.icon:
            self._attr_icon = resolved.icon

        if resolved.entity_category:
            self._attr_entity_category = EntityCategory(resolved.entity_category)

        # Store value_formatter name from enrichment (if source is enrichment)
        if resolved.source == "enrichment":
            from .bridge_enrichment import FIELD_ENRICHMENT

            enrichment = FIELD_ENRICHMENT.get(resolved.path)
            if enrichment and enrichment.value_formatter:
                self._value_formatter_name = enrichment.value_formatter

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update sensor from coordinator bridge data."""
        bridge = self.coordinator.data.get("bridge")
        if not bridge:
            self._data_present = False
            self.async_write_ha_state()
            return

        value = _resolve_dot_path(bridge, self._field_path)
        if value is None:
            self._data_present = False
        else:
            # SensorDeviceClass.TIMESTAMP requires a datetime, not a
            # raw ISO string, parse here so HA's value coercion path
            # doesn't reject the sensor.
            if getattr(self, "_attr_device_class", None) == SensorDeviceClass.TIMESTAMP and isinstance(value, str):
                try:
                    self._attr_native_value = parse_iso_datetime(value)
                except (ValueError, TypeError):
                    _LOGGER.debug(
                        "Bridge Sensors: could not parse timestamp %r, "
                        "marking sensor unavailable for this cycle",
                        value,
                    )
                    self._attr_native_value = None
            else:
                self._attr_native_value = _format_value(
                    value,
                    self._field_path,
                    self._value_type,
                    self._value_formatter_name,
                )
            self._data_present = self._attr_native_value is not None
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return source metadata for debugging and auto-documentation."""
        return {
            "source_path": self._field_path,
            "value_type": _detect_value_type(
                self.coordinator.data.get("bridge"),
                self._field_path,
            ),
        }


# ---------------------------------------------------------------------------
# Bridge meta sensors: capabilities summary and schema version tracking
# ---------------------------------------------------------------------------


class TadoBridgeCapabilitiesSensor(
    PerEntityAvailabilityMixin,
    CoordinatorEntity["TadoDataUpdateCoordinator"],
    SensorEntity,
):
    """Sensor showing bridge wiring type and available capabilities."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TadoDataUpdateCoordinator, bridge_serial: str) -> None:
        super().__init__(coordinator)
        meta = get_meta("sensor_bridge_capabilities")
        self._attr_device_info = get_bridge_device_info(bridge_serial, coordinator.home_id)
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{meta.unique_id_suffix}"
        self._attr_translation_key = meta.translation_key
        self._attr_entity_registry_enabled_default = meta.enabled_default
        if meta.icon:
            self._attr_icon = meta.icon
        self._capabilities: BridgeCapabilities | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update capabilities from current bridge data."""
        bridge = self.coordinator.data.get("bridge")
        if not bridge:
            self._data_present = False
            self.async_write_ha_state()
            return
        fields = flatten_response(bridge)
        caps = extract_capabilities(fields)
        self._attr_native_value = caps.wiring_type
        self._data_present = True
        self._capabilities = caps
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return capability flags."""
        if self._capabilities is None:
            return {}
        c = self._capabilities
        return {
            "has_temperature_monitoring": c.has_temperature_monitoring,
            "has_flow_temperature": c.has_flow_temperature,
            "has_output_temperature": c.has_output_temperature,
            "has_max_temp_control": c.has_max_temp_control,
            "discovered_field_count": c.discovered_field_count,
            "device_type": c.device_type,
        }


class TadoBridgeSchemaVersionSensor(
    PerEntityAvailabilityMixin,
    CoordinatorEntity["TadoDataUpdateCoordinator"],
    SensorEntity,
):
    """Sensor tracking Bridge API response schema changes."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TadoDataUpdateCoordinator, bridge_serial: str) -> None:
        super().__init__(coordinator)
        meta = get_meta("sensor_bridge_schema_version")
        self._attr_device_info = get_bridge_device_info(bridge_serial, coordinator.home_id)
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{meta.unique_id_suffix}"
        self._attr_translation_key = meta.translation_key
        self._attr_entity_registry_enabled_default = meta.enabled_default
        if meta.icon:
            self._attr_icon = meta.icon
        self._previous_fields: list[DiscoveredField] | None = None
        self._last_schema_change: datetime | None = None
        self._recent_changes: list[dict[str, str]] = []
        self._current_field_paths: list[str] = []

    @callback
    def _handle_coordinator_update(self) -> None:
        """Detect schema changes between polls."""
        bridge = self.coordinator.data.get("bridge")
        if not bridge:
            self._data_present = False
            self.async_write_ha_state()
            return

        current_fields = flatten_response(bridge)
        self._attr_native_value = len(current_fields)
        self._data_present = True

        if self._previous_fields is not None:
            diff = diff_responses(self._previous_fields, current_fields)
            if diff.has_changes:
                self._last_schema_change = dt_util.utcnow()
                self._recent_changes = diff.to_change_list()
                _LOGGER.info(
                    "Bridge Sensors: API schema changed: %s",
                    diff.summary,
                )

        self._previous_fields = current_fields
        self._current_field_paths = [f.path for f in current_fields]
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return schema metadata."""
        # Cap field_paths to reduce recorder DB bloat. Full native_value
        # (scalar count) is still exposed for schema-drift detection.
        return {
            "field_paths": self._current_field_paths[:_ATTR_HISTORY_CAP],
            "last_schema_change": (
                self._last_schema_change.isoformat() if self._last_schema_change else None
            ),
            "changes_detected": self._recent_changes,
        }
