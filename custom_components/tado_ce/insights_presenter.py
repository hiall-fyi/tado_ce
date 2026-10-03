"""Tado CE Insights Presenter: correlation, aggregation, and formatting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import TYPE_CHECKING, Any

from .const import INSIGHT_ESCALATION_DAYS
from .helpers import parse_iso_datetime as _parse_iso_dt
from .insights_models import (
    _INSIGHT_TO_GROUP,
    ESCALATION_RULES,
    SUMMARY_MAX_LENGTH,
    TEMP_DEVIATION_MIN_SAMPLES,
    TEMP_DEVIATION_NORMAL,
    TEMP_DEVIATION_SIGNIFICANT,
    Insight,
    InsightPriority,
)

if TYPE_CHECKING:
    from .insight_history import InsightHistoryTracker


# ============================================================================
# Causal analysis templates for correlation groups
# ============================================================================

CAUSAL_TEMPLATES: dict[str, dict[str, str]] = {
    "humidity_problem": {
        "chain": "Sustained high humidity \u2192 mold risk rising \u2192 condensation risk",
        "mold_risk": "Mold risk detected",
        "humidity_trend": "Humidity trending upward",
        "condensation": "Condensation forming",
        "cross_zone_condensation": "Condensation across multiple zones",
    },
    "heating_efficiency_issue": {
        "chain": "Heating anomaly \u2192 efficiency declining \u2192 boiler flow abnormal",
        "heating_anomaly": "Heating not responding normally",
        "thermal_efficiency": "Poor thermal efficiency",
        "boiler_flow_anomaly": "Boiler flow temperature abnormal",
    },
    "schedule_review": {
        "chain": "Schedule gaps \u2192 deviating from schedule",
        "schedule_gap": "Gaps in heating schedule",
        "schedule_deviation": "Deviating from schedule",
    },
    "device_maintenance": {
        "chain": "Low battery \u2192 unstable connection",
        "battery": "Battery level low",
        "connection": "Connection unstable",
    },
}


# ============================================================================
# Category grouping map for actions_needed
# ============================================================================

CATEGORY_MAP: dict[str, tuple[str, list[str]]] = {
    "environment": ("\U0001f321\ufe0f Environment", [
        "mold_risk", "condensation", "comfort",
        "humidity_trend", "humidity_imbalance", "humidity_problem",
    ]),
    "heating": ("\U0001f525 Heating", [
        "heating_anomaly", "thermal_efficiency", "boiler_flow_anomaly",
        "heating_off_cold", "preheat_timing", "heating_efficiency_issue",
    ]),
    "schedule": ("\U0001f4c5 Schedule", [
        "schedule_gap", "schedule_deviation", "schedule_review",
    ]),
    "weather": ("\U0001f324\ufe0f Weather", [
        "frost_risk", "weather_impact", "ventilation_opportunity",
    ]),
    "device": ("\U0001f50b Device", ["battery", "connection", "device_maintenance"]),
    "presence": ("\U0001f3e0 Presence", [
        "away_heating", "home_all_off", "geofencing_offline",
    ]),
    "window": ("\U0001fa9f Window", [
        "window_predicted", "cross_zone_window", "cross_zone_mold",
    ]),
    "system": ("\u2699\ufe0f System", [
        "api_quota_planning", "api_usage_spike", "cross_zone_condensation",
    ]),
}

# Reverse lookup: insight_type → (category_key, display_label)
_INSIGHT_TO_CATEGORY: dict[str, tuple[str, str]] = {}
for _cat_key, (_cat_label, _cat_types) in CATEGORY_MAP.items():
    for _itype in _cat_types:
        _INSIGHT_TO_CATEGORY[_itype] = (_cat_key, _cat_label)


# ============================================================================
# Duration extraction from recommendation text
# ============================================================================

_DURATION_RE = re.compile(
    r"(?:"
    r"(\d+)\s*days?\s*(?:since first reported|overdue)"
    r"|reported\s+(\d+)\s*days?\s*ago"
    r"|(?:ongoing|offline|persisting)\s+for\s+(\d+)\s*days?"
    r"|(\d+)\s*days?\s*and\s+worsening"
    r"|\((\d+)\s*days?\)"
    r")",
)


# ============================================================================
# Health score deductions per priority level
# ============================================================================

_HEALTH_DEDUCTIONS: dict[InsightPriority, int] = {
    InsightPriority.CRITICAL: 25,
    InsightPriority.HIGH: 15,
    InsightPriority.MEDIUM: 8,
    InsightPriority.LOW: 3,
}


# ============================================================================
# Presentation functions
# ============================================================================


def get_insight_priority(insight_type: str, severity: str) -> InsightPriority:
    """Get priority level for an insight based on type and severity."""
    priority_map = {
        ("window_predicted", "high"): InsightPriority.HIGH,
        ("window_predicted", "medium"): InsightPriority.MEDIUM,
        ("window_predicted", "low"): InsightPriority.LOW,
        ("mold_risk", "critical"): InsightPriority.CRITICAL,
        ("mold_risk", "high"): InsightPriority.HIGH,
        ("mold_risk", "medium"): InsightPriority.MEDIUM,
        ("condensation", "critical"): InsightPriority.CRITICAL,
        ("condensation", "high"): InsightPriority.HIGH,
        ("condensation", "medium"): InsightPriority.MEDIUM,
        ("connection", "offline"): InsightPriority.HIGH,
        ("connection", "offline_long"): InsightPriority.CRITICAL,
        ("battery", "critical"): InsightPriority.CRITICAL,
        ("battery", "low"): InsightPriority.HIGH,
        ("comfort", "too_cold"): InsightPriority.MEDIUM,
        ("comfort", "too_hot"): InsightPriority.MEDIUM,
        ("comfort", "danger"): InsightPriority.HIGH,
        ("comfort", "extreme_danger"): InsightPriority.CRITICAL,
        ("heating_off_cold", "medium"): InsightPriority.MEDIUM,
        ("heating_off_cold", "high"): InsightPriority.HIGH,
        ("heating_off_cold", "critical"): InsightPriority.CRITICAL,
    }
    return priority_map.get(
        (insight_type, severity.lower()), InsightPriority.NONE,
    )


def _get_action_label(insight_type: str) -> str:
    """Map insight_type to a user-friendly action label for grouping."""
    action_map = {
        "battery": "Replace batteries",
        "connection": "Check device connection",
        "mold_risk": "Improve ventilation (mold risk)",
        "condensation": "Reduce condensation risk",
        "comfort": "Review comfort settings",
        "humidity_trend": "Monitor humidity trend",
        "humidity_imbalance": "Balance humidity across zones",
        "thermal_efficiency": "Check heating efficiency",
        "heating_anomaly": "Investigate heating anomaly",
        "heating_off_cold": "Turn on heating (zone too cold)",
        "boiler_flow_anomaly": "Check boiler flow temperature",
        "cross_zone_efficiency": "Improve cross-zone efficiency",
        "cross_zone_condensation": "Address cross-zone condensation",
        "schedule_deviation": "Review schedule deviation",
        "schedule_gap": "Fill schedule gaps",
        "preheat_timing": "Adjust preheat timing",
        "weather_impact": "Weather affecting heating",
        "frost_risk": "Frost protection needed",
        "ventilation_opportunity": "Open a window to dry the air",
        "temp_imbalance": "Balance temperatures across zones",
        "window_predicted": "Close window (heat loss detected)",
        "cross_zone_window": "Multiple windows open",
        "cross_zone_mold": "Mold risk across multiple zones",
        "away_heating": "Heating active while away",
        "home_all_off": "All zones off while home",
        "api_quota_planning": "Review API quota usage",
        "api_usage_spike": "API usage spike detected",
        "geofencing_offline": "Check geofencing status",
        "humidity_problem": "Address humidity problem",
        "heating_efficiency_issue": "Investigate heating efficiency",
        "schedule_review": "Review schedule settings",
        "device_maintenance": "Check device health",
    }
    return action_map.get(insight_type, insight_type.replace("_", " ").title())


def escalate_priorities(
    insights: list[Insight],
    history: InsightHistoryTracker,
    now: datetime,
) -> list[Insight]:
    """Return new list with escalated priorities based on persistence duration.

    Pure function: does not mutate input insights or history.
    Insights not in ESCALATION_RULES keep their original priority.
    Escalated priority is always >= base priority (monotonic).
    Escalated priority is capped at CRITICAL (4).
    """
    result: list[Insight] = []
    for insight in insights:
        rules = ESCALATION_RULES.get(insight.insight_type)
        if not rules:
            result.append(insight)
            continue

        dur = history.get_duration(insight.insight_type, insight.zone_name)
        if dur is None:
            result.append(insight)
            continue

        days = dur.total_seconds() / 86400
        escalated_priority = insight.priority
        for threshold_days, new_priority in rules:
            if days >= threshold_days and new_priority > escalated_priority:
                escalated_priority = new_priority

        escalated_priority = min(escalated_priority, InsightPriority.CRITICAL)

        if escalated_priority != insight.priority:
            result.append(
                Insight(
                    priority=escalated_priority,
                    recommendation=insight.recommendation,
                    insight_type=insight.insight_type,
                    zone_name=insight.zone_name,
                ),
            )
        else:
            result.append(insight)
    return result


def _build_causal_recommendation(
    zone_name: str,
    grp_key: str,
    members: list[Insight],
) -> str:
    """Build a causal-analysis recommendation for correlated insights."""
    template = CAUSAL_TEMPLATES.get(grp_key)
    if template is None:
        # Fallback: join action labels (legacy behaviour)
        type_labels = [_get_action_label(m.insight_type) for m in members]
        return (
            f"{zone_name}: {grp_key.replace('_', ' ').title()} "
            f"\u2014 {' + '.join(type_labels)}"
        )

    present_types = [m.insight_type for m in members]
    descriptions = [
        template[t] for t in present_types if t in template
    ]
    chain = template.get("chain", "")

    if descriptions:
        detail = "; ".join(descriptions)
        return f"{zone_name}: {chain} \u2014 {detail}"

    return (
        f"{zone_name}: {grp_key.replace('_', ' ').title()} \u2014 {chain}"
    )


def correlate_insights(
    zone_insights: dict[str, list[Insight]],
) -> dict[str, list[Insight]]:
    """Correlate related insights within each zone for home-level aggregation (returns new dict)."""
    result: dict[str, list[Insight]] = {}

    for zone_name, insights in zone_insights.items():
        if zone_name.startswith("_"):
            result[zone_name] = list(insights)
            continue

        groups: dict[str, list[Insight]] = {}
        ungrouped: list[Insight] = []

        for insight in insights:
            grp = _INSIGHT_TO_GROUP.get(insight.insight_type)
            if grp is not None:
                groups.setdefault(grp, []).append(insight)
            else:
                ungrouped.append(insight)

        merged: list[Insight] = list(ungrouped)

        for grp_key, members in groups.items():
            if len(members) == 1:
                merged.append(members[0])
            else:
                max_pri = max(m.priority for m in members)
                combined_rec = _build_causal_recommendation(
                    zone_name, grp_key, members,
                )
                merged.append(
                    Insight(
                        priority=max_pri,
                        recommendation=combined_rec,
                        insight_type=grp_key,
                        zone_name=zone_name,
                    ),
                )

        result[zone_name] = merged

    return result


def calculate_insight_health_score(insights: list[Insight]) -> int:
    """Calculate home health score (0\u2013100, higher = healthier).

    Starts at 100 and deducts per insight based on priority.
    Floor at 0. Score of 100 = no active insights.
    """
    score = 100
    for insight in insights:
        score -= _HEALTH_DEDUCTIONS.get(insight.priority, 0)
    return max(score, 0)



def _narrative_zone_str(zones: list[str]) -> str:
    """Format zone names with natural language joining."""
    if not zones:
        return ""
    if len(zones) == 1:
        return zones[0]
    if len(zones) == 2:
        return f"{zones[0]} and {zones[1]}"
    return f"{', '.join(zones[:-1])}, and {zones[-1]}"


def _narrative_urgency_reason(label: str, grp: dict[str, Any]) -> str:
    """Build an urgency reason clause based on insight type and duration."""
    insight_type = _reverse_action_label(label)
    dur = grp.get("max_duration_days", 0)

    if insight_type == "battery":
        if dur >= INSIGHT_ESCALATION_DAYS:
            return f"first reported {dur} days ago"
        if dur >= 2:
            return f"reported {dur} days ago"
        return "to avoid losing control"

    if insight_type == "mold_risk":
        if dur >= 7:
            return f"ongoing for {dur} days"
        if dur >= 2:
            return f"reported {dur} days ago"
        return "needs attention"

    # General fallback
    if dur >= 2:
        return f"reported {dur} days ago"
    return ""


def _narrative_build_primary(label: str, grp: dict[str, Any]) -> str:
    """Build the primary narrative sentence for the top action."""
    zones = grp["zones"]
    zs = _narrative_zone_str(zones)
    reason = _narrative_urgency_reason(label, grp)

    core = f"{label} in {zs}" if zs else label
    if reason:
        return f"{core} \u2014 {reason}."
    return f"{core}."


def _narrative_build_secondary(label: str, grp: dict[str, Any]) -> str:
    """Build a brief secondary mention for one extra action."""
    zones = grp["zones"]
    zs = _narrative_zone_str(zones)
    insight_type = _reverse_action_label(label)
    verb = "are" if len(zones) > 1 else "is"

    # Use short phrasing depending on type
    if insight_type == "comfort" and zs:
        return f"{zs} {verb} also running cold."
    if insight_type in ("mold_risk", "condensation") and zs:
        have = "have" if len(zones) > 1 else "has"
        return f"{zs} also {have} {insight_type.replace('_', ' ')} concerns."
    if zs:
        return f"{zs} also needs attention." if len(zones) == 1 else f"{zs} also need attention."
    return f"{label} also needs attention."


def _build_narrative_summary(
    sorted_actions: list[tuple[str, dict[str, Any]]],
) -> str:
    """Build a narrative summary focusing on the single most urgent action."""
    if not sorted_actions:
        return "All zones are running well \u2014 no issues detected."

    top_label, top_grp = sorted_actions[0]
    primary = _narrative_build_primary(top_label, top_grp)

    if len(sorted_actions) < 2:
        result = primary
    else:
        sec_label, sec_grp = sorted_actions[1]
        secondary = f" {_narrative_build_secondary(sec_label, sec_grp)}"
        candidate = f"{primary}{secondary}"
        result = candidate if len(candidate) <= SUMMARY_MAX_LENGTH else primary

    # Final length guard
    if len(result) > SUMMARY_MAX_LENGTH:
        result = result[: SUMMARY_MAX_LENGTH - 1] + "\u2026"

    return result




def build_flat_action_list(
    action_groups: dict[str, dict[str, Any]],
) -> list[str]:
    """Build narrative action list sorted by priority."""
    if not action_groups:
        return []

    items: list[tuple[str, int]] = []
    for label, grp in action_groups.items():
        zones = grp["zones"]
        dur = grp.get("max_duration_days", 0)

        # Consistent duration phrasing per insight type
        dur_suffix = ""
        if dur >= 2:
            insight_type = _reverse_action_label(label)
            if insight_type == "battery" and dur >= INSIGHT_ESCALATION_DAYS:
                dur_suffix = f" (overdue \u2014 {dur} days)"
            elif insight_type == "battery":
                dur_suffix = f" ({dur} days)"
            else:
                dur_suffix = f" (ongoing \u2014 {dur} days)"

        if zones:
            if len(zones) == 1:
                zone_str = zones[0]
            elif len(zones) == 2:
                zone_str = f"{zones[0]} and {zones[1]}"
            else:
                zone_str = f"{', '.join(zones[:-1])}, and {zones[-1]}"
            action_str = f"{label} in {zone_str}{dur_suffix}"
        else:
            action_str = f"{label}{dur_suffix}"
        items.append((action_str, grp["priority"]))

    items.sort(key=lambda x: (-x[1], x[0]))
    return [s.rstrip(".") for s, _ in items]



def _reverse_action_label(label: str) -> str:
    """Reverse-lookup an action label to its insight_type."""
    _label_to_type = {
        "Replace batteries": "battery",
        "Check device connection": "connection",
        "Improve ventilation (mold risk)": "mold_risk",
        "Reduce condensation risk": "condensation",
        "Review comfort settings": "comfort",
        "Monitor humidity trend": "humidity_trend",
        "Balance humidity across zones": "humidity_imbalance",
        "Check heating efficiency": "thermal_efficiency",
        "Investigate heating anomaly": "heating_anomaly",
        "Turn on heating (zone too cold)": "heating_off_cold",
        "Check boiler flow temperature": "boiler_flow_anomaly",
        "Improve cross-zone efficiency": "cross_zone_efficiency",
        "Address cross-zone condensation": "cross_zone_condensation",
        "Review schedule deviation": "schedule_deviation",
        "Fill schedule gaps": "schedule_gap",
        "Adjust preheat timing": "preheat_timing",
        "Weather affecting heating": "weather_impact",
        "Frost protection needed": "frost_risk",
        "Open a window to dry the air": "ventilation_opportunity",
        "Balance temperatures across zones": "temp_imbalance",
        "Close window (heat loss detected)": "window_predicted",
        "Multiple windows open": "cross_zone_window",
        "Mold risk across multiple zones": "cross_zone_mold",
        "Heating active while away": "away_heating",
        "All zones off while home": "home_all_off",
        "Review API quota usage": "api_quota_planning",
        "API usage spike detected": "api_usage_spike",
        "Check geofencing status": "geofencing_offline",
        "Address humidity problem": "humidity_problem",
        "Investigate heating efficiency": "heating_efficiency_issue",
        "Review schedule settings": "schedule_review",
        "Check device health": "device_maintenance",
    }
    return _label_to_type.get(label, label.lower().replace(" ", "_"))



def _extract_max_duration_days(recommendation: str) -> int:
    """Extract the maximum duration in days from a recommendation string."""
    dur_days = 0
    for m in _DURATION_RE.finditer(recommendation):
        for g in m.groups():
            if g is not None:
                try:
                    dur_days = max(dur_days, int(g))
                except ValueError:
                    pass
                break
    return dur_days


def _collect_insights_and_zones(
    zone_insights: dict[str, list[Insight]],
) -> tuple[list[Insight], set[str]]:
    """Collect all insights and zone names from zone_insights dict."""
    all_insights: list[Insight] = []
    all_zone_names: set[str] = set()
    for zone_name, insights in zone_insights.items():
        if zone_name.startswith("_"):
            all_insights.extend(insights)
            continue
        all_zone_names.add(zone_name)
        if insights:
            all_insights.extend(insights)
    return all_insights, all_zone_names


def _group_insights_by_action(
    all_insights: list[Insight],
) -> dict[str, dict[str, Any]]:
    """Group insights by action label, tracking zones, priority, and duration."""
    action_groups: dict[str, dict[str, Any]] = {}
    for insight in all_insights:
        label = _get_action_label(insight.insight_type)
        if label not in action_groups:
            action_groups[label] = {"zones": [], "priority": insight.priority, "max_duration_days": 0}
        grp = action_groups[label]
        if insight.zone_name and insight.zone_name not in grp["zones"]:
            grp["zones"].append(insight.zone_name)
        grp["priority"] = max(grp["priority"], insight.priority)
        dur = _extract_max_duration_days(insight.recommendation or "")
        if dur > 0:
            grp["max_duration_days"] = max(grp["max_duration_days"], dur)
    return action_groups


def aggregate_home_insights(
    zone_insights: dict[str, list[Insight]],
) -> dict[str, Any]:
    """Aggregate insights from all zones into action-based home summary."""
    empty_result: dict[str, Any] = {
        "total_insights": 0,
        "top_priority": "none",
        "summary": "All zones are running well \u2014 no issues detected.",
        "actions_needed": [],
    }
    if not zone_insights:
        return empty_result

    all_insights, _all_zone_names = _collect_insights_and_zones(zone_insights)
    if not all_insights:
        return empty_result

    action_groups = _group_insights_by_action(all_insights)

    sorted_actions = sorted(
        action_groups.items(),
        key=lambda x: (-x[1]["priority"], x[0]),
    )

    actions_needed = build_flat_action_list(action_groups)

    top_insight = max(all_insights, key=lambda i: i.priority)
    top_priority = top_insight.priority.name.lower()
    summary = _build_narrative_summary(sorted_actions)

    return {
        "total_insights": len(all_insights),
        "top_priority": top_priority,
        "summary": summary,
        "actions_needed": actions_needed,
    }



def _deviation_warmer_msg(
    zone_name: str,
    abs_dev: float,
    current_temp: float | None,
    historical_avg: float | None,
) -> str:
    """Build recommendation for significantly warmer deviation."""
    if current_temp is not None and historical_avg is not None:
        return (
            f"{zone_name}: {abs_dev:.1f}\u00b0C warmer than usual "
            f"({current_temp:.1f}\u00b0C vs avg {historical_avg:.1f}\u00b0C) "
            f"\u2014 check if heating schedule needs adjustment"
        )
    return (
        f"{zone_name}: {abs_dev:.1f}\u00b0C warmer than usual "
        f"\u2014 review heating schedule"
    )


def _deviation_above_msg(zone_name: str, abs_dev: float, current_temp: float | None) -> str:
    """Build recommendation for moderately above-average deviation."""
    if current_temp is not None:
        return (
            f"{zone_name}: {abs_dev:.1f}\u00b0C above average "
            f"({current_temp:.1f}\u00b0C) \u2014 monitor for pattern"
        )
    return f"{zone_name}: {abs_dev:.1f}\u00b0C above average \u2014 monitor for pattern"


def _deviation_colder_msg(
    zone_name: str,
    abs_dev: float,
    current_temp: float | None,
    historical_avg: float | None,
) -> str:
    """Build recommendation for significantly colder deviation."""
    if current_temp is not None and historical_avg is not None:
        return (
            f"{zone_name}: {abs_dev:.1f}\u00b0C colder than usual "
            f"({current_temp:.1f}\u00b0C vs avg {historical_avg:.1f}\u00b0C) "
            f"\u2014 check windows and heating system"
        )
    return (
        f"{zone_name}: {abs_dev:.1f}\u00b0C colder than usual "
        f"\u2014 check windows and heating"
    )


def _deviation_below_msg(zone_name: str, abs_dev: float, current_temp: float | None) -> str:
    """Build recommendation for moderately below-average deviation."""
    if current_temp is not None:
        return (
            f"{zone_name}: {abs_dev:.1f}\u00b0C below average "
            f"({current_temp:.1f}\u00b0C) \u2014 check for drafts or open windows"
        )
    return f"{zone_name}: {abs_dev:.1f}\u00b0C below average \u2014 check for drafts"


def calculate_historical_deviation_recommendation(
    deviation: float | None,
    zone_name: str,
    current_temp: float | None = None,
    historical_avg: float | None = None,
    sample_count: int = 0,
) -> str:
    """Calculate SMART recommendation for historical temperature deviation."""
    if deviation is None or sample_count < TEMP_DEVIATION_MIN_SAMPLES:
        return ""

    abs_deviation = abs(deviation)

    if abs_deviation <= TEMP_DEVIATION_NORMAL:
        return ""

    if deviation > TEMP_DEVIATION_SIGNIFICANT:
        return _deviation_warmer_msg(zone_name, abs_deviation, current_temp, historical_avg)

    if deviation > TEMP_DEVIATION_NORMAL:
        return _deviation_above_msg(zone_name, abs_deviation, current_temp)

    if deviation < -TEMP_DEVIATION_SIGNIFICANT:
        return _deviation_colder_msg(zone_name, abs_deviation, current_temp, historical_avg)

    if deviation < -TEMP_DEVIATION_NORMAL:
        return _deviation_below_msg(zone_name, abs_deviation, current_temp)

    return ""


# ============================================================================
# Weekly digest
# ============================================================================


@dataclass
class _WeeklyStats:
    """Intermediate stats collected from insight history entries."""

    active_count: int
    resolved_count: int
    type_counts: dict[str, int]
    zone_counts: dict[str, int]
    longest: tuple[str, str | None, float] | None  # (type, zone, days)
    active_keys: set[str]  # set of "type:zone" keys active this period


def _collect_weekly_stats(
    entries: dict[str, dict[str, Any]],
    week_start: datetime,
    now: datetime,
) -> _WeeklyStats:
    """Collect weekly insight statistics from history entries."""
    type_counts: dict[str, int] = {}
    zone_counts: dict[str, int] = {}
    longest: tuple[str, str | None, float] | None = None
    longest_days: float = 0.0
    active_count = 0
    resolved_count = 0
    active_keys: set[str] = set()

    for key, entry in entries.items():
        try:
            first_seen = _parse_iso_dt(entry["first_seen"])
            last_seen = _parse_iso_dt(entry["last_seen"])
        except (ValueError, KeyError, TypeError):
            continue

        if last_seen < week_start:
            continue

        # Skip entries that started after this window closed
        if first_seen > now:
            continue

        parts = key.split(":", 1)
        insight_type = parts[0]
        zone_name = parts[1] if len(parts) > 1 else None

        active_count += 1
        active_keys.add(key)
        type_counts[insight_type] = type_counts.get(insight_type, 0) + 1
        if zone_name:
            zone_counts[zone_name] = zone_counts.get(zone_name, 0) + 1

        duration_days = (last_seen - first_seen).total_seconds() / 86400
        if duration_days > longest_days:
            longest_days = duration_days
            longest = (insight_type, zone_name, round(duration_days, 1))

        if last_seen < now - timedelta(hours=1):
            resolved_count += 1

    return _WeeklyStats(
        active_count=active_count,
        resolved_count=resolved_count,
        type_counts=type_counts,
        zone_counts=zone_counts,
        longest=longest,
        active_keys=active_keys,
    )


def _natural_join(items: list[str]) -> str:
    """Join a list of strings with natural language (commas + 'and')."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _group_keys_by_type(keys: set[str]) -> dict[str, list[str]]:
    """Group issue keys by insight type, collecting zone names."""
    type_zones: dict[str, list[str]] = {}
    for key in sorted(keys):
        parts = key.split(":", 1)
        itype = parts[0]
        zone = parts[1] if len(parts) > 1 else None
        # Skip internal zone prefixes (e.g. "_hub" for cross-zone insights)
        if zone and zone.startswith("_"):
            zone = None
        if itype not in type_zones:
            type_zones[itype] = []
        if zone:
            type_zones[itype].append(zone)
    return type_zones


