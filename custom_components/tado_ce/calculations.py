"""Tado CE physics calculations: dew point, surface temp, mold/comfort/heat-risk classifiers.

Pure functions used by the environment / smart-comfort
sensors. The Magnus-Tetens approximation is accurate to ±0.1%
in the −40°C to 50°C range, covering every realistic indoor /
outdoor temperature.
"""

from __future__ import annotations

import logging
import math

from .const import INTERIOR_SURFACE_HEAT_TRANSFER_COEFFICIENT

_LOGGER = logging.getLogger(__name__)

# ============ Magnus-Tetens Constants (Alduchov & Eskridge 1996) ============
# Valid range: -40°C to 50°C, accuracy ±0.1%
MAGNUS_A: float = 17.27
MAGNUS_B: float = 237.7  # °C

# ============ Mold Risk Thresholds (margin = effective_temp - dew_point) ============
MOLD_RISK_CRITICAL: float = 3.0  # °C: margin < 3 → Critical
MOLD_RISK_HIGH: float = 5.0  # °C: margin < 5 → High
MOLD_RISK_MEDIUM: float = 7.0  # °C: margin < 7 → Medium

# ============ Condensation Risk Thresholds: HEATING ============
# margin = surface_temp - indoor_dew_point, uses <= comparisons
CONDENSATION_HEATING_CRITICAL: float = 1.0  # °C
CONDENSATION_HEATING_HIGH: float = 3.0  # °C
CONDENSATION_HEATING_MEDIUM: float = 5.0  # °C
CONDENSATION_HEATING_LOW: float = 7.0  # °C

# ============ Condensation Risk Thresholds: AC ============
# margin = window_outer_surface_temp - outdoor_dew_point, uses < comparisons
CONDENSATION_AC_CRITICAL: float = 2.0  # °C
CONDENSATION_AC_HIGH: float = 4.0  # °C
CONDENSATION_AC_MEDIUM: float = 6.0  # °C

# ============ ASHRAE 55 Adaptive Comfort Model ============
ASHRAE_SLOPE: float = 0.31
ASHRAE_INTERCEPT: float = 17.8  # °C

# ============ Seasonal Comfort Constants ============
# Latitude offset by climate zone (Decision D-3: conservative scale)
SEASONAL_LAT_OFFSETS: tuple[tuple[float, float], ...] = (
    (55.0, -1.0),  # Nordic/Subarctic
    (45.0, -0.5),  # Northern Europe/Canada
    (40.0, 0.0),  # Temperate (default)
    (30.0, 0.5),  # Mediterranean
    (0.0, 1.0),  # Subtropical
)

SEASONAL_BASE_TARGETS: dict[str, float] = {
    "summer": 24.0,
    "winter": 20.0,
}

# ============ Comfort Level Thresholds ============
COMFORT_COLD: float = 16.0  # °C: below → Cold
COMFORT_COOL: float = 18.0  # °C: below → Cool
COMFORT_WARM: float = 24.0  # °C: above → Warm
COMFORT_HOT: float = 26.0  # °C: above → Hot

# ============ Cooling Rate Thresholds (°C/h) ============
# Cooling rate is measured in °C/h everywhere (smart_comfort._calculate_rate
# regresses temperature over hours; get_cooling_rate / _MIN_COOLING_RATE agree).
COOLING_RATE_MIN: float = -5.0  # °C/h: floor clamp for outlier rejection
COOLING_RATE_STABLE: float = -0.1  # °C/h: abs(rate) below this → room is stable

# ============ Heat Index Constants (NOAA/NWS) ============
HEAT_INDEX_ACTIVATION_TEMP: float = 26.7  # °C: below this, Heat Index = air temp

# Internal constants for the Steadman→Rothfusz transition blend.
_TRANSITION_THRESHOLD_F: float = 80.0  # °F: NOAA switch point
_BLEND_WIDTH_F: float = 2.0  # °F: linear-blend window above threshold

HEAT_RISK_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (51.0, "Extreme Danger"),
    (39.0, "Danger"),
    (32.0, "Extreme Caution"),
    (27.0, "Caution"),
)


# ============ Heat Index Calculation (NOAA/NWS) ============


