"""Tado CE display formatting helpers: internal-to-display value conversions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import OVERLAY_MODE_REVERSE_MAP, WINDOW_TYPE_REVERSE_MAP

if TYPE_CHECKING:
    from collections.abc import Callable

# === Maps owned by this module (no equivalent in const.py) ===

WEATHER_STATE_MAP: dict[str, str] = {
    "CLOUDY_MOSTLY": "Mostly Cloudy",
    "CLOUDY_PARTLY": "Partly Cloudy",
    "CLOUDY": "Cloudy",
    "DRIZZLE": "Drizzle",
    "FOGGY": "Foggy",
    "NIGHT_CLEAR": "Clear Night",
    "NIGHT_CLOUDY": "Cloudy Night",
    "RAIN": "Rain",
    "SCATTERED_RAIN": "Scattered Rain",
    "SNOW": "Snow",
    "SUN": "Sunny",
    "THUNDERSTORMS": "Thunderstorms",
    "WINDY": "Windy",
}

ZONE_TYPE_DISPLAY_MAP: dict[str, str] = {
    "HEATING": "Heating",
    "AIR_CONDITIONING": "Air Conditioning",
    "HOT_WATER": "Hot Water",
}

COMFORT_MODEL_DISPLAY_MAP: dict[str, str] = {"adaptive": "Adaptive", "seasonal": "Seasonal"}

BRIDGE_WIRING_STATE_MAP: dict[str, str] = {
    "INSTALLATION_COMPLETED": "Ready",
    "INSTALLED": "Installed",
    "NOT_INSTALLED": "Not Installed",
    "INSTALLATION_IN_PROGRESS": "Installing",
    "INSTALLATION_FAILED": "Failed",
}

INSIGHT_TYPE_DISPLAY_MAP: dict[str, str] = {
    "mold_risk": "Mold Risk",
    "comfort": "Comfort",
    "battery": "Battery",
    "connection": "Connection",
    "window_predicted": "Open Window",
    "condensation": "Condensation",
    "preheat_timing": "Preheat Timing",
    "schedule_deviation": "Schedule Deviation",
    "heating_anomaly": "Heating Anomaly",
    "cross_zone_mold": "Cross-Zone Mold",
    "cross_zone_window": "Cross-Zone Open Window",
    "cross_zone_condensation": "Cross-Zone Condensation",
    "cross_zone_efficiency": "Cross-Zone Efficiency",
    "api_quota_planning": "API Quota",
    "weather_impact": "Weather Impact",
    "overlay_duration": "Overlay Duration",
    "schedule_gap": "Schedule Gap",
    "frequent_override": "Frequent Override",
    "away_heating": "Away Heating",
    "home_all_off": "Home All Off",
    "solar_gain": "Solar Gain",
    "solar_ac_load": "Solar AC Load",
    "frost_risk": "Frost Risk",
    "heating_season": "Heating Season",
    "heating_off_cold": "Heating Off Cold",
    "boiler_flow_anomaly": "Boiler Flow Anomaly",
    "early_start_disabled": "Early Start Disabled",
    "thermal_efficiency": "Thermal Efficiency",
    "temp_imbalance": "Temperature Imbalance",
    "humidity_imbalance": "Humidity Imbalance",
    "humidity_trend": "Humidity Trend",
    "device_limitation": "Device Limitation",
    "geofencing_offline": "Geofencing Offline",
    "api_usage_spike": "API Usage Spike",
}

API_STATUS_DISPLAY_MAP: dict[str, str] = {
    "ok": "OK",
    "warning": "Warning",
    "rate_limited": "Rate Limited",
}

CONFIDENCE_DISPLAY_MAP: dict[str, str] = {
    "no_schedule": "No Schedule",
    "insufficient_data": "Insufficient Data",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "none": "None",
    "unknown": "Unknown",
}

TADO_MODE_DISPLAY_MAP: dict[str, str] = {"HOME": "Home", "AWAY": "Away"}
DATA_SOURCE_DISPLAY_MAP: dict[str, str] = {"home_state": "Home State", "zones": "Zones"}

BATTERY_STATE_DISPLAY_MAP: dict[str, str] = {
    "NORMAL": "Normal",
    "LOW": "Low",
    "CRITICAL": "Critical",
}


CONNECTION_STATE_ATTR_MAP: dict[bool | None, str] = {True: "online", False: "offline", None: "unknown"}


# === Generic lookup helper ===


def _lookup(mapping: dict[str, Any], value: Any, fallback_fn: Callable[..., Any] | None = None) -> str:
    """Look up value in mapping. Falsy value -> 'Unknown', unmapped -> fallback."""
    if value is None or value == "":
        return "Unknown"
    if value in mapping:
        return mapping[value]  # type: ignore[no-any-return]
    if fallback_fn:
        return fallback_fn(value)  # type: ignore[no-any-return]
    return str(value).replace("_", " ").title()


# === Format functions ===


def format_zone_type(zone_type: str) -> str:
    """Convert internal zone_type to user-friendly display value."""
    return ZONE_TYPE_DISPLAY_MAP.get(zone_type, zone_type)


def format_window_type(window_type: str) -> str:
    """Convert internal window_type to user-friendly display value."""
    return WINDOW_TYPE_REVERSE_MAP.get(window_type, window_type)


def format_comfort_model(comfort_model: str) -> str:
    """Convert internal comfort_model to user-friendly display value."""
    return _lookup(COMFORT_MODEL_DISPLAY_MAP, comfort_model, lambda v: v.title())


def format_insight_type(insight_type: str) -> str:
    """Convert internal insight_type to user-friendly display value."""
    return _lookup(INSIGHT_TYPE_DISPLAY_MAP, insight_type)


def format_priority(priority: str) -> str:
    """Convert internal priority to Title Case display value."""
    return priority.title() if priority else "None"


def format_api_status(status: str) -> str:
    """Convert internal API status to user-friendly display value."""
    return _lookup(API_STATUS_DISPLAY_MAP, status)


def format_overlay_type(overlay_type: str | None) -> str:
    """Convert internal overlay_type to user-friendly display value."""
    if overlay_type is None:
        return "None"
    return OVERLAY_MODE_REVERSE_MAP.get(
        overlay_type,
        str(overlay_type).replace("_", " ").title(),
    )


def format_confidence(confidence: str) -> str:
    """Convert internal confidence to user-friendly display value."""
    return _lookup(CONFIDENCE_DISPLAY_MAP, confidence)


def format_tado_mode(mode: str) -> str:
    """Convert internal tado mode to user-friendly display value."""
    return _lookup(TADO_MODE_DISPLAY_MAP, mode, lambda v: v.title())


def format_data_source(source: str) -> str:
    """Convert internal data source to user-friendly display value."""
    return _lookup(DATA_SOURCE_DISPLAY_MAP, source)




def format_battery_state(state: str) -> str:
    """Convert API batteryState to user-friendly display value."""
    return _lookup(BATTERY_STATE_DISPLAY_MAP, state, lambda v: v.title())


def format_connection_state_attr(connected: bool | None) -> str:
    """Map a parsed connectionState (True/False/None) to "online" / "offline" / "unknown"."""
    return CONNECTION_STATE_ATTR_MAP[connected]


def strip_zone_prefix(text: str, zone_name: str) -> str:
    """Remove redundant `<zone>: ` prefix from per-zone entity recommendation text (home-level keeps prefix)."""
    prefix = f"{zone_name}: "
    if text.startswith(prefix):
        # Capitalise first char after stripping
        remainder = text[len(prefix):]
        return remainder[0].upper() + remainder[1:] if remainder else remainder
    return text


def format_health_score(score: int) -> str:
    """Format health score (0-100) with emoji and label for readability.

    Bands: 90-100 Excellent, 70-89 Good, 50-69 Fair, 25-49 Poor, 0-24 Critical.
    """
    if score >= 90:
        return f"🟢 {score}: Excellent"
    if score >= 70:
        return f"🟢 {score}: Good"
    if score >= 50:
        return f"🟡 {score}: Fair"
    if score >= 25:
        return f"🟠 {score}: Poor"
    return f"🔴 {score}: Critical"


def format_bridge_wiring_state(state: str) -> str:
    """Convert bridge wiring state to user-friendly display value."""
    return _lookup(BRIDGE_WIRING_STATE_MAP, state)




def format_boolean_connected(value: bool | None) -> str:
    """Format boolean as Connected/Disconnected for bridge connectivity fields."""
    if value is True:
        return "Connected"
    return "Disconnected"


def format_boolean_yes_no(value: bool | None) -> str:
    """Format boolean as Yes/No for bridge presence fields."""
    if value is True:
        return "Yes"
    return "No"


_PRIORITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢",
    "none": "⚪",
}

_PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}


def _format_duration_human(hours: float) -> str:
    """Format duration hours into human-readable string like '1d 4h' or '28h'."""
    if hours < 1:
        return f"{int(hours * 60)}m"
    days = int(hours // 24)
    remaining_hours = int(hours % 24)
    if days > 0 and remaining_hours > 0:
        return f"{days}d {remaining_hours}h"
    if days > 0:
        return f"{days}d"
    return f"{remaining_hours}h"


def format_persistent_insights_grouped(
    raw: list[dict[str, Any]],
    escalated_priorities: dict[tuple[str, str | None], int] | None = None,
) -> list[str]:
    """Group persistent insights by priority+type, merge zones, return sorted lines.

    Input: list of dicts from InsightHistoryTracker.get_persistent_insights()
    Output: list of formatted strings like "🔴 High: Battery in Guest (1d 4h)"
    Zones with same insight type + priority are merged into one line.

    When *escalated_priorities* is provided, the escalated priority is used
    instead of the base_priority stored in history.  Keys are
    ``(formatted_insight_type, zone_name)`` → ``InsightPriority.value`` int.
    """
    if not raw:
        return []

    priority_names: dict[int, str] = {0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical"}

    groups: dict[tuple[str, str], list[tuple[str | None, float]]] = {}
    for item in raw:
        base_priority_str = str(item.get("base_priority", "Low")).lower()
        insight_type = str(item.get("insight_type", "Unknown"))
        zone_name: str | None = item.get("zone_name")
        duration = float(item.get("duration_hours", 0))

        # Use escalated priority when available
        if escalated_priorities:
            escalated_val = escalated_priorities.get((insight_type, zone_name))
            if escalated_val is not None:
                priority_str = priority_names.get(escalated_val, base_priority_str)
            else:
                priority_str = base_priority_str
        else:
            priority_str = base_priority_str

        key = (priority_str, insight_type)
        if key not in groups:
            groups[key] = []
        groups[key].append((zone_name, duration))

    sorted_keys = sorted(groups, key=lambda k: (_PRIORITY_ORDER.get(k[0], 99), k[1]))

    lines: list[str] = []
    for priority_str, insight_type in sorted_keys:
        entries = groups[(priority_str, insight_type)]
        zones = [z for z, _ in entries if z]
        max_duration = max(d for _, d in entries)
        emoji = _PRIORITY_EMOJI.get(priority_str, "⚪")
        priority_display = priority_str.title()
        duration_str = _format_duration_human(max_duration)

        if zones:
            zones_str = ", ".join(sorted(zones))
            lines.append(f"{emoji} {priority_display}: {insight_type} in {zones_str} ({duration_str})")
        else:
            lines.append(f"{emoji} {priority_display}: {insight_type} ({duration_str})")

    return lines



def build_zone_insight_attributes(
    insights: list[Any],
    zone_name: str,
) -> dict[str, Any]:
    """Build extra_state_attributes for a zone insight sensor (only includes non-empty `insight_types` / `recommendations`)."""
    if not insights:
        return {}

    attrs: dict[str, Any] = {}
    types_list = [format_insight_type(i.insight_type) for i in insights]
    recs_list = [strip_zone_prefix(i.recommendation, zone_name) for i in insights]
    if types_list:
        attrs["insight_types"] = types_list
    if recs_list:
        attrs["recommendations"] = recs_list
    return attrs
