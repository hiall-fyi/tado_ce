"""Heating insights: anomaly, preheat, boiler flow, efficiency."""

from __future__ import annotations

from .calculations import classify_cold_risk_level
from .insights_models import (
    BOILER_FLOW_HIGH_DEMAND,
    BOILER_FLOW_HIGH_TEMP,
    BOILER_FLOW_LOW_DEMAND,
    BOILER_FLOW_LOW_TEMP,
    CONFIDENCE_ADEQUATE,
    CONFIDENCE_LOW,
    CONFIDENCE_MODERATE,
    HEATING_ANOMALY_MIN_MINUTES,
    HEATING_ANOMALY_POWER_THRESHOLD,
    HEATING_ANOMALY_TEMP_DELTA,
    HEATING_OFF_COLD_MIN_DEFICIT,
    HEATING_RATE_SLOW,
    PREHEAT_LONG_MINUTES,
    THERMAL_EFFICIENCY_MIN_CONFIDENCE,
    THERMAL_INERTIA_HIGH_MINUTES,
    Insight,
    InsightPriority,
)


def calculate_heating_anomaly_insight(
    heating_power_pct: float | None = None,
    temp_delta: float | None = None,
    duration_minutes: int = 0,
    zone_name: str = "",
) -> Insight | None:
    """Detect heating power anomaly (high power + low temp delta = blocked / air-locked)."""
    if heating_power_pct is None or temp_delta is None:
        return None
    if duration_minutes < HEATING_ANOMALY_MIN_MINUTES:
        return None
    if heating_power_pct < HEATING_ANOMALY_POWER_THRESHOLD or temp_delta >= HEATING_ANOMALY_TEMP_DELTA:
        return None

    hours = duration_minutes / 60
    rec = (
        f"{zone_name}: Heating at {heating_power_pct:.0f}% for {hours:.1f}h "
        f"but temp only changed {temp_delta:.1f}\u00b0C \u2014 "
        f"check TRV/radiator for blockage or air lock"
    )

    return Insight(
        priority=InsightPriority.HIGH,
        recommendation=rec,
        insight_type="heating_anomaly",
        zone_name=zone_name,
    )


def calculate_preheat_timing_insight(
    preheat_time_minutes: float | None = None,
    next_schedule_time: str | None = None,
    zone_name: str = "",
) -> Insight | None:
    """Calculate preheat timing insight (combines preheat_time + next schedule change)."""
    if preheat_time_minutes is None or next_schedule_time is None:
        return None
    if preheat_time_minutes <= 0:
        return None

    time_str = str(next_schedule_time)
    rec = (
        f"{zone_name}: Preheat takes ~{preheat_time_minutes:.0f} min. "
        f"Next schedule change at {time_str} \u2014 "
        f"start heating {preheat_time_minutes:.0f} min before."
    )

    priority = InsightPriority.LOW
    if preheat_time_minutes > PREHEAT_LONG_MINUTES:
        priority = InsightPriority.MEDIUM

    return Insight(
        priority=priority,
        recommendation=rec,
        insight_type="preheat_timing",
        zone_name=zone_name,
    )


def calculate_boiler_flow_anomaly_insight(
    flow_temp: float | None = None,
    heating_power_pct: float | None = None,
    zone_name: str = "",
    hot_water_on: bool = False,
) -> Insight | None:
    """Detect boiler flow temperature anomaly relative to heating demand."""
    if flow_temp is None or heating_power_pct is None:
        return None

    # A cylinder reheat runs the boiler hot with no zone calling for heat, and Tado doesn't
    # say whether the cylinder is actually calling, so "too hot" can't be told apart from it.
    if (
        flow_temp > BOILER_FLOW_HIGH_TEMP
        and heating_power_pct < BOILER_FLOW_LOW_DEMAND
        and not hot_water_on
    ):
        rec = (
            f"Boiler flow temp is {flow_temp:.0f}\u00b0C but heating demand "
            f"is only {heating_power_pct:.0f}% \u2014 flow temperature may be "
            f"set too high, consider lowering for efficiency"
        )
        return Insight(
            priority=InsightPriority.MEDIUM,
            recommendation=rec,
            insight_type="boiler_flow_anomaly",
            zone_name=zone_name or None,
        )

    if flow_temp < BOILER_FLOW_LOW_TEMP and heating_power_pct > BOILER_FLOW_HIGH_DEMAND:
        rec = (
            f"Boiler flow temp is only {flow_temp:.0f}\u00b0C but heating "
            f"demand is {heating_power_pct:.0f}% \u2014 boiler may not be "
            f"firing correctly, check boiler status"
        )
        return Insight(
            priority=InsightPriority.HIGH,
            recommendation=rec,
            insight_type="boiler_flow_anomaly",
            zone_name=zone_name or None,
        )

    return None