def calculate_heat_index(temperature: float, humidity: float) -> float:
    """Calculate Heat Index using NOAA/NWS Rothfusz regression.

    Implements the 3-step NOAA algorithm: Steadman simple formula first,
    then full Rothfusz regression with low-RH and high-RH adjustments.
    All intermediate math in °F; result converted back to °C.

    Args:
        temperature: Air temperature in °C.
        humidity: Relative humidity in % (0-100).

    Returns:
        Heat Index in °C. Returns input temperature unchanged when < 26.7 °C.
    """
    if temperature < HEAT_INDEX_ACTIVATION_TEMP:
        return temperature

    # Convert to °F for NOAA formulas
    t_f = temperature * 9.0 / 5.0 + 32.0
    rh = humidity

    # Step 1: Steadman simple formula
    hi_simple = 0.5 * (t_f + 61.0 + (t_f - 68.0) * 1.2 + rh * 0.094)

    hi_simple_c = (hi_simple - 32.0) * 5.0 / 9.0

    avg = (hi_simple + t_f) / 2.0
    if avg < _TRANSITION_THRESHOLD_F:
        return hi_simple_c

    # Step 2: Full Rothfusz regression
    hi = (
        -42.379
        + 2.04901523 * t_f
        + 10.14333127 * rh
        - 0.22475541 * t_f * rh
        - 0.00683783 * t_f * t_f
        - 0.05481717 * rh * rh
        + 0.00122874 * t_f * t_f * rh
        + 0.00085282 * t_f * rh * rh
        - 0.00000199 * t_f * t_f * rh * rh
    )

    # Step 3a: Low-RH adjustment
    if rh < 13.0 and 80.0 < t_f < 112.0:
        hi -= ((13.0 - rh) / 4.0) * math.sqrt((17.0 - abs(t_f - 95.0)) / 17.0)

    # Step 3b: High-RH adjustment
    elif rh > 85.0 and 80.0 < t_f < 87.0:
        hi += ((rh - 85.0) / 10.0) * ((87.0 - t_f) / 5.0)

    rothfusz_c = (hi - 32.0) * 5.0 / 9.0

    # Step 4: Smooth the Steadman→Rothfusz transition.
    # At the exact boundary (avg ≈ 80 °F) the two formulas can disagree,
    # creating a discontinuity that violates humidity-monotonicity.
    # Linear-blend over a 2 °F window eliminates the jump while
    # converging to pure Rothfusz above the window.
    if avg < _TRANSITION_THRESHOLD_F + _BLEND_WIDTH_F:
        alpha = (avg - _TRANSITION_THRESHOLD_F) / _BLEND_WIDTH_F
        return hi_simple_c * (1.0 - alpha) + rothfusz_c * alpha

    return rothfusz_c


# ============ Heat Risk Level Classification ============


def classify_heat_risk_level(heat_index: float) -> str:
    """Classify Heat Index into NOAA risk level.

    Args:
        heat_index: Heat Index value in °C.

    Returns:
        Risk level: "Extreme Danger", "Danger", "Extreme Caution", "Caution", or "None".
    """
    for threshold, level in HEAT_RISK_THRESHOLDS:
        if heat_index >= threshold:
            return level
    return "None"


# ============ Cold Risk Level Classification ============

# WHO indoor-temperature health bands (absolute °C), warm → no risk.
# 16 °C top boundary aligns with COMFORT_COLD; below 18 °C WHO flags risk for
# vulnerable groups, below 16 °C respiratory, below 12 °C cardiovascular.
COLD_RISK_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (16.0, "None"),
    (12.0, "Respiratory"),
    (0.0, "Cardiovascular"),
)


def classify_cold_risk_level(inside_temp: float) -> str:
    """Classify indoor temperature into a WHO cold-health-risk band."""
    for threshold, level in COLD_RISK_THRESHOLDS:
        if inside_temp >= threshold:
            return level
    return "Cardiovascular"


# ============ Dew Point Calculation ============


def calculate_dew_point(temperature: float, humidity: float) -> float:
    """Calculate dew point using Magnus-Tetens formula (Alduchov & Eskridge 1996).

    Formula: Td = (b * alpha) / (a - alpha)
    where alpha = (a * T) / (b + T) + ln(RH/100)

    Args:
        temperature: Air temperature in °C.
        humidity: Relative humidity in %.

    Returns:
        Dew point temperature in °C (exact, no rounding).
    """
    humidity = max(1.0, min(100.0, humidity))
    alpha = (MAGNUS_A * temperature) / (MAGNUS_B + temperature) + math.log(humidity / 100.0)
    return (MAGNUS_B * alpha) / (MAGNUS_A - alpha)


