"""Shared models and constants for the Tado CE insights engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .models import InsightTemperatureReading

__all__ = ["InsightTemperatureReading"]


class InsightPriority(IntEnum):
    """Priority levels for insights (higher = more urgent)."""

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class Insight:
    """Represent an actionable insight."""

    priority: InsightPriority
    recommendation: str
    insight_type: str  # e.g., "mold_risk", "comfort", "battery", "window_predicted"
    zone_name: str | None = None


@dataclass
class WindowPredictedResult:
    """Result of window predicted detection."""

    detected: bool
    confidence: str  # "none", "low", "medium", "high"
    temp_drop: float
    time_window_minutes: int
    recommendation: str
    anomaly_readings: int = 0
    cooldown_active: bool = False
    detection_mode: str = "active"


# =============================================================================
# Insight Threshold Constants
# =============================================================================

# Window detection
WINDOW_MIN_READINGS = 2

# Window predicted sensitivity presets: threshold mappings per sensitivity level
WINDOW_SENSITIVITY_PRESETS: dict[str, dict[str, int | float]] = {
    "low": {
        "consecutive_drops": 3,
        "high_confidence_count": 4,
        "high_confidence_change": 2.0,
        "medium_change_threshold": 1.5,
    },
    "medium": {
        "consecutive_drops": 2,
        "high_confidence_count": 3,
        "high_confidence_change": 1.5,
        "medium_change_threshold": 1.0,
    },
    "high": {
        "consecutive_drops": 1,
        "high_confidence_count": 2,
        "high_confidence_change": 1.0,
        "medium_change_threshold": 0.5,
    },
}

# Passive mode signal weights
PASSIVE_WEIGHT_TEMP = 0.60
PASSIVE_WEIGHT_HUMIDITY = 0.25
PASSIVE_WEIGHT_OUTDOOR = 0.15

# Passive mode sensitivity presets: stricter than active
WINDOW_PASSIVE_SENSITIVITY_PRESETS: dict[str, dict[str, float]] = {
    "low": {
        "temp_rate_threshold": 0.4,
        "high_confidence_score": 0.85,
        "medium_confidence_score": 0.60,
        "humidity_boost_threshold": 8.0,
        "min_readings": 4,
    },
    "medium": {
        "temp_rate_threshold": 0.25,
        "high_confidence_score": 0.75,
        "medium_confidence_score": 0.50,
        "humidity_boost_threshold": 5.0,
        "min_readings": 3,
    },
    "high": {
        "temp_rate_threshold": 0.15,
        "high_confidence_score": 0.65,
        "medium_confidence_score": 0.40,
        "humidity_boost_threshold": 3.0,
        "min_readings": 2,
    },
}

# Cooldown readings required per sensitivity
COOLDOWN_READINGS: dict[str, int] = {"low": 3, "medium": 2, "high": 1}

# Outdoor differential thresholds (°C)
OUTDOOR_DIFF_HIGH = 10.0
OUTDOOR_DIFF_LOW = 5.0

# Seasonal baseline defaults
SEASONAL_BASELINE_MIN_SAMPLES = 168  # 7 days of hourly readings
SEASONAL_COLD_THRESHOLD = 5.0  # °C outdoor: winter mode

# Mold risk: humidity thresholds (%)
MOLD_HUMIDITY_CRITICAL = 70
MOLD_HUMIDITY_HIGH = 70
MOLD_HUMIDITY_MEDIUM = 65

# Mold risk: dew point margin thresholds (°C)
MOLD_MARGIN_HIGH = 5
MOLD_MARGIN_MEDIUM = 7

# Device offline thresholds (minutes)
OFFLINE_RECENT_MINUTES = 30
OFFLINE_SHORT_MINUTES = 120
OFFLINE_DAY_MINUTES = 1440  # 24 hours

# API usage thresholds (%)
API_USAGE_NOTICE = 70
API_USAGE_WARNING = 80
API_USAGE_HIGH = 90
API_USAGE_CRITICAL = 95

# Historical temperature deviation thresholds (°C)
TEMP_DEVIATION_NORMAL = 1.5
TEMP_DEVIATION_SIGNIFICANT = 3.0
TEMP_DEVIATION_MIN_SAMPLES = 3

# Thermal analysis confidence thresholds (%)
CONFIDENCE_ADEQUATE = 70
CONFIDENCE_LOW = 30
CONFIDENCE_MODERATE = 50

# Heating anomaly detection
HEATING_ANOMALY_MIN_MINUTES = 60
HEATING_ANOMALY_POWER_THRESHOLD = 80  # %
HEATING_ANOMALY_TEMP_DELTA = 0.5  # °C

# Preheat timing
PREHEAT_LONG_MINUTES = 30

# Summary text max length
SUMMARY_MAX_LENGTH = 300

# Cross-zone thresholds
CROSS_ZONE_MOLD_MIN_ZONES = 3
CROSS_ZONE_WINDOW_MIN_ZONES = 2
CROSS_ZONE_CONDENSATION_MIN_ZONES = 3
CROSS_ZONE_EFFICIENCY_MIN_ZONES = 2

# API quota planning
API_QUOTA_BUFFER_HOURS = 6
API_QUOTA_HIGH_BUFFER_HOURS = 12

# Weather impact thresholds (°C)
WEATHER_COLD_SNAP_DELTA = 5.0
WEATHER_SEVERE_COLD_SNAP_DELTA = 10

# Calls per hour minimum samples
CALLS_PER_HOUR_MIN_SAMPLES = 2

# Schedule gap thresholds
SCHEDULE_GAP_MIN_OFF_HOURS = 6
SCHEDULE_GAP_MIN_DEFICIT = 2.0  # °C

# Home all off / heating off cold thresholds (°C)
HOME_COLD_MIN_DEFICIT = 2.0
HEATING_OFF_COLD_MIN_DEFICIT = 3.0

# Frost risk threshold (°C)
FROST_RISK_TEMP = 3.0

# Boiler flow anomaly thresholds
BOILER_FLOW_HIGH_TEMP = 60  # °C
BOILER_FLOW_LOW_DEMAND = 20  # %
BOILER_FLOW_LOW_TEMP = 30  # °C
BOILER_FLOW_HIGH_DEMAND = 80  # %

# Thermal efficiency thresholds
THERMAL_EFFICIENCY_MIN_CONFIDENCE = 0.5
THERMAL_INERTIA_HIGH_MINUTES = 60
HEATING_RATE_SLOW = 0.5  # °C/h

# Temperature imbalance threshold (°C)
TEMP_IMBALANCE_MIN_DIFF = 4.0

# Humidity imbalance threshold (%)
HUMIDITY_IMBALANCE_MIN_EXCESS = 15

# Humidity trend thresholds
HUMIDITY_TREND_MIN_SAMPLES = 6
HUMIDITY_TREND_MIN_RISE = 10  # %

# Ventilation opportunity thresholds
VENTILATION_OPPORTUNITY_MIN_SAMPLES = 6
VENTILATION_OPPORTUNITY_MIN_RISE = 2.0  # g/m3

# API usage spike threshold (ratio)
API_USAGE_SPIKE_RATIO = 2.0


# ============ Priority Escalation Rules ============
# Maps insight_type → list of (days_threshold, escalated_priority), sorted ascending.
# Last matching rule wins (i.e., longest duration that applies).
ESCALATION_RULES: dict[str, list[tuple[int, InsightPriority]]] = {
    "battery": [(7, InsightPriority.HIGH), (14, InsightPriority.CRITICAL)],
    "mold_risk": [(3, InsightPriority.HIGH), (7, InsightPriority.CRITICAL)],
    "condensation": [(3, InsightPriority.HIGH)],
    "connection": [(2, InsightPriority.CRITICAL)],
    "heating_anomaly": [(1, InsightPriority.CRITICAL)],
    "humidity_trend": [(5, InsightPriority.HIGH)],
    "heating_off_cold": [(2, InsightPriority.HIGH)],
    "frost_risk": [(1, InsightPriority.HIGH), (3, InsightPriority.CRITICAL)],
}

# ---------------------------------------------------------------------------
# Correlation groups: related insight types that can be merged per zone
# ---------------------------------------------------------------------------
CORRELATION_GROUPS: dict[str, list[str]] = {
    "humidity_problem": [
        "mold_risk",
        "humidity_trend",
        "condensation",
        "cross_zone_condensation",
    ],
    "heating_efficiency_issue": [
        "heating_anomaly",
        "thermal_efficiency",
        "boiler_flow_anomaly",
    ],
    "schedule_review": [
        "schedule_gap",
        "schedule_deviation",
    ],
    "device_maintenance": ["battery", "connection"],
}

# Reverse lookup: insight_type → group key (immutable after module load)
_INSIGHT_TO_GROUP: dict[str, str] = {
    t: grp for grp, types in CORRELATION_GROUPS.items() for t in types
}
