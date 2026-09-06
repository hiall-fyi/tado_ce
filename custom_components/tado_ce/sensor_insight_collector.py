"""Tado CE insight collector: gathers per-zone, cross-zone, and hub insights."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .calculations import (
    calculate_heat_index,
    classify_comfort_level,
    classify_heat_risk_level,
    classify_mold_risk_level,
)
from .const import ENTITY_DATA_CONDENSATION_RISK, ENTITY_DATA_WINDOW_PREDICTED
from .helpers import get_zone_states, merge_homekit_into_zone_data, parse_iso_datetime
from .insights_api import (
    calculate_api_quota_planning_insight,
    calculate_api_usage_spike_insight,
    calculate_calls_per_hour,
)
from .insights_cross_zone import (
    aggregate_cross_zone_condensation,
    aggregate_cross_zone_mold_risk,
    aggregate_cross_zone_window_predicted,
    calculate_cross_zone_efficiency_insight,
    calculate_humidity_imbalance_insight,
    calculate_temperature_imbalance_insight,
)
from .insights_device import (
    calculate_battery_recommendation,
    calculate_connection_recommendation,
    calculate_geofencing_device_offline_insight,
)
from .insights_environment import (
    calculate_comfort_recommendation,
    calculate_humidity_trend_insight,
    calculate_mold_risk_recommendation,
)
from .insights_heating import (
    calculate_boiler_flow_anomaly_insight,
    calculate_heating_anomaly_insight,
    calculate_heating_off_cold_room_insight,
    calculate_poor_thermal_efficiency_insight,
    calculate_preheat_timing_insight,
)
from .insights_misc import (
    calculate_away_heating_active_insight,
    calculate_frost_risk_insight,
    calculate_home_all_off_insight,
    calculate_schedule_gap_insight,
    calculate_weather_impact_insight,
)
from .insights_models import Insight
from .insights_presenter import get_insight_priority

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Insight collection thresholds
_HUMIDITY_HISTORY_MAX_SAMPLES = 48  # max humidity readings to keep per zone
_HEATING_ANOMALY_POWER_PCT = 80  # %: high power threshold for anomaly detection
_HEATING_ANOMALY_TEMP_DELTA = 0.5  # °C: near-target threshold for anomaly detection
_OUTDOOR_TEMP_MIN_SAMPLES = 48  # minimum outdoor temp readings for 7-day average

# NOAA heat-risk bands, low to high, for vulnerable-group promotion.
_HEAT_RISK_BANDS = ("None", "Caution", "Extreme Caution", "Danger", "Extreme Danger")


def _promote_one_band(risk: str) -> str:
    """Bump a NOAA heat-risk band up one step for vulnerable-group homes.

    "None" (not hot) stays "None" so a mild room never becomes a warning;
    "Extreme Danger" saturates because there is no higher band.
    """
    if risk == "None":
        return "None"
    idx = _HEAT_RISK_BANDS.index(risk)
    return _HEAT_RISK_BANDS[min(idx + 1, len(_HEAT_RISK_BANDS) - 1)]


@dataclass(frozen=True)
class InsightContext:
    """Immutable config snapshot for gating insights per poll cycle."""

    environment_enabled: bool
    preheat_enabled: bool
    thermal_enabled: bool
    schedule_enabled: bool
    weather_enabled: bool
    presence_enabled: bool
    geofencing_active: bool
    comfort_heat_vulnerable_group: bool = False

    @classmethod
    def from_coordinator(cls, coordinator: TadoDataUpdateCoordinator) -> InsightContext:
        """Build context from coordinator config and API state."""
        cfg = coordinator.config_manager

        # Geofencing is active only when presence sync is on AND
        # the API reports presenceLocked=False (i.e. auto/geofencing mode).
        presence_enabled = cfg.get_home_state_sync_enabled()
        home_state = (coordinator.data or {}).get("home_state") or {}
        geofencing_active = presence_enabled and not home_state.get(
            "presenceLocked", True,
        )

        return cls(
            # Environment sensors are always on (the old config toggle was
            # never user-reachable); keep the gate field for the collector's
            # per-cycle insight gating, which other features still share.
            environment_enabled=True,
            preheat_enabled=cfg.get_adaptive_preheat_enabled(),
            thermal_enabled=cfg.get_thermal_analytics_enabled(),
            schedule_enabled=cfg.get_schedule_calendar_enabled(),
            weather_enabled=cfg.get_weather_enabled(),
            presence_enabled=presence_enabled,
            geofencing_active=geofencing_active,
            comfort_heat_vulnerable_group=cfg.get_comfort_heat_vulnerable_group(),
        )


def _collect_mold_risk_insight(
    zone_name: str,
    inside_temp: float,
    humidity: float,
    insights: list[Any],
) -> None:
    """Collect mold risk insight if risk level is medium or above."""
    risk = classify_mold_risk_level(inside_temp, humidity)
    if risk in ("Critical", "High", "Medium"):
        rec = calculate_mold_risk_recommendation(
            risk_level=risk,
            zone_name=zone_name,
            humidity=humidity,
            current_temp=inside_temp,
        )
        insights.append(
            Insight(
                priority=get_insight_priority("mold_risk", risk.lower()),
                recommendation=rec,
                insight_type="mold_risk",
                zone_name=zone_name,
            ),
        )


def _collect_comfort_insight(
    zone_name: str,
    inside_temp: float,
    insights: list[Any],
    power: str = "ON",
    is_cooling_zone: bool = False,
    humidity: float | None = None,
    is_vulnerable_group: bool = False,
) -> None:
    """Collect comfort insight if temperature is outside comfortable range."""
    comfort_state = classify_comfort_level(inside_temp)
    heat_index: float | None = None
    heat_risk: str | None = None
    if comfort_state in ("Cold", "Cool"):
        # Cold comfort only when powered ON. When heating is off, a room well
        # below target is already flagged by the heating-off-cold insight, so
        # firing here too would duplicate it.
        if power != "ON":
            return
        severity = "too_cold"
    elif comfort_state == "Hot":
        # Hot comfort fires regardless of power; summer overheat matters most
        # exactly when heating is off. Severity follows the NOAA heat-index band.
        if humidity is None:
            severity = "too_hot"
        else:
            heat_index = calculate_heat_index(inside_temp, humidity)
            heat_risk = classify_heat_risk_level(heat_index)
            risk_for_severity = (
                _promote_one_band(heat_risk) if is_vulnerable_group else heat_risk
            )
            severity = {
                "Extreme Danger": "extreme_danger",
                "Danger": "danger",
            }.get(risk_for_severity, "too_hot")
    else:
        return
    rec = calculate_comfort_recommendation(
        comfort_state=comfort_state,
        zone_name=zone_name,
        current_temp=inside_temp,
        is_cooling_zone=is_cooling_zone,
        heat_index=heat_index,
        heat_risk_level=heat_risk,
    )
    insights.append(
        Insight(
            priority=get_insight_priority("comfort", severity),
            recommendation=rec,
            insight_type="comfort",
            zone_name=zone_name,
        ),
    )


def _collect_boiler_flow_insight(
    zone_data: dict[str, Any], zone_name: str, insights: list[Any],
) -> None:
    """Collect boiler flow anomaly insight."""
    activity = zone_data.get("activityDataPoints") or {}
    flow_data = activity.get("boilerFlowTemperature") or {}
    flow_temp = flow_data.get("celsius")
    if flow_temp is not None:
        hp_pct = (activity.get("heatingPower") or {}).get("percentage")
        insight = calculate_boiler_flow_anomaly_insight(
            flow_temp=flow_temp,
            heating_power_pct=hp_pct,
            zone_name=zone_name,
        )
        if insight:
            insights.append(insight)


def _collect_humidity_trend_insight(
    zone_id: str,
    zone_name: str,
    humidity: float,
    humidity_histories: dict[str, list[Any]],
    insights: list[Any],
) -> None:
    """Collect humidity trend insight, maintaining per-zone history."""
    if zone_id not in humidity_histories:
        humidity_histories[zone_id] = []
    humidity_histories[zone_id].append(humidity)
    if len(humidity_histories[zone_id]) > _HUMIDITY_HISTORY_MAX_SAMPLES:
        humidity_histories[zone_id] = humidity_histories[zone_id][-_HUMIDITY_HISTORY_MAX_SAMPLES:]
    insight = calculate_humidity_trend_insight(
        current_humidity=humidity,
        humidity_history=humidity_histories[zone_id],
        zone_name=zone_name,
    )
    if insight:
        insights.append(insight)


def _collect_optional_zone_insights(
    ctx: InsightContext,
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    zone_data: dict[str, Any],
    inside_temp: float | None,
    schedules: dict[str, Any] | None,
    humidity: float | None,
    humidity_histories: dict[str, list[Any]] | None,
    insights: list[Any],
) -> None:
    """Collect optional zone insights gated by config context."""
    if ctx.thermal_enabled:
        insight = _check_thermal_efficiency(coordinator, zone_id, zone_name)
        if insight:
            insights.append(insight)
        _collect_boiler_flow_insight(zone_data, zone_name, insights)

    if ctx.schedule_enabled and schedules and inside_temp is not None:
        insight = _check_schedule_gap(schedules, zone_id, zone_data, inside_temp, zone_name)
        if insight:
            insights.append(insight)

    if ctx.environment_enabled and humidity_histories is not None and humidity is not None:
        _collect_humidity_trend_insight(zone_id, zone_name, humidity, humidity_histories, insights)


def collect_single_zone_insights(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    zone_data: dict[str, Any],
    zones_info: list[Any] | None,
    anomaly_start_times: dict[str, datetime],
    humidity_histories: dict[str, list[Any]] | None = None,
    schedules: dict[str, Any] | None = None,
    ctx: InsightContext | None = None,
) -> list[Any]:
    """Collect all insights for a single zone."""
    if ctx is None:
        ctx = InsightContext.from_coordinator(coordinator)

    insights: list[Any] = []

    sensor_data = zone_data.get("sensorDataPoints") or {}
    humidity = (sensor_data.get("humidity") or {}).get("percentage")
    inside_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")

    # --- Mold risk (environment) ---
    if ctx.environment_enabled and humidity is not None and inside_temp is not None:
        _collect_mold_risk_insight(zone_name, inside_temp, humidity, insights)

    # --- Comfort (environment): hot fires even when heating OFF; cold gated ON ---
    setting = zone_data.get("setting") or {}
    power = setting.get("power", "OFF")
    is_cooling_zone = setting.get("type") == "AIR_CONDITIONING"
    if ctx.environment_enabled and inside_temp is not None:
        _collect_comfort_insight(
            zone_name, inside_temp, insights, power, is_cooling_zone,
            humidity, ctx.comfort_heat_vulnerable_group,
        )

    # --- Coordinator-based insights (condensation, window, preheat, anomaly) ---
    _collect_ha_entity_insights(
        coordinator,
        zone_id,
        zone_name,
        zone_data,
        inside_temp,
        anomaly_start_times,
        insights,
        ctx,
    )

    # --- Heating off + cold room (always relevant: basic safety) ---
    result = calculate_heating_off_cold_room_insight(
        power_state=setting.get("power"),
        current_temp=inside_temp,
        target_temp=(setting.get("temperature") or {}).get("celsius"),
        zone_name=zone_name,
        is_vulnerable_group=ctx.comfort_heat_vulnerable_group,
    )
    if result:
        severity, recommendation = result
        priority = get_insight_priority("heating_off_cold", severity)
        insights.append(
            Insight(
                priority=priority,
                recommendation=recommendation,
                insight_type="heating_off_cold",
                zone_name=zone_name,
            )
        )

    # --- Optional insights gated by config ---
    _collect_optional_zone_insights(
        ctx, coordinator, zone_id, zone_name, zone_data, inside_temp,
        schedules, humidity, humidity_histories, insights,
    )

    # --- Battery and connection (always relevant: device health) ---
    if zones_info:
        _collect_single_zone_device_insights(zone_id, zone_name, zones_info, insights)

    return insights


def collect_zone_insights(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    anomaly_start_times: dict[str, datetime],
    humidity_histories: dict[str, list[Any]],
) -> dict[str, list[Any]]:
    """Collect insights from all zones using the coordinator's cached zone data."""
    zone_insights: dict[str, list[Any]] = {}

    try:
        ctx = InsightContext.from_coordinator(coordinator)
        coord_data = coordinator.data or {}
        zones_info = coord_data.get("zones_info")
        zone_states = get_zone_states(coord_data)
        if not zone_states:
            return zone_insights

        schedules = coord_data.get("schedules")

        zone_name_map: dict[str, str] = {}
        if zones_info:
            for z in zones_info:
                zone_name_map[str(z.get("id"))] = z.get("name", f"Zone {z.get('id')}")

        for zone_id, zone_data in zone_states.items():
            zone_name = zone_name_map.get(zone_id, f"Zone {zone_id}")
            # Merge HomeKit data so insights use the freshest readings
            merged_zone_data = merge_homekit_into_zone_data(zone_data, zone_id, coordinator)
            insights = collect_single_zone_insights(
                hass=hass,
                coordinator=coordinator,
                zone_id=zone_id,
                zone_name=zone_name,
                zone_data=merged_zone_data,
                zones_info=zones_info,
                anomaly_start_times=anomaly_start_times,
                humidity_histories=humidity_histories,
                schedules=schedules,
                ctx=ctx,
            )
            if insights:
                zone_insights[zone_name] = insights

    except Exception as e:
        _LOGGER.debug(
            "Insight Collector: zone insight collection failed (%s), "
            "returning whatever insights were gathered so far",
            e,
        )

    return zone_insights