# ============ Surface Temperature Calculation ============


def calculate_surface_temperature(
    indoor_temp: float,
    outdoor_temp: float,
    u_value: float,
    h: float = INTERIOR_SURFACE_HEAT_TRANSFER_COEFFICIENT,
) -> float:
    """Calculate window surface temperature using heat transfer physics.

    Formula: T_surface = T_indoor - (T_indoor - T_outdoor) * U / (U + h)

    Args:
        indoor_temp: Indoor temperature in °C.
        outdoor_temp: Outdoor temperature in °C.
        u_value: Window U-value (thermal transmittance) in W/m²K.
        h: Interior surface heat transfer coefficient in W/m²K.
           Default 8.0 (ISO 6946 combined convective+radiative).

    Returns:
        Estimated surface temperature in °C (exact, no rounding).
    """
    temp_diff = indoor_temp - outdoor_temp
    return indoor_temp - (temp_diff * u_value / (u_value + h))


# ============ Surface Relative Humidity ============


def calculate_surface_rh(effective_temp: float, dew_point: float) -> int | None:
    """Calculate relative humidity at the window surface.

    Uses Magnus-Tetens saturation vapour pressure with unified constants
    (a=17.27, b=237.7) matching calculate_dew_point.

    Args:
        effective_temp: Surface (or room) temperature in °C.
        dew_point: Dew point temperature in °C.

    Returns:
        Surface relative humidity as integer percentage (0-100), or None on error.
    """
    try:

        def _svp(temp: float) -> float:
            """Calculate saturation vapour pressure."""
            return 6.112 * math.exp((MAGNUS_A * temp) / (temp + MAGNUS_B))

        surface_rh = (_svp(dew_point) / _svp(effective_temp)) * 100.0
        return round(min(100.0, max(0.0, surface_rh)))
    except (ValueError, TypeError, ZeroDivisionError):
        _LOGGER.debug(
            "Calculations: surface RH could not be computed "
            "(effective_temp=%s, dew_point=%s), returning None",
            effective_temp,
            dew_point,
        )
        return None


# ============ Mold Risk Classification ============


def classify_mold_risk_by_margin(margin: float) -> str:
    """Classify mold risk level from pre-computed margin.

    Args:
        margin: Temperature margin (effective_temp - dew_point) in °C.

    Returns:
        Risk level: "Critical", "High", "Medium", or "Low".
    """
    if margin < MOLD_RISK_CRITICAL:
        return "Critical"
    if margin < MOLD_RISK_HIGH:
        return "High"
    if margin < MOLD_RISK_MEDIUM:
        return "Medium"
    return "Low"


def classify_mold_risk_level(inside_temp: float, humidity: float) -> str:
    """Classify mold risk level from temperature and humidity.

    Uses exact (unrounded) dew point to preserve monotonicity:
    higher temperature must never worsen the risk level.

    Args:
        inside_temp: Indoor temperature in °C.
        humidity: Relative humidity in %.

    Returns:
        Risk level: "Critical", "High", "Medium", or "Low".
    """
    dew_point = calculate_dew_point(inside_temp, humidity)
    margin = inside_temp - dew_point
    return classify_mold_risk_by_margin(margin)


# ============ Condensation Risk Classification ============


def classify_condensation_risk(margin: float, zone_type: str) -> str:
    """Classify condensation risk from margin and zone type.

    HEATING zones use <= thresholds (accounts for cold spots at window edges).
    AC zones use < thresholds.

    Args:
        margin: Temperature margin (surface_temp - dew_point) in °C.
        zone_type: "HEATING" or "AIR_CONDITIONING".

    Returns:
        HEATING: "Critical", "High", "Medium", "Low", or "None".
        AC: "Critical", "High", "Medium", or "Low".
    """
    if zone_type == "HEATING":
        if margin <= CONDENSATION_HEATING_CRITICAL:
            return "Critical"
        if margin <= CONDENSATION_HEATING_HIGH:
            return "High"
        if margin <= CONDENSATION_HEATING_MEDIUM:
            return "Medium"
        if margin <= CONDENSATION_HEATING_LOW:
            return "Low"
        return "None"

    # AC zone
    if margin < CONDENSATION_AC_CRITICAL:
        return "Critical"
    if margin < CONDENSATION_AC_HIGH:
        return "High"
    if margin < CONDENSATION_AC_MEDIUM:
        return "Medium"
    return "Low"


