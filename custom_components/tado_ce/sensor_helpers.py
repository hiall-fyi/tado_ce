"""Tado CE sensor helpers: outdoor temperature lookup + effective temperature for mold risk."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .calculations import calculate_surface_temperature

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .config_manager import ConfigurationManager
    from .zone_config_manager import ZoneConfigManager

_LOGGER = logging.getLogger(__name__)


def get_outdoor_temperature(hass: HomeAssistant, entity_id: str, use_feels_like: bool = False) -> float | None:
    """Read outdoor temperature from a weather entity or plain sensor."""
    if not hass or not entity_id:
        return None

    try:
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None

        if entity_id.startswith("weather."):
            if use_feels_like:
                temp = state.attributes.get("apparent_temperature")
                if temp is None:
                    temp = state.attributes.get("feels_like")
                if temp is None:
                    temp = state.attributes.get("temperature")
            else:
                temp = state.attributes.get("temperature")

            if temp is not None:
                return float(temp)
        else:
            return float(state.state)
    except (ValueError, TypeError):
        # State wasn't numeric (e.g. weather entity reporting a
        # "snowy" condition string), silently fall through.
        pass
    except Exception as e:
        _LOGGER.debug(
            "Sensor Helpers: could not read outdoor temperature from "
            "%s (%s), falling back to None",
            entity_id, e,
        )

    return None


def get_effective_temperature(
    hass: HomeAssistant,
    zone_id: str,
    room_temp: float,
    config_manager: ConfigurationManager = None,  # type: ignore[assignment]
    *,
    zone_config_manager: ZoneConfigManager,
) -> tuple[Any, ...]:
    """Pick the temperature mold-risk math should use for this zone."""
    fallback = (room_temp, None, None, "room_average", 0.0)

    try:
        if not config_manager:
            return fallback

        outdoor_entity = config_manager.get_outdoor_temp_entity()
        if not outdoor_entity:
            return fallback

        outdoor_temp = get_outdoor_temperature(
            hass,
            outdoor_entity,
            config_manager.get_use_feels_like(),
        )
        if outdoor_temp is None:
            return fallback

        u_value = zone_config_manager.get_window_u_value(zone_id, config_manager)
        surface_offset = zone_config_manager.get_surface_temp_offset(zone_id)

        surface_temp = calculate_surface_temperature(room_temp, outdoor_temp, u_value)

        # Calibration offset is added after the physics so users can
        # nudge the estimate without affecting the underlying U-value.
        if surface_offset != 0.0:
            surface_temp = surface_temp + surface_offset
        source = "surface_estimation"

        return (surface_temp, outdoor_temp, surface_temp, source, surface_offset)

    except Exception as e:
        _LOGGER.debug(
            "Sensor Helpers: could not derive effective temperature "
            "for zone %s (%s), falling back to room average",
            zone_id, e,
        )
        return fallback


__all__ = ["get_outdoor_temperature"]