def _collect_condensation_insight(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    insights: list[Any],
) -> None:
    """Collect condensation risk insight for a zone."""
    cond_data = coordinator.get_entity_data(zone_id, ENTITY_DATA_CONDENSATION_RISK)
    if not cond_data:
        return
    cond_state = cond_data.get("state", "None")
    if cond_state in ("None", "Low", "unavailable", "unknown"):
        return
    cond_rec = cond_data.get("recommendation", "") or f"{zone_name}: Condensation risk detected"
    insights.append(
        Insight(
            priority=get_insight_priority("condensation", cond_state.lower()),
            recommendation=cond_rec,
            insight_type="condensation",
            zone_name=zone_name,
        ),
    )


def _collect_window_predicted_insight(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    insights: list[Any],
) -> None:
    """Collect window predicted insight for a zone."""
    wp_data = coordinator.get_entity_data(zone_id, ENTITY_DATA_WINDOW_PREDICTED)
    if not wp_data or wp_data.get("state") != "on":
        return
    wp_rec = wp_data.get("recommendation", "") or f"{zone_name}: Possible open window detected"
    insights.append(
        Insight(
            priority=get_insight_priority("window_predicted", "high"),
            recommendation=wp_rec,
            insight_type="window_predicted",
            zone_name=zone_name,
        ),
    )