def calculate_poor_thermal_efficiency_insight(
    thermal_inertia: float | None = None,
    heating_rate: float | None = None,
    confidence_score: float | None = None,
    zone_name: str = "",
) -> Insight | None:
    """Detect poor thermal efficiency from Thermal Analytics data."""
    if confidence_score is not None and confidence_score < THERMAL_EFFICIENCY_MIN_CONFIDENCE:
        return None
    if thermal_inertia is None and heating_rate is None:
        return None

    issues = []
    if thermal_inertia is not None and thermal_inertia > THERMAL_INERTIA_HIGH_MINUTES:
        issues.append(f"thermal inertia is {thermal_inertia:.0f} min (high)")
    if heating_rate is not None and heating_rate < HEATING_RATE_SLOW:
        issues.append(f"heating rate is {heating_rate:.2f}\u00b0C/h (slow)")

    if not issues:
        return None

    issues_str = " and ".join(issues)
    rec = f"{zone_name}: {issues_str} \u2014 check insulation, radiator sizing, or TRV operation"

    return Insight(
        priority=InsightPriority.MEDIUM,
        recommendation=rec,
        insight_type="thermal_efficiency",
        zone_name=zone_name,
    )


def calculate_heating_off_cold_room_insight(
    power_state: str | None = None,
    current_temp: float | None = None,
    target_temp: float | None = None,
    zone_name: str = "",
    is_vulnerable_group: bool = False,
) -> tuple[str, str] | None:
    """Detect when heating is OFF but room has dropped significantly below target."""
    if power_state is None or power_state.upper() != "OFF":
        return None
    if current_temp is None or target_temp is None:
        return None

    deficit = target_temp - current_temp
    deficit_fires = deficit >= HEATING_OFF_COLD_MIN_DEFICIT

    # A vulnerable home also fires on absolute room temperature, regardless of
    # how far below target it is: a low setback target can still leave the room
    # cold enough to be a health risk for the occupant.
    risk = classify_cold_risk_level(current_temp) if is_vulnerable_group else "None"
    absolute_fires = risk in ("Respiratory", "Cardiovascular")

    if not (deficit_fires or absolute_fires):
        return None

    severity = "medium"
    if risk == "Cardiovascular":
        severity = "critical"
    elif risk == "Respiratory":
        severity = "high"

    if deficit_fires:
        rec = (
            f"{zone_name}: Heating is off and the room is {current_temp:.1f}\u00b0C, "
            f"{deficit:.1f}\u00b0C below the {target_temp:.0f}\u00b0C target. "
            f"Consider turning the heating back on."
        )
    else:
        rec = (
            f"{zone_name}: Heating is off and the room is {current_temp:.1f}\u00b0C, "
            f"cold enough to be a health concern for a vulnerable occupant. "
            f"Consider turning the heating on."
        )

    return (severity, rec)


def calculate_confidence_recommendation(
    confidence_percent: float | None,
    zone_name: str,
    cycle_count: int = 0,
    completed_count: int = 0,
) -> str:
    """Calculate SMART recommendation for thermal analysis confidence."""
    if confidence_percent is None:
        return ""

    if confidence_percent >= CONFIDENCE_ADEQUATE:
        return ""

    if confidence_percent < CONFIDENCE_LOW:
        needed = max(5 - completed_count, 1)
        return (
            f"{zone_name}: Low analysis confidence ({confidence_percent:.0f}%) "
            f"\u2014 need {needed} more complete heating cycles for reliable estimates"
        )

    if confidence_percent < CONFIDENCE_MODERATE:
        needed = max(3 - completed_count, 1)
        return (
            f"{zone_name}: Moderate confidence ({confidence_percent:.0f}%) "
            f"\u2014 {needed} more heating cycles will improve preheat accuracy"
        )

    # 50-70%
    return (
        f"{zone_name}: Building confidence ({confidence_percent:.0f}%) "
        f"\u2014 estimates improving with each heating cycle"
    )
