"""Miscellaneous insights: presence, schedule, weather."""

from __future__ import annotations

from typing import Any

from .insights_models import (
    FROST_RISK_TEMP,
    HOME_COLD_MIN_DEFICIT,
    SCHEDULE_GAP_MIN_DEFICIT,
    SCHEDULE_GAP_MIN_OFF_HOURS,
    VENTILATION_OPPORTUNITY_MIN_RISE,
    VENTILATION_OPPORTUNITY_MIN_SAMPLES,
    WEATHER_COLD_SNAP_DELTA,
    WEATHER_SEVERE_COLD_SNAP_DELTA,
    Insight,
    InsightPriority,
)

# ── Presence insights ────────────────────────────────────────────────


def calculate_away_heating_active_insight(
    presence: str | None = None,
    active_zones: list[Any] | None = None,
) -> Insight | None:
    """Detect energy waste: home is AWAY but zones still heating/cooling."""
    if presence is None or presence.upper() != "AWAY":
        return None
    if not active_zones:
        return None

    zone_descs = []
    for z in active_zones[:5]:
        name = z.get("zone_name", "Unknown")
        pct = z.get("power_pct", 0)
        zone_descs.append(f"{name} ({pct:.0f}%)")

    zones_str = ", ".join(zone_descs)
    rec = f"Home is AWAY but {len(active_zones)} zone(s) still active: {zones_str} \u2014 check if this is intentional"

    return Insight(
        priority=InsightPriority.HIGH,
        recommendation=rec,
        insight_type="away_heating",
        zone_name=None,
    )


def calculate_home_all_off_insight(
    presence: str | None = None,
    all_zones_off: bool = True,
    coldest_zone_name: str | None = None,
    coldest_zone_temp: float | None = None,
    coldest_zone_target: float | None = None,
) -> Insight | None:
    """Detect when someone is home but all heating is off and rooms are cold."""
    if presence is None or presence.upper() != "HOME":
        return None
    if not all_zones_off:
        return None
    if coldest_zone_temp is None or coldest_zone_target is None:
        return None

    deficit = coldest_zone_target - coldest_zone_temp
    if deficit < HOME_COLD_MIN_DEFICIT:
        return None

    rec = (
        f"Someone is home but all heating is off. "
        f"{coldest_zone_name}: {coldest_zone_temp:.1f}\u00b0C "
        f"({deficit:.1f}\u00b0C below target {coldest_zone_target:.0f}\u00b0C)"
    )

    return Insight(
        priority=InsightPriority.MEDIUM,
        recommendation=rec,
        insight_type="home_all_off",
        zone_name=None,
    )


# ── Schedule insights ────────────────────────────────────────────────


def calculate_schedule_gap_insight(
    schedule_blocks: list[Any] | None = None,
    current_temp: float | None = None,
    next_target_temp: float | None = None,
    longest_off_hours: float | None = None,
    zone_name: str = "",
) -> Insight | None:
    """Detect long OFF gaps in schedule while room is cold."""
    if schedule_blocks is None or current_temp is None:
        return None
    if next_target_temp is None or longest_off_hours is None:
        return None
    if longest_off_hours < SCHEDULE_GAP_MIN_OFF_HOURS:
        return None

    temp_deficit = next_target_temp - current_temp
    if temp_deficit < SCHEDULE_GAP_MIN_DEFICIT:
        return None

    rec = (
        f"{zone_name}: Schedule has a {longest_off_hours:.0f}h OFF gap and "
        f"room is {current_temp:.1f}\u00b0C ({temp_deficit:.1f}\u00b0C below "
        f"next target {next_target_temp:.0f}\u00b0C) \u2014 consider adding a "
        f"setback temperature to prevent deep cooling"
    )

    return Insight(
        priority=InsightPriority.MEDIUM,
        recommendation=rec,
        insight_type="schedule_gap",
        zone_name=zone_name,
    )


# ── Weather insights ─────────────────────────────────────────────────


def calculate_weather_impact_insight(
    current_outdoor_temp: float | None = None,
    avg_outdoor_temp_7d: float | None = None,
    zone_name: str = "",
) -> Insight | None:
    """Calculate weather impact insight (cold-snap heating demand)."""
    if current_outdoor_temp is None or avg_outdoor_temp_7d is None:
        return None

    diff = round(avg_outdoor_temp_7d - current_outdoor_temp, 1)
    if diff <= WEATHER_COLD_SNAP_DELTA:
        return None

    # Rough estimate: each 1C drop increases heating by ~3-5%
    impact_pct = round(diff * 4)  # ~4% per degree

    rec = (
        f"Cold snap: {current_outdoor_temp:.0f}\u00b0C outdoor, "
        f"{diff:.0f}\u00b0C below 7-day average. "
        f"Estimated {impact_pct}% increase in heating demand"
    )

    priority = InsightPriority.LOW
    if diff > WEATHER_SEVERE_COLD_SNAP_DELTA:
        priority = InsightPriority.MEDIUM

    return Insight(
        priority=priority,
        recommendation=rec,
        insight_type="weather_impact",
        zone_name=zone_name or None,
    )


def calculate_frost_risk_insight(
    outdoor_temp: float | None = None,
) -> Insight | None:
    """Warn about frost/pipe freezing risk when outdoor temp near freezing."""
    if outdoor_temp is None:
        return None
    if outdoor_temp > FROST_RISK_TEMP:
        return None

    if outdoor_temp <= 0:
        rec = (
            f"Outdoor temperature is {outdoor_temp:.1f}\u00b0C (below freezing) "
            f"\u2014 ensure heating is not fully off to prevent pipe freezing"
        )
        priority = InsightPriority.HIGH
    else:
        rec = (
            f"Outdoor temperature is {outdoor_temp:.1f}\u00b0C (approaching "
            f"freezing) \u2014 monitor heating to prevent pipe freezing risk"
        )
        priority = InsightPriority.MEDIUM

    return Insight(
        priority=priority,
        recommendation=rec,
        insight_type="frost_risk",
        zone_name=None,
    )


def calculate_ventilation_opportunity_insight(
    current_differential: float | None = None,
    differential_history: list[Any] | None = None,
    zone_name: str = "",
) -> Insight | None:
    """Detect when the wettest zone's air is unusually wetter than outside, vs this home's own recent norm."""
    if current_differential is None or not differential_history:
        return None
    if len(differential_history) < VENTILATION_OPPORTUNITY_MIN_SAMPLES:
        return None

    avg_history = sum(differential_history) / len(differential_history)
    rise = current_differential - avg_history
    if rise < VENTILATION_OPPORTUNITY_MIN_RISE:
        return None

    rec = (
        f"{zone_name}: air is {current_differential:.1f} g/m³ more humid than "
        f"outside, well above this home's recent average gap of {avg_history:.1f} "
        f"— open a window to dry it out efficiently"
    )

    return Insight(
        priority=InsightPriority.LOW,
        recommendation=rec,
        insight_type="ventilation_opportunity",
        zone_name=None,
    )
