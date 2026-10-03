"""Bridge API field enrichment registry: known field metadata mappings."""

from __future__ import annotations

from .bridge_discovery import FieldEnrichment

# ---------------------------------------------------------------------------
# Enrichment registry: key is dot-notation field path
# ---------------------------------------------------------------------------

FIELD_ENRICHMENT: dict[str, FieldEnrichment] = {
    "state": FieldEnrichment(
        icon="mdi:pipe-wrench",
        entity_category="diagnostic",
        translation_key="bridge_wiring_state",
        value_formatter="format_bridge_wiring_state",
    ),
    "boiler.outputTemperature.celsius": FieldEnrichment(
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        icon="mdi:thermometer-water",
        entity_category="diagnostic",
        translation_key="bridge_boiler_output_temp",
    ),
    "boiler.outputTemperature.timestamp": FieldEnrichment(
        device_class="timestamp",
        entity_category="diagnostic",
        translation_key="bridge_boiler_output_temp_time",
        enabled_default=False,
    ),
    "boiler.flowTemperature.celsius": FieldEnrichment(
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        icon="mdi:thermometer-water",
        entity_category="diagnostic",
        translation_key="bridge_boiler_flow_temp",
    ),
    "boiler.flowTemperature.timestamp": FieldEnrichment(
        device_class="timestamp",
        entity_category="diagnostic",
        translation_key="bridge_boiler_flow_temp_time",
        enabled_default=False,
    ),
    "boilerMaxOutputTemperatureInCelsius": FieldEnrichment(
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        icon="mdi:thermometer-water",
        entity_category="diagnostic",
        translation_key="bridge_boiler_max_output_temp",
        enabled_default=False,
    ),
    "bridgeConnected": FieldEnrichment(
        icon="mdi:lan-connect",
        entity_category="diagnostic",
        # No entity is built from this path (sensor.py's skip_paths): the
        # registry's binary_sensor_bridge_connected already serves this fact.
        value_formatter="format_boolean_connected",
    ),
    "hotWaterZonePresent": FieldEnrichment(
        icon="mdi:water-boiler",
        entity_category="diagnostic",
        translation_key="bridge_hot_water_present",
        value_formatter="format_boolean_yes_no",
        enabled_default=False,
    ),
    "deviceWiredToBoiler.type": FieldEnrichment(
        icon="mdi:chip",
        entity_category="diagnostic",
        translation_key="bridge_device_type",
        enabled_default=False,
    ),
    "deviceWiredToBoiler.serialNo": FieldEnrichment(
        icon="mdi:identifier",
        entity_category="diagnostic",
        translation_key="bridge_device_serial",
        enabled_default=False,
    ),
    "deviceWiredToBoiler.thermInterfaceType": FieldEnrichment(
        icon="mdi:connection",
        entity_category="diagnostic",
        translation_key="bridge_therm_interface_type",
        enabled_default=False,
    ),
    "deviceWiredToBoiler.connected": FieldEnrichment(
        icon="mdi:lan-connect",
        entity_category="diagnostic",
        translation_key="bridge_device_connected",
        value_formatter="format_boolean_connected",
        enabled_default=False,
    ),
}

# ---------------------------------------------------------------------------
# Backward compatibility: old unique_id_suffix -> field path
# ---------------------------------------------------------------------------

LEGACY_UNIQUE_ID_MAP: dict[str, str] = {
    "boiler_wiring_state": "state",
    "boiler_output_temperature": "boiler.outputTemperature.celsius",
    "boiler_max_output_temperature": "boilerMaxOutputTemperatureInCelsius",
}
