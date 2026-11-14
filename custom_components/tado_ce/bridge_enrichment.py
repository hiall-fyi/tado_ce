"""Bridge API field enrichment registry: known field metadata mappings."""

from __future__ import annotations

from .bridge_discovery import FieldEnrichment, get_unique_id_suffix

# ---------------------------------------------------------------------------
# Enrichment registry: key is dot-notation field path
# ---------------------------------------------------------------------------

FIELD_ENRICHMENT: dict[str, FieldEnrichment] = {
    "state": FieldEnrichment(
        icon="mdi:pipe-wrench",
        entity_category="diagnostic",
        translation_key="boiler_wiring_state",
        value_formatter="format_bridge_wiring_state",
        unique_id_suffix="boiler_wiring_state",
    ),
    "boiler.outputTemperature.celsius": FieldEnrichment(
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        icon="mdi:thermometer-water",
        entity_category="diagnostic",
        translation_key="boiler_output_temperature",
        unique_id_suffix="boiler_output_temperature",
    ),
    "boiler.outputTemperature.timestamp": FieldEnrichment(
        device_class="timestamp",
        entity_category="diagnostic",
        translation_key="boiler_output_temperature_time",
        enabled_default=False,
        unique_id_suffix="boiler_output_temperature_time",
    ),
    "boiler.flowTemperature.celsius": FieldEnrichment(
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        icon="mdi:thermometer-water",
        entity_category="diagnostic",
        translation_key="boiler_flow_temperature",
        # Full word, never boiler_flow_temp: the short form is the cloud
        # sensor's own suffix, which sits outside the bridge feature group.
        unique_id_suffix="boiler_flow_temperature",
    ),
    "boiler.flowTemperature.timestamp": FieldEnrichment(
        device_class="timestamp",
        entity_category="diagnostic",
        translation_key="boiler_flow_temperature_time",
        enabled_default=False,
        unique_id_suffix="boiler_flow_temperature_time",
    ),
    "boilerMaxOutputTemperatureInCelsius": FieldEnrichment(
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        icon="mdi:thermometer-water",
        entity_category="diagnostic",
        translation_key="boiler_max_output_temperature",
        enabled_default=False,
        unique_id_suffix="boiler_max_output_temperature",
    ),
    "bridgeConnected": FieldEnrichment(
        icon="mdi:lan-connect",
        entity_category="diagnostic",
        value_formatter="format_boolean_connected",
        # No entity is built from this path, see sensor.py's skip_paths: the
        # registry's binary_sensor_bridge_connected already serves this fact.
        # The suffix is still needed because the cleanup derivation reads it,
        # and it is prefixed to stay clear of that entity's bare suffix.
        unique_id_suffix="boiler_bridge_connected",
    ),
    "hotWaterZonePresent": FieldEnrichment(
        icon="mdi:water-boiler",
        entity_category="diagnostic",
        translation_key="boiler_hot_water_present",
        value_formatter="format_boolean_yes_no",
        enabled_default=False,
        unique_id_suffix="boiler_hot_water_present",
    ),
    "deviceWiredToBoiler.type": FieldEnrichment(
        icon="mdi:chip",
        entity_category="diagnostic",
        translation_key="boiler_device_type",
        enabled_default=False,
        unique_id_suffix="boiler_device_type",
    ),
    "deviceWiredToBoiler.serialNo": FieldEnrichment(
        icon="mdi:identifier",
        entity_category="diagnostic",
        translation_key="boiler_device_serial",
        enabled_default=False,
        unique_id_suffix="boiler_device_serial",
    ),
    "deviceWiredToBoiler.thermInterfaceType": FieldEnrichment(
        icon="mdi:connection",
        entity_category="diagnostic",
        translation_key="boiler_interface_type",
        enabled_default=False,
        unique_id_suffix="boiler_interface_type",
    ),
    "deviceWiredToBoiler.connected": FieldEnrichment(
        icon="mdi:lan-connect",
        entity_category="diagnostic",
        translation_key="boiler_device_connected",
        value_formatter="format_boolean_connected",
        enabled_default=False,
        unique_id_suffix="boiler_device_connected",
    ),
}


def bridge_cleanup_suffixes() -> frozenset[str]:
    """Return the bridge unique_id suffixes that an open `bridge_` tail cannot match.

    Derives from the same source that builds these entities, so a renamed
    field moves both together. Suffixes starting `bridge_` are omitted: the
    caller pairs this with an open-tail pattern that already covers them,
    including ones runtime inference mints for unenriched API fields.
    """
    return frozenset(
        suffix
        for path in FIELD_ENRICHMENT
        if not (suffix := get_unique_id_suffix(path, FIELD_ENRICHMENT)).startswith("bridge_")
    )