def _collect_preheat_insight(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    zone_data: dict[str, Any],
    insights: list[Any],
) -> None:
    """Collect preheat timing insight for a zone."""
    hcc = coordinator.heating_cycle_coordinator
    ph_min: float | None = None
    if hcc:
        zone_state = hcc.get_zone_state(zone_id)
        if zone_state:
            current_temp = zone_state.get("current_temp")
            target_temp = zone_state.get("target_temp")
            if current_temp is not None and target_temp is not None:
                ph_min = hcc.estimate_preheat_time(zone_id, current_temp, target_temp)

    sc_val: str | None = None
    schedules = (coordinator.data or {}).get("schedules") or {}
    zone_schedule = schedules.get(zone_id) or schedules.get(str(zone_id))
    if zone_schedule:
        next_change = zone_data.get("nextScheduleChange") or {}
        sc_val = next_change.get("start")

    insight = calculate_preheat_timing_insight(
        preheat_time_minutes=ph_min,
        next_schedule_time=sc_val,
        zone_name=zone_name,
    )
    if insight:
        insights.append(insight)


def _collect_heating_anomaly_insight(
    zone_data: dict[str, Any],
    zone_id: str,
    zone_name: str,
    inside_temp: float | None,
    anomaly_start_times: dict[str, datetime],
    insights: list[Any],
) -> None:
    """Collect heating anomaly insight for a zone."""
    activity = zone_data.get("activityDataPoints") or {}
    heating_power = activity.get("heatingPower") or {}
    power_pct = heating_power.get("percentage")
    if power_pct is None:
        return
    try:
        power_pct = float(power_pct)
        setting = zone_data.get("setting") or {}
        target = (setting.get("temperature") or {}).get("celsius")
        if inside_temp is None or target is None:
            return
        temp_delta = abs(inside_temp - target)
        if power_pct >= _HEATING_ANOMALY_POWER_PCT and temp_delta < _HEATING_ANOMALY_TEMP_DELTA:
            if zone_id not in anomaly_start_times:
                anomaly_start_times[zone_id] = dt_util.utcnow()
            elapsed = (dt_util.utcnow() - anomaly_start_times[zone_id]).total_seconds() / 60
            ha_insight = calculate_heating_anomaly_insight(
                heating_power_pct=power_pct,
                temp_delta=temp_delta,
                duration_minutes=int(elapsed),
                zone_name=zone_name,
            )
            if ha_insight:
                insights.append(ha_insight)
        else:
            anomaly_start_times.pop(zone_id, None)
    except (ValueError, TypeError):
        pass


