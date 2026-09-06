"""Tado CE sensor platform: instantiate every sensor entity per config entry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .config_manager import ConfigurationManager
    from .coordinator import TadoConfigEntry, TadoDataUpdateCoordinator
    from .data_loader import DataLoader

from .sensor_bridge import TadoDynamicBridgeSensor
from .sensor_device import (
    TadoBatterySensor,
)
from .sensor_environment import (
    TadoComfortLevelSensor,
    TadoCondensationRiskSensor,
    TadoDewPointSensor,
    TadoMoldRiskPercentageSensor,
    TadoMoldRiskSensor,
    TadoSurfaceTemperatureSensor,
)
from .sensor_hub import (
    TadoApiBreakdownSensor,
    TadoApiHistorySensor,
    TadoApiLimitSensor,
    TadoApiResetSensor,
    TadoApiStatusSensor,
    TadoApiUsageSensor,
    TadoHomeIdSensor,
    TadoHomekitReadsSavedSensor,
    TadoHomekitWritesSavedSensor,
    TadoLastSyncSensor,
    TadoNextSyncSensor,
    TadoPollingIntervalSensor,
    TadoTokenStatusSensor,
    TadoZoneCountSensor,
)
from .sensor_insight import (
    TadoHomeInsightsSensor,
    TadoZoneInsightsSensor,
)
from .sensor_smart_comfort import (
    TadoNextScheduleTempSensor,
    TadoNextScheduleTimeSensor,
    TadoPreheatAdvisorSensor,
    TadoScheduleDeviationSensor,
    TadoSmartComfortTargetSensor,
)
from .sensor_thermal import (
    TadoApproachFactorSensor,
    TadoConfidenceSensor,
    TadoHeatingAccelerationSensor,
    TadoHeatingRateSensor,
    TadoPreheatTimeSensor,
    TadoThermalInertiaSensor,
)
from .sensor_weather import (
    TadoOutsideTemperatureSensor,
    TadoSolarIntensitySensor,
    TadoWeatherStateSensor,
)
from .sensor_zone import (
    TadoACPowerSensor,
    TadoBoilerFlowTemperatureSensor,
    TadoHeatingPowerSensor,
    TadoHumiditySensor,
    TadoOverlaySensor,
    TadoTargetTempSensor,
    TadoTemperatureSensor,  # Re-exported for base class
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def _create_common_zone_sensors(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    zone_type: str,
    config_manager: ConfigurationManager,
) -> list[SensorEntity]:
    """Create sensors common to HEATING and AC zones."""
    sensors: list[SensorEntity] = [
        TadoTemperatureSensor(coordinator, zone_id, zone_name, zone_type),
        TadoHumiditySensor(coordinator, zone_id, zone_name, zone_type),
        TadoTargetTempSensor(coordinator, zone_id, zone_name, zone_type),
        TadoOverlaySensor(coordinator, zone_id, zone_name, zone_type),
    ]
    sensors.append(TadoZoneInsightsSensor(coordinator, zone_id, zone_name, zone_type))
    sensors.extend(
        [
            TadoMoldRiskSensor(coordinator, zone_id, zone_name, zone_type),
            TadoMoldRiskPercentageSensor(coordinator, zone_id, zone_name, zone_type),
            TadoComfortLevelSensor(coordinator, zone_id, zone_name, zone_type),
            TadoCondensationRiskSensor(coordinator, zone_id, zone_name, zone_type),
            TadoSurfaceTemperatureSensor(coordinator, zone_id, zone_name, zone_type),
            TadoDewPointSensor(coordinator, zone_id, zone_name, zone_type),
        ],
    )
    if config_manager.get_smart_comfort_enabled():
        sensors.extend(
            [
                TadoScheduleDeviationSensor(coordinator, zone_id, zone_name, zone_type),
                TadoNextScheduleTimeSensor(coordinator, zone_id, zone_name, zone_type),
                TadoNextScheduleTempSensor(coordinator, zone_id, zone_name, zone_type),
                TadoPreheatAdvisorSensor(coordinator, zone_id, zone_name, zone_type),
                TadoSmartComfortTargetSensor(coordinator, zone_id, zone_name, zone_type),
            ],
        )
    return sensors


def _create_heating_zone_sensors(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    zone_type: str,
    config_manager: Any,
    zones_with_heating_power: set[str],
    sensors: list[SensorEntity],
) -> None:
    """Create sensors for a HEATING zone."""
    sensors.extend(
        _create_common_zone_sensors(coordinator, zone_id, zone_name, zone_type, config_manager),
    )
    sensors.append(TadoHeatingPowerSensor(coordinator, zone_id, zone_name, zone_type))

    thermal_analytics_zones = config_manager.get_thermal_analytics_zones()
    zone_thermal_enabled = (not thermal_analytics_zones) or (zone_id in thermal_analytics_zones)
    if not (
        config_manager.get_thermal_analytics_enabled()
        and zone_id in zones_with_heating_power
        and zone_thermal_enabled
    ):
        return

    hcc = coordinator.heating_cycle_coordinator
    if not hcc:
        _LOGGER.warning(
            "Sensor: zone %s reports heating power but the heating "
            "cycle coordinator hasn't started yet, thermal analytics "
            "sensors will be created on the next reload",
            zone_name,
        )
        return

    sensors.extend([
        TadoThermalInertiaSensor(coordinator.home_id, hcc, zone_id, zone_name, zone_type),
        TadoHeatingRateSensor(coordinator.home_id, hcc, zone_id, zone_name, zone_type),
        TadoPreheatTimeSensor(coordinator.home_id, hcc, zone_id, zone_name, zone_type),
        TadoConfidenceSensor(coordinator.home_id, hcc, zone_id, zone_name, zone_type),
        TadoHeatingAccelerationSensor(coordinator.home_id, hcc, zone_id, zone_name, zone_type),
        TadoApproachFactorSensor(coordinator.home_id, hcc, zone_id, zone_name, zone_type),
    ])


def _create_hot_water_sensors(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    zone_type: str,
    has_temperature: bool,
    sensors: list[SensorEntity],
) -> None:
    """Create sensors for a HOT_WATER zone."""
    if has_temperature:
        sensors.append(TadoTemperatureSensor(coordinator, zone_id, zone_name, zone_type))
    sensors.append(TadoOverlaySensor(coordinator, zone_id, zone_name, zone_type))


def _create_zone_sensors(
    coordinator: TadoDataUpdateCoordinator,
    data_loader: Any,
    config_manager: Any,
    zone_names: dict[str, str],
    sensors: list[SensorEntity],
) -> None:
    """Create all zone-level sensors from zone data."""
    zones_data = data_loader.load_zones_file()
    zones_info = data_loader.load_zones_info_file()

    zone_types: dict[str, str] = {}
    if zones_info:
        zone_types = {str(z.get("id")): z.get("type") or "" for z in zones_info}

    zones_with_heating_power: set[str] = set()
    if not zones_data:
        return

    zone_states = zones_data.get("zoneStates") or {}
    for zone_id, zone_data in zone_states.items():
        activity_data = zone_data.get("activityDataPoints") or {}
        if activity_data.get("heatingPower") is not None:
            zones_with_heating_power.add(zone_id)

    if zones_with_heating_power:
        _LOGGER.debug(
            "Sensor: zones reporting heating power: %s",
            zones_with_heating_power,
        )

    for zone_id, zone_data in zone_states.items():
        zone_type = zone_types.get(zone_id, "")
        zone_name = zone_names.get(zone_id, f"Zone {zone_id}")

        if zone_type == "HEATING":
            _create_heating_zone_sensors(
                coordinator, zone_id, zone_name, zone_type,
                config_manager, zones_with_heating_power, sensors,
            )
        elif zone_type == "AIR_CONDITIONING":
            sensors.extend(
                _create_common_zone_sensors(coordinator, zone_id, zone_name, zone_type, config_manager),
            )
            sensors.append(TadoACPowerSensor(coordinator, zone_id, zone_name, zone_type))
        elif zone_type == "HOT_WATER":
            sensor_data = zone_data.get("sensorDataPoints") or {}
            has_temperature = (sensor_data.get("insideTemperature") or {}).get("celsius") is not None
            _create_hot_water_sensors(coordinator, zone_id, zone_name, zone_type, has_temperature, sensors)


def _zone_priority(item: tuple[Any, ...]) -> int:
    """Return the priority order for zone sensor creation."""
    zt = item[2]
    if zt == "HEATING":
        return 0
    if zt == "AIR_CONDITIONING":
        return 1
    return 2


def _build_device_zone_map(
    zones_info: list[dict[str, Any]],
) -> dict[str, list[tuple[str, str, str, dict[str, Any]]]]:
    """Build a mapping from device serial to zone info tuples."""
    device_zones: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {}
    for zone in zones_info:
        zone_id = str(zone.get("id"))
        zone_name = zone.get("name", f"Zone {zone_id}")
        zone_type = zone.get("type") or ""
        for device in zone.get("devices") or []:
            serial = device.get("shortSerialNo")
            if serial:
                if serial not in device_zones:
                    device_zones[serial] = []
                device_zones[serial].append((zone_id, zone_name, zone_type, device))
    return device_zones


def _create_device_sensors(
    coordinator: TadoDataUpdateCoordinator,
    data_loader: Any,
    sensors: list[SensorEntity],
) -> None:
    """Create device-level battery sensors (connection is a binary_sensor, created separately)."""
    zones_info = data_loader.load_zones_info_file()
    if not zones_info:
        return

    device_zones = _build_device_zone_map(zones_info)

    for zone_list in device_zones.values():
        zone_list.sort(key=_zone_priority)
        zone_id, zone_name, zone_type, device = zone_list[0]

        if "batteryState" in device:
            sensors.append(
                TadoBatterySensor(coordinator, zone_id, zone_name, zone_type, device, zones_info),
            )


def _create_bridge_sensors(
    coordinator: TadoDataUpdateCoordinator,
    bridge_data: dict[str, Any] | None,
    sensors: list[SensorEntity],
) -> None:
    """Create bridge dynamic discovery sensors and meta sensors."""
    if bridge_data:
        from .bridge_discovery import flatten_response, resolve_entities
        from .bridge_enrichment import FIELD_ENRICHMENT, LEGACY_UNIQUE_ID_MAP

        fields = flatten_response(bridge_data)
        resolved = resolve_entities(
            fields, FIELD_ENRICHMENT, LEGACY_UNIQUE_ID_MAP,
            skip_paths=frozenset({"bridgeConnected"}),
        )
        bridge_sensor_count = 0
        for entity in resolved:
            if entity.platform == "sensor":
                sensors.append(TadoDynamicBridgeSensor(coordinator, entity))
                bridge_sensor_count += 1
        _LOGGER.info(
            "Sensor: discovered %d bridge sensor(s) from %d bridge fields",
            bridge_sensor_count, len(fields),
        )

    # Meta sensors are always created once bridge credentials are
    # present, even before the first bridge poll lands; they expose
    # the capability / schema version state machine to users.
    from .sensor_bridge import (
        TadoBridgeCapabilitiesSensor,
        TadoBridgeSchemaVersionSensor,
    )

    sensors.append(TadoBridgeCapabilitiesSensor(coordinator))
    sensors.append(TadoBridgeSchemaVersionSensor(coordinator))
    _LOGGER.debug(
        "Sensor: bridge meta sensors created (capabilities + schema version)",
    )

    if not bridge_data:
        _LOGGER.info(
            "Sensor: bridge credentials configured but no bridge data "
            "yet, dynamic bridge sensors will be created after the "
            "next reload",
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tado CE sensors from a config entry."""
    coordinator = entry.runtime_data
    config_manager = coordinator.config_manager
    data_loader = coordinator.data_loader

    zones_info = coordinator.data.get("zones_info") or []
    zone_names = {str(z.get("id")): z.get("name", f"Zone {z.get('id')}") for z in zones_info}

    sensors: list[SensorEntity] = []

    sensors.append(TadoHomeIdSensor(coordinator))
    sensors.append(TadoApiUsageSensor(coordinator))
    sensors.append(TadoApiLimitSensor(coordinator))
    sensors.append(TadoApiResetSensor(coordinator))
    sensors.append(TadoApiStatusSensor(coordinator))
    sensors.append(TadoTokenStatusSensor(coordinator))
    sensors.append(TadoZoneCountSensor(coordinator))
    sensors.append(TadoLastSyncSensor(coordinator))

    sensors.append(TadoNextSyncSensor(coordinator))
    sensors.append(TadoPollingIntervalSensor(coordinator))
    sensors.append(TadoApiHistorySensor(coordinator))
    sensors.append(TadoApiBreakdownSensor(coordinator))
    sensors.append(TadoHomeInsightsSensor(coordinator))

    if await hass.async_add_executor_job(_has_boiler_flow_temperature_data, data_loader):
        _LOGGER.debug(
            "Sensor: boiler flow temperature available, creating "
            "TadoBoilerFlowTemperatureSensor",
        )
        sensors.append(TadoBoilerFlowTemperatureSensor(coordinator))
    else:
        _LOGGER.debug(
            "Sensor: no boiler flow temperature in zone data, boiler "
            "flow sensor not created (requires an OpenTherm boiler)",
        )

    # Weather sensors (optional)
    if config_manager.get_weather_enabled():
        sensors.append(TadoOutsideTemperatureSensor(coordinator))
        sensors.append(TadoSolarIntensitySensor(coordinator))
        sensors.append(TadoWeatherStateSensor(coordinator))

    # HomeKit savings sensors (optional)
    if config_manager.get_homekit_enabled():
        sensors.append(TadoHomekitReadsSavedSensor(coordinator))
        sensors.append(TadoHomekitWritesSavedSensor(coordinator))

    try:
        await hass.async_add_executor_job(
            _create_zone_sensors, coordinator, data_loader, config_manager, zone_names, sensors,
        )
    except Exception:
        _LOGGER.warning(
            "Sensor: could not parse zone list while creating zone "
            "sensors, zone-level sensors will retry on the next "
            "reload",
            exc_info=True,
        )

    try:
        await hass.async_add_executor_job(
            _create_device_sensors, coordinator, data_loader, sensors,
        )
    except Exception as e:
        _LOGGER.warning(
            "Sensor: could not parse device list while creating "
            "battery sensors (%s), battery sensors will retry on "
            "the next reload",
            e,
        )

    # Bridge sensors (dynamic discovery)
    bridge_serial = entry.options.get("bridge_serial")
    bridge_auth_key = entry.options.get("bridge_auth_key")
    if bridge_serial and bridge_auth_key:
        bridge_data = coordinator.data.get("bridge")
        _create_bridge_sensors(coordinator, bridge_data, sensors)

    # Weather Compensation sensors (requires bridge + wc_enabled)
    if config_manager.get_wc_enabled() and coordinator.bridge_api_client:
        from .sensor_weather_compensation import (
            TadoWeatherCompensationStatusSensor,
            TadoWeatherCompensationTargetSensor,
        )

        sensors.append(TadoWeatherCompensationTargetSensor(coordinator))
        sensors.append(TadoWeatherCompensationStatusSensor(coordinator))
        _LOGGER.debug(
            "Sensor: weather compensation sensors created (target + status)",
        )

    async_add_entities(sensors, True)
    _LOGGER.info("Sensor: created %d sensor entity(ies)", len(sensors))


def _has_boiler_flow_temperature_data(data_loader: DataLoader) -> bool:
    """Return True when at least one zone reports boilerFlowTemperature."""
    try:
        data = data_loader.load_zones_file()
        if not data:
            return False

        zone_states = data.get("zoneStates") or {}
        for zone_id, zone_data in zone_states.items():
            activity_data = zone_data.get("activityDataPoints") or {}
            flow_temp = (activity_data.get("boilerFlowTemperature") or {}).get("celsius")
            if flow_temp is not None:
                _LOGGER.debug(
                    "Sensor: zone %s reports boiler flow %s°C, boiler "
                    "flow sensor will be created",
                    zone_id, flow_temp,
                )
                return True

        return False
    except Exception as e:
        _LOGGER.debug(
            "Sensor: could not probe zone data for boiler flow "
            "temperature (%s), boiler flow sensor not created",
            e,
        )
        return False
