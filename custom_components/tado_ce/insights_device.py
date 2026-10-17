"""Device insights: battery, connection, geofencing."""

from __future__ import annotations

from typing import Any

from .insights_models import (
    OFFLINE_DAY_MINUTES,
    OFFLINE_RECENT_MINUTES,
    OFFLINE_SHORT_MINUTES,
    Insight,
    InsightPriority,
)


def calculate_battery_recommendation(
    battery_state: str,
    zone_name: str,
    device_type: str | None = None,
    offline_since: str | None = None,
) -> str:
    """Calculate SMART recommendation for battery status."""
    if battery_state.upper() == "NORMAL":
        return ""

    battery_type = "AA batteries"
    if device_type:
        device_lower = device_type.lower()
        if "trv" in device_lower or "va0" in device_lower or "ru0" in device_lower:
            battery_type = "2x AA batteries"
        elif "thermostat" in device_lower or "su0" in device_lower:
            battery_type = "3x AAA batteries"

    if offline_since and battery_state.upper() in ("LOW", "CRITICAL"):
        return (
            f"{zone_name}: Device offline since {offline_since}, last reported battery {battery_state.lower()}. "
            f"Replace the {battery_type} and check the device reconnects."
        )

    if battery_state.upper() == "CRITICAL":
        return f"{zone_name}: Replace {battery_type} TODAY, the device may stop working"

    if battery_state.upper() == "LOW":
        return f"{zone_name}: Replace {battery_type} within 1-2 weeks"

    return ""


def calculate_connection_recommendation(
    connection_state: str,
    zone_name: str,
    last_seen: str | None = None,
    offline_minutes: int | None = None,
) -> str:
    """Calculate SMART recommendation for device connection status."""
    if connection_state.upper() == "ONLINE":
        return ""

    if connection_state.upper() == "OFFLINE":
        if offline_minutes is not None:
            if offline_minutes < OFFLINE_RECENT_MINUTES:
                return f"{zone_name}: Device offline {offline_minutes} min. May be temporary, wait 30 minutes"
            if offline_minutes < OFFLINE_SHORT_MINUTES:
                return (
                    f"{zone_name}: Device offline {offline_minutes} min. "
                    f"Check if the device is within 10m of the bridge"
                )
            if offline_minutes < OFFLINE_DAY_MINUTES:  # 24 hours
                hours = offline_minutes // 60
                return f"{zone_name}: Device offline {hours}h. Check batteries and bridge connection"
            days = offline_minutes // 1440
            return f"{zone_name}: Device offline {days} days. Replace batteries and re-pair if needed"

        if last_seen:
            return f"{zone_name}: Device offline since {last_seen}. Check batteries and bridge connection"

        return (
            f"{zone_name}: Device offline. 1) Check batteries "
            "2) Verify bridge is online 3) Move device closer to bridge"
        )

    return ""


def calculate_geofencing_device_offline_insight(
    devices: list[Any] | None = None,
) -> Insight | None:
    """Detect when a geofencing mobile device has location tracking disabled."""
    if not devices:
        return None

    offline_devices = [d.get("name", "Unknown") for d in devices if not d.get("location_enabled", True)]

    if not offline_devices:
        return None

    devices_str = ", ".join(offline_devices[:3])
    rec = f"Geofencing device(s) with location disabled: {devices_str}. Home/away detection may be inaccurate"

    return Insight(
        priority=InsightPriority.MEDIUM,
        recommendation=rec,
        insight_type="geofencing_offline",
        zone_name=None,
    )