def _collect_ha_entity_insights(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
    zone_data: dict[str, Any],
    inside_temp: float | None,
    anomaly_start_times: dict[str, datetime],
    insights: list[Any],
    ctx: InsightContext,
) -> None:
    """Collect insights using coordinator data instead of hass.states.get()."""
    if ctx.environment_enabled:
        _collect_condensation_insight(coordinator, zone_id, zone_name, insights)
        _collect_window_predicted_insight(coordinator, zone_id, zone_name, insights)

    if ctx.preheat_enabled:
        _collect_preheat_insight(coordinator, zone_id, zone_name, zone_data, insights)

    if ctx.thermal_enabled:
        _collect_heating_anomaly_insight(zone_data, zone_id, zone_name, inside_temp, anomaly_start_times, insights)


def _check_thermal_efficiency(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    zone_name: str,
) -> dict[str, Any] | None:
    """Check poor thermal efficiency insight (reads thermal analytics from HeatingCycleCoordinator)."""
    hcc = coordinator.heating_cycle_coordinator
    if not hcc:
        return None
    zone_data = hcc.get_zone_data(zone_id)
    if not zone_data:
        return None
    ti_val = zone_data.get("inertia_time")
    hr_val = zone_data.get("heating_rate")
    conf_val = zone_data.get("confidence_score")
    return calculate_poor_thermal_efficiency_insight(  # type: ignore[return-value]
        thermal_inertia=ti_val,
        heating_rate=hr_val,
        confidence_score=conf_val,
        zone_name=zone_name,
    )