def _describe_issue_keys(keys: set[str]) -> str:
    """Describe a set of issue keys as a readable phrase grouped by insight type."""
    type_zones = _group_keys_by_type(keys)

    fragments: list[str] = []
    for itype, zones in type_zones.items():
        readable = itype.replace("_", " ")
        if zones:
            fragments.append(f"{readable} in {_natural_join(zones)}")
        else:
            fragments.append(readable)

    return _natural_join(fragments)


def _describe_resolved_keys(keys: set[str]) -> str:
    """Describe resolved issue keys as a readable sentence grouped by insight type."""
    type_counts: dict[str, int] = {}
    for key in keys:
        itype = key.split(":", 1)[0]
        type_counts[itype] = type_counts.get(itype, 0) + 1

    total = len(keys)
    if total == 1:
        itype = next(iter(type_counts))
        readable = itype.replace("_", " ")
        return f"1 {readable} issue was resolved."

    # Multiple resolved: group by type
    parts: list[str] = []
    for itype, count in sorted(type_counts.items()):
        readable = itype.replace("_", " ")
        parts.append(f"{count} {readable}")

    if len(parts) == 1:
        return f"{parts[0]} issues were resolved."
    return f"{' and '.join(parts)} issues were resolved."


def build_trend_digest(
    history: InsightHistoryTracker,
    now: datetime,
) -> str:
    """Build a pure-trend weekly digest comparing this week vs previous week."""
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    entries = history.entries
    if not entries:
        return "No insights this week."

    current_stats = _collect_weekly_stats(entries, week_ago, now)
    previous_stats = _collect_weekly_stats(entries, two_weeks_ago, week_ago)

    cur = current_stats.active_count
    prev = previous_stats.active_count

    label = "insight" if cur == 1 else "insights"

    if cur == 0 and prev == 0:
        return "No insights this week."

    if cur == 0:
        header = f"No insights this week, down from {prev} last week."
    elif prev == 0:
        header = f"{cur} {label} this week."
    elif cur > prev:
        header = f"{cur} {label} this week, up from {prev} last week."
    elif cur < prev:
        header = f"{cur} {label} this week, down from {prev} last week."
    else:
        header = f"{cur} {label} this week, same as last week."

    new_keys = current_stats.active_keys - previous_stats.active_keys
    resolved_keys = previous_stats.active_keys - current_stats.active_keys

    details: list[str] = []

    if new_keys:
        new_descriptions = _describe_issue_keys(new_keys)
        details.append(f"New: {new_descriptions}.")

    if resolved_keys:
        resolved_descriptions = _describe_resolved_keys(resolved_keys)
        details.append(resolved_descriptions)

    if not new_keys and not resolved_keys and prev > 0:
        details.append("No new issues, none resolved.")

    parts = [header] + details
    return " ".join(parts)