# ============ Comfort Level Classification ============


def classify_comfort_level(inside_temp: float) -> str:
    """Classify comfort level from indoor temperature.

    Args:
        inside_temp: Indoor temperature in °C.

    Returns:
        Comfort level: "Cold", "Cool", "Comfortable", "Warm", or "Hot".
    """
    if inside_temp < COMFORT_COLD:
        return "Cold"
    if inside_temp < COMFORT_COOL:
        return "Cool"
    if inside_temp <= COMFORT_WARM:
        return "Comfortable"
    if inside_temp <= COMFORT_HOT:
        return "Warm"
    return "Hot"


# ============ ASHRAE 55 Adaptive Comfort ============


def calculate_ashrae_comfort_temp(outdoor_temp: float) -> float:
    """Calculate neutral comfort temperature using ASHRAE 55 Adaptive Comfort Model.

    Formula: Comfort_temp = 0.31 * outdoor_temp + 17.8°C

    Args:
        outdoor_temp: Outdoor temperature in °C.

    Returns:
        Neutral comfort temperature in °C (exact, no rounding).
    """
    return ASHRAE_SLOPE * outdoor_temp + ASHRAE_INTERCEPT


# ============ Seasonal Comfort Target ============


SEASONAL_PEAK_DAY: float = 197.0  # mid-July: Northern Hemisphere summer peak


def calculate_seasonal_comfort_target(latitude: float, day_of_year: int) -> float:
    """Calculate comfort target from a smooth day-of-year seasonal blend.

    Pure function: no HA dependencies. Caller provides latitude and day_of_year
    (from e.g. `datetime.timetuple().tm_yday`).

    Interpolates continuously between the summer and winter base targets on a
    cosine curve, rather than switching between discrete month buckets: a
    discrete switch put a real step in the target right at each month
    boundary with nothing about the weather having changed.

    Args:
        latitude: Geographic latitude in degrees (negative = Southern Hemisphere).
        day_of_year: Day of the year (1-366).

    Returns:
        Comfort target temperature in °C.
    """
    half_year = 365.25 / 2
    peak_day = SEASONAL_PEAK_DAY - half_year if latitude < 0 else SEASONAL_PEAK_DAY
    phase = 2 * math.pi * (day_of_year - peak_day) / 365.25
    seasonal_factor = (math.cos(phase) + 1) / 2  # 1.0 at peak_day, 0.0 half a year later

    abs_lat = abs(latitude)
    lat_offset = 0.0
    for threshold, offset in SEASONAL_LAT_OFFSETS:
        if abs_lat > threshold:
            lat_offset = offset
            break

    base = SEASONAL_BASE_TARGETS["winter"] + seasonal_factor * (
        SEASONAL_BASE_TARGETS["summer"] - SEASONAL_BASE_TARGETS["winter"]
    )
    return base + lat_offset

# ============ Cooling Rate Crossover Estimation ============


def estimate_cooling_crossover(
    current_temp: float,
    target_temp: float,
    cooling_rate: float,
) -> float | None:
    """Estimate hours until temperature crosses below target due to cooling.

    Uses linear extrapolation from current cooling rate.
    Pure function: no HA dependencies.

    Args:
        current_temp: Current room temperature in °C.
        target_temp: Target temperature in °C.
        cooling_rate: Cooling rate in °C/h (negative value).

    Returns:
        Hours until crossover (positive float), or None if:
        - current_temp < target_temp (already below target)
        - cooling_rate >= COOLING_RATE_STABLE (stable or warming)
    """
    if current_temp < target_temp:
        return None

    if cooling_rate >= COOLING_RATE_STABLE:
        return None

    # Clamp extreme rates
    effective_rate = max(cooling_rate, COOLING_RATE_MIN)

    temp_margin = current_temp - target_temp
    hours = temp_margin / abs(effective_rate)

    return hours