def _check_schedule_gap(
    schedules: dict[str, Any],
    zone_id: str,
    zone_data: dict[str, Any],
    inside_temp: float,
    zone_name: str,
) -> dict[str, Any] | None:
    """Check schedule gap insight for a zone."""
    zone_schedule = schedules.get(zone_id)
    if not zone_schedule:
        return None

    raw_blocks = zone_schedule.get("blocks") or zone_schedule.get("schedule") or []
    if isinstance(raw_blocks, dict):
        blocks = [b for day_blocks in raw_blocks.values() for b in day_blocks]
    else:
        blocks = raw_blocks

    setting = zone_data.get("setting") or {}
    next_target = (setting.get("temperature") or {}).get("celsius")

    longest_off = None
    if blocks:
        off_durations = []
        for block in blocks:
            block_setting = block.get("setting") or {}
            if block_setting.get("power") == "OFF":
                start_str = block.get("start", "")
                end_str = block.get("end", "")
                if start_str and end_str:
                    try:
                        sh, sm = int(start_str.split(":")[0]), int(start_str.split(":")[1])
                        eh, em = int(end_str.split(":")[0]), int(end_str.split(":")[1])
                        dur = (eh * 60 + em) - (sh * 60 + sm)
                        if dur < 0:
                            dur += 24 * 60
                        off_durations.append(dur / 60.0)
                    except (ValueError, IndexError):
                        pass
        if off_durations:
            longest_off = max(off_durations)

    return calculate_schedule_gap_insight(  # type: ignore[return-value]
        schedule_blocks=blocks or None,
        current_temp=inside_temp,
        next_target_temp=next_target,
        longest_off_hours=longest_off,
        zone_name=zone_name,
    )


def _collect_single_zone_device_insights(
    zone_id: str,
    zone_name: str,
    zones_info: list[Any],
    insights: list[Any],
) -> None:
    """Collect battery and connection insights for a single zone."""
    zone_info = next(
        (z for z in zones_info if str(z.get("id")) == zone_id),
        None,
    )
    if not zone_info:
        return

    # Tado API may return null for 'devices'; 'or []' handles None correctly
    for device in zone_info.get("devices") or []:
        battery = device.get("batteryState")
        if battery and battery.upper() in ("LOW", "CRITICAL"):
            device_type = device.get("deviceType", "unknown")
            rec = calculate_battery_recommendation(
                battery_state=battery,
                zone_name=zone_name,
                device_type=device_type,
            )
            severity = "critical" if battery.upper() == "CRITICAL" else "low"
            insights.append(
                Insight(
                    priority=get_insight_priority("battery", severity),
                    recommendation=rec,
                    insight_type="battery",
                    zone_name=zone_name,
                ),
            )

        conn = device.get("connectionState") or {}
        conn_value = conn.get("value")
        if conn_value is not None and not conn_value:
            rec = calculate_connection_recommendation(
                connection_state="Offline",
                zone_name=zone_name,
            )
            insights.append(
                Insight(
                    priority=get_insight_priority("connection", "offline"),
                    recommendation=rec,
                    insight_type="connection",
                    zone_name=zone_name,
                ),
            )


