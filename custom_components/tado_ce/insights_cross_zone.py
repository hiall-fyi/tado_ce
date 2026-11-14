"""Cross-zone insights: mold aggregation, condensation, efficiency."""

from __future__ import annotations

from typing import Any

from .insights_models import (
    CROSS_ZONE_CONDENSATION_MIN_ZONES,
    CROSS_ZONE_EFFICIENCY_MIN_ZONES,
    CROSS_ZONE_MOLD_MIN_ZONES,
    HUMIDITY_IMBALANCE_MIN_EXCESS,
    TEMP_IMBALANCE_MIN_DIFF,
    Insight,
    InsightPriority,
)


def aggregate_cross_zone_mold_risk(
    zone_mold_risks: dict[str, str],
) -> Insight | None:
    """Aggregate mold risk across zones (whole-house humidity problem)."""
    if not zone_mold_risks:
        return None

    affected = [name for name, level in zone_mold_risks.items() if level in ("Medium", "High", "Critical")]

    if len(affected) < CROSS_ZONE_MOLD_MIN_ZONES:
        return None

    zones_str = ", ".join(affected[:5])
    rec = (
        f"Whole-house mold risk: {len(affected)} zones affected "
        f"({zones_str}) \u2014 consider whole-house dehumidifier or "
        f"check ventilation system"
    )

    has_critical = any(zone_mold_risks[z] == "Critical" for z in affected)
    priority = InsightPriority.CRITICAL if has_critical else InsightPriority.HIGH

    return Insight(
        priority=priority,
        recommendation=rec,
        insight_type="cross_zone_mold",
        zone_name=None,
    )


def aggregate_cross_zone_condensation(
    zone_condensation_states: dict[str, Any],
) -> Insight | None:
    """Aggregate condensation risk across zones (whole-house ventilation issue)."""
    if not zone_condensation_states:
        return None

    affected = [
        name
        for name, level in zone_condensation_states.items()
        if level not in ("unavailable", "unknown", "None", "Low", None)
    ]

    if len(affected) < CROSS_ZONE_CONDENSATION_MIN_ZONES:
        return None

    zones_str = ", ".join(affected[:5])
    rec = (
        f"Whole-house condensation risk: {len(affected)} zones affected "
        f"({zones_str}) \u2014 check ventilation system and consider "
        f"using a dehumidifier"
    )

    return Insight(
        priority=InsightPriority.HIGH,
        recommendation=rec,
        insight_type="cross_zone_condensation",
        zone_name=None,
    )


def calculate_cross_zone_efficiency_insight(
    zone_heating_rates: dict[str, Any],
) -> Insight | None:
    """Compare heating efficiency across zones (slowest vs average)."""
    if not zone_heating_rates or len(zone_heating_rates) < CROSS_ZONE_EFFICIENCY_MIN_ZONES:
        return None

    rates = list(zone_heating_rates.values())
    avg_rate = sum(rates) / len(rates)
    if avg_rate <= 0:
        return None

    slowest_zone = min(zone_heating_rates, key=zone_heating_rates.get)  # type: ignore[arg-type]
    slowest_rate = zone_heating_rates[slowest_zone]

    # Trigger if slowest is less than half the average
    if slowest_rate >= avg_rate * 0.5:
        return None

    rec = (
        f"{slowest_zone} heats at {slowest_rate:.2f}\u00b0C/h "
        f"(avg across zones: {avg_rate:.2f}\u00b0C/h) \u2014 "
        f"investigate insulation or radiator issues in this zone"
    )

    return Insight(
        priority=InsightPriority.LOW,
        recommendation=rec,
        insight_type="cross_zone_efficiency",
        zone_name=None,
    )


def calculate_temperature_imbalance_insight(
    zone_temperatures: dict[str, Any],
) -> Insight | None:
    """Detect large temperature differences between active zones."""
    if not zone_temperatures or len(zone_temperatures) < CROSS_ZONE_EFFICIENCY_MIN_ZONES:
        return None

    warmest_zone = max(zone_temperatures, key=zone_temperatures.get)  # type: ignore[arg-type]
    coldest_zone = min(zone_temperatures, key=zone_temperatures.get)  # type: ignore[arg-type]
    warmest_temp = zone_temperatures[warmest_zone]
    coldest_temp = zone_temperatures[coldest_zone]

    diff = warmest_temp - coldest_temp
    if diff < TEMP_IMBALANCE_MIN_DIFF:
        return None

    rec = (
        f"Temperature imbalance: {warmest_zone} is {warmest_temp:.1f}\u00b0C "
        f"but {coldest_zone} is {coldest_temp:.1f}\u00b0C "
        f"({diff:.1f}\u00b0C difference) \u2014 check heat distribution"
    )

    return Insight(
        priority=InsightPriority.LOW,
        recommendation=rec,
        insight_type="temp_imbalance",
        zone_name=None,
    )


def calculate_humidity_imbalance_insight(
    zone_humidities: dict[str, Any],
) -> Insight | None:
    """Detect when one zone has significantly higher humidity than others."""
    if not zone_humidities or len(zone_humidities) < CROSS_ZONE_EFFICIENCY_MIN_ZONES:
        return None

    values = list(zone_humidities.values())
    avg_humidity = sum(values) / len(values)

    most_humid_zone = max(zone_humidities, key=zone_humidities.get)  # type: ignore[arg-type]
    most_humid_val = zone_humidities[most_humid_zone]

    excess = most_humid_val - avg_humidity
    if excess < HUMIDITY_IMBALANCE_MIN_EXCESS:
        return None

    rec = (
        f"{most_humid_zone} humidity is {most_humid_val:.0f}% "
        f"({excess:.0f}% above average of {avg_humidity:.0f}%) "
        f"\u2014 check ventilation in this zone"
    )

    return Insight(
        priority=InsightPriority.MEDIUM,
        recommendation=rec,
        insight_type="humidity_imbalance",
        zone_name=None,
    )
