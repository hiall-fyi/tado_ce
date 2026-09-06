"""Pure functions for comfort banding: humidity level + comfort target."""

from __future__ import annotations

from .calculations import (
    HEAT_INDEX_ACTIVATION_TEMP,
    calculate_ashrae_comfort_temp,
    calculate_heat_index,
    calculate_seasonal_comfort_target,
)
from .const import (
    _HUMIDITY_DRY_THRESHOLD,
    _HUMIDITY_HUMID_THRESHOLD,
)


def classify_humidity_level(humidity: float | None) -> str | None:
    """Band humidity to Tado's DRY / COMFY / HUMID (None when no reading)."""
    if humidity is None:
        return None
    if humidity < _HUMIDITY_DRY_THRESHOLD:
        return "dry"
    if humidity > _HUMIDITY_HUMID_THRESHOLD:
        return "humid"
    return "comfy"


def compute_comfort_target(
    temperature: float,
    humidity: float | None,
    outdoor_temp: float | None,
    latitude: float,
    day_of_year: int,
) -> tuple[float, float]:
    """Return (comfort_target, deviation) mirroring the comfort_level model."""
    if outdoor_temp is not None:
        target = round(calculate_ashrae_comfort_temp(outdoor_temp), 1)
    else:
        target = calculate_seasonal_comfort_target(latitude, day_of_year)

    effective_temp = temperature
    if humidity is not None and temperature >= HEAT_INDEX_ACTIVATION_TEMP:
        effective_temp = calculate_heat_index(temperature, humidity)

    return target, effective_temp - target