def _collect_cross_zone_mold(
    zone_states: dict[str, Any], zone_name_map: dict[str, str],
) -> Any | None:
    """Collect cross-zone mold risk insight."""
    zone_mold_risks: dict[str, str] = {}
    for zone_id, zone_data in zone_states.items():
        zone_name = zone_name_map.get(zone_id, f"Zone {zone_id}")
        sensor_data = zone_data.get("sensorDataPoints") or {}
        humidity = (sensor_data.get("humidity") or {}).get("percentage")
        inside_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
        if humidity is not None and inside_temp is not None:
            zone_mold_risks[zone_name] = classify_mold_risk_level(inside_temp, humidity)
    return aggregate_cross_zone_mold_risk(zone_mold_risks)


def _collect_cross_zone_windows(
    coordinator: TadoDataUpdateCoordinator, zones_info: list[Any],
) -> Any | None:
    """Collect cross-zone window predicted insight."""
    zone_window_states: dict[str, bool] = {}
    for z in zones_info:
        z_name = z.get("name", f"Zone {z.get('id')}")
        z_id = str(z.get("id"))
        wp_data = coordinator.get_entity_data(z_id, ENTITY_DATA_WINDOW_PREDICTED)
        if wp_data:
            zone_window_states[z_name] = wp_data.get("state") == "on"
    return aggregate_cross_zone_window_predicted(zone_window_states)


def _collect_cross_zone_condensation(
    coordinator: TadoDataUpdateCoordinator, zones_info: list[Any],
) -> Any | None:
    """Collect cross-zone condensation insight."""
    zone_cond_states: dict[str, str] = {}
    for z in zones_info:
        z_name = z.get("name", f"Zone {z.get('id')}")
        z_id = str(z.get("id"))
        cond_data = coordinator.get_entity_data(z_id, ENTITY_DATA_CONDENSATION_RISK)
        if cond_data:
            cond_state = cond_data.get("state", "None")
            if cond_state not in ("unavailable", "unknown"):
                zone_cond_states[z_name] = cond_state
    return aggregate_cross_zone_condensation(zone_cond_states)


def _collect_cross_zone_efficiency(
    coordinator: TadoDataUpdateCoordinator, zones_info: list[Any],
) -> Any | None:
    """Collect cross-zone heating efficiency insight."""
    hcc = coordinator.heating_cycle_coordinator
    if not hcc:
        return None
    zone_heating_rates: dict[str, float] = {}
    for z in zones_info:
        z_name = z.get("name", f"Zone {z.get('id')}")
        z_id = str(z.get("id"))
        zone_hcc_data = hcc.get_zone_data(z_id)
        if zone_hcc_data:
            hr_val = zone_hcc_data.get("heating_rate")
            if hr_val is not None:
                zone_heating_rates[z_name] = hr_val
    return calculate_cross_zone_efficiency_insight(zone_heating_rates)


def _collect_temp_imbalance(
    zone_states: dict[str, Any], zone_name_map: dict[str, str],
) -> Any | None:
    """Collect temperature imbalance insight across active zones."""
    zone_temps: dict[str, float] = {}
    for zid, zd in zone_states.items():
        z_name = zone_name_map.get(zid, f"Zone {zid}")
        s = zd.get("setting") or {}
        if s.get("power") == "ON":
            sd = zd.get("sensorDataPoints") or {}
            t = (sd.get("insideTemperature") or {}).get("celsius")
            if t is not None:
                zone_temps[z_name] = t
    return calculate_temperature_imbalance_insight(zone_temps)


def _collect_humidity_imbalance(
    zone_states: dict[str, Any], zone_name_map: dict[str, str],
) -> Any | None:
    """Collect humidity imbalance insight across zones."""
    zone_hums: dict[str, float] = {}
    for zid, zd in zone_states.items():
        z_name = zone_name_map.get(zid, f"Zone {zid}")
        sd = zd.get("sensorDataPoints") or {}
        h = (sd.get("humidity") or {}).get("percentage")
        if h is not None:
            zone_hums[z_name] = h
    return calculate_humidity_imbalance_insight(zone_hums)


def _collect_all_cross_zone(
    ctx: InsightContext,
    coordinator: TadoDataUpdateCoordinator,
    zone_states: dict[str, Any],
    zone_name_map: dict[str, str],
    zones_info: list[Any] | None,
) -> list[Any]:
    """Collect all cross-zone insights based on context flags."""
    candidates: list[Any] = []

    if ctx.environment_enabled and zone_states:
        candidates.append(_collect_cross_zone_mold(zone_states, zone_name_map))

    if ctx.environment_enabled and zones_info:
        candidates.append(_collect_cross_zone_windows(coordinator, zones_info))
        candidates.append(_collect_cross_zone_condensation(coordinator, zones_info))

    if ctx.thermal_enabled and zones_info:
        candidates.append(_collect_cross_zone_efficiency(coordinator, zones_info))

    if zone_states:
        candidates.append(_collect_temp_imbalance(zone_states, zone_name_map))

    if ctx.environment_enabled and zone_states:
        candidates.append(_collect_humidity_imbalance(zone_states, zone_name_map))

    return [c for c in candidates if c is not None]


def get_cross_zone_insights(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    zone_insights: dict[str, list[Any]],
    ctx: InsightContext | None = None,
) -> list[Any]:
    """Get cross-zone aggregation insights (mold, windows, condensation, efficiency, imbalance)."""
    if ctx is None:
        ctx = InsightContext.from_coordinator(coordinator)

    try:
        coord_data = coordinator.data or {}
        zones_info = coord_data.get("zones_info")

        zone_name_map: dict[str, str] = {}
        if zones_info:
            for z in zones_info:
                zone_name_map[str(z.get("id"))] = z.get("name", f"Zone {z.get('id')}")

        zone_states = get_zone_states(coord_data)

        # Merge HomeKit data into each zone for fresher readings
        merged_states = {}
        for zid, zdata in zone_states.items():
            merged_states[zid] = merge_homekit_into_zone_data(zdata, zid, coordinator)

        return _collect_all_cross_zone(ctx, coordinator, merged_states, zone_name_map, zones_info)

    except Exception as e:
        _LOGGER.debug(
            "Insight Collector: cross-zone insight collection failed "
            "(%s), returning whatever was gathered so far",
            e,
        )

    return []


def _collect_api_quota_insight(
    coord_data: dict[str, Any], hub_insights: list[Any],
) -> None:
    """Collect API quota planning insight."""
    ratelimit = coord_data.get("ratelimit")
    if not ratelimit:
        return
    remaining = ratelimit.get("remaining")
    total = ratelimit.get("limit")
    reset_seconds = ratelimit.get("reset_seconds")
    hours_until_reset = None
    if reset_seconds is not None and reset_seconds > 0:
        hours_until_reset = reset_seconds / 3600

    history_raw = coord_data.get("api_call_history")
    if history_raw and isinstance(history_raw, dict):
        history = [call for calls in history_raw.values() for call in calls]
    else:
        history = history_raw or []
    calls_per_hour = calculate_calls_per_hour(history) if history else None

    if remaining is not None and calls_per_hour is not None:
        insight = calculate_api_quota_planning_insight(
            remaining_calls=remaining,
            total_calls=total,
            calls_per_hour=calls_per_hour,
            hours_until_reset=hours_until_reset,
        )
        if insight:
            hub_insights.append(insight)


def _collect_weather_insights(
    coordinator: TadoDataUpdateCoordinator,
    coord_data: dict[str, Any],
    hub_insights: list[Any],
) -> None:
    """Collect weather impact and frost risk insights."""
    weather = coord_data.get("weather")
    if not weather:
        return
    outdoor_temp = (weather.get("outsideTemperature") or {}).get("celsius")
    if outdoor_temp is None:
        return
    outdoor_temp_history = coordinator.outdoor_temp_history

    avg_7d = None
    if len(outdoor_temp_history) >= _OUTDOOR_TEMP_MIN_SAMPLES:
        avg_7d = sum(outdoor_temp_history) / len(outdoor_temp_history)
    insight = calculate_weather_impact_insight(
        current_outdoor_temp=outdoor_temp,
        avg_outdoor_temp_7d=avg_7d,
    )
    if insight:
        hub_insights.append(insight)

    frost_insight = calculate_frost_risk_insight(outdoor_temp=outdoor_temp)
    if frost_insight:
        hub_insights.append(frost_insight)


def get_hub_insights(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    ctx: InsightContext | None = None,
) -> list[Any]:
    """Get hub-level insights (API quota, weather, presence)."""
    if ctx is None:
        ctx = InsightContext.from_coordinator(coordinator)

    hub_insights: list[Any] = []

    try:
        coord_data = coordinator.data or {}

        _collect_api_quota_insight(coord_data, hub_insights)

        if ctx.weather_enabled:
            _collect_weather_insights(coordinator, coord_data, hub_insights)

        if ctx.presence_enabled:
            _collect_presence_insights(hass, coordinator, hub_insights)

        if ctx.geofencing_active:
            mobile_devices = coord_data.get("mobile_devices")
            if mobile_devices:
                device_list = [
                    {
                        "name": md.get("name", "Unknown"),
                        "location_enabled": (md.get("settings") or {}).get(
                            "geoTrackingEnabled",
                            True,
                        ),
                    }
                    for md in mobile_devices
                ]
                geo_insight = calculate_geofencing_device_offline_insight(devices=device_list)
                if geo_insight:
                    hub_insights.append(geo_insight)

        _collect_api_spike_insight(coord_data, hub_insights)

    except Exception as e:
        _LOGGER.debug(
            "Insight Collector: hub insight collection failed (%s), "
            "returning whatever was gathered so far",
            e,
        )

    return hub_insights


def _scan_zone_presence_data(
    zone_states: dict[str, Any], zone_name_map: dict[str, str],
) -> tuple[list[dict[str, Any]], bool, str | None, float | None, float | None]:
    """Scan zone states for presence insight data."""
    active_zones: list[dict[str, Any]] = []
    all_off = True
    coldest_name: str | None = None
    coldest_temp: float | None = None
    coldest_target: float | None = None

    for zid, zd in zone_states.items():
        s = zd.get("setting") or {}
        if s.get("type") == "HOT_WATER":
            continue
        z_name = zone_name_map.get(zid, f"Zone {zid}")
        if s.get("power") == "ON":
            all_off = False
            hp = (zd.get("activityDataPoints") or {}).get("heatingPower") or {}
            pct = hp.get("percentage", 0)
            if pct > 0:
                active_zones.append(
                    {"zone_name": z_name, "power_pct": pct, "zone_type": s.get("type") or ""},
                )
        sd = zd.get("sensorDataPoints") or {}
        t = (sd.get("insideTemperature") or {}).get("celsius")
        tgt = (s.get("temperature") or {}).get("celsius")
        if t is not None and (coldest_temp is None or t < coldest_temp):
            coldest_temp = t
            coldest_name = z_name
            coldest_target = tgt

    return active_zones, all_off, coldest_name, coldest_temp, coldest_target


def _collect_presence_insights(
    hass: HomeAssistant, coordinator: TadoDataUpdateCoordinator, hub_insights: list[Any],
) -> None:
    """Collect away+heating and home+all-off insights."""
    coord_data = coordinator.data or {}
    home_state_data = coord_data.get("home_state")
    if not home_state_data:
        return

    presence = home_state_data.get("presence")
    zones_info = coord_data.get("zones_info")
    zone_states = get_zone_states(coord_data)
    if not zone_states or not zones_info:
        return

    zone_name_map: dict[str, str] = {}
    for z in zones_info:
        zone_name_map[str(z.get("id"))] = z.get("name", f"Zone {z.get('id')}")

    active_zones, all_off, coldest_name, coldest_temp, coldest_target = (
        _scan_zone_presence_data(zone_states, zone_name_map)
    )

    away_insight = calculate_away_heating_active_insight(
        presence=presence,
        active_zones=active_zones or None,
    )
    if away_insight:
        hub_insights.append(away_insight)

    home_off_insight = calculate_home_all_off_insight(
        presence=presence,
        all_zones_off=all_off,
        coldest_zone_name=coldest_name,
        coldest_zone_temp=coldest_temp,
        coldest_zone_target=coldest_target,
    )
    if home_off_insight:
        hub_insights.append(home_off_insight)


def _collect_api_spike_insight(coord_data: dict[str, Any], hub_insights: list[Any]) -> None:
    """Collect API usage spike insight."""
    history_raw = coord_data.get("api_call_history")
    if not history_raw or not isinstance(history_raw, dict):
        return

    all_calls = [call for calls in history_raw.values() for call in calls]
    cph = calculate_calls_per_hour(all_calls) if all_calls else None

    now = dt_util.utcnow()
    today_key = now.strftime("%Y-%m-%d")
    today_calls = history_raw.get(today_key, [])
    current_hour_calls = 0
    for call in today_calls:
        ts = call.get("timestamp") or call.get("time", "")
        if ts:
            try:
                call_time = parse_iso_datetime(ts)
                if call_time.hour == now.hour:
                    current_hour_calls += 1
            except (ValueError, TypeError):
                pass

    if cph is not None and current_hour_calls > 0:
        spike_insight = calculate_api_usage_spike_insight(
            current_hour_calls=current_hour_calls,
            avg_calls_per_hour=cph,
        )
        if spike_insight:
            hub_insights.append(spike_insight)
