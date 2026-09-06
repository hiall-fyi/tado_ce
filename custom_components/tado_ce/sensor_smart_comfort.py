"""Tado CE Smart Comfort sensors: schedule deviation, next-schedule preview, preheat advisor, target."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .calculations import (
    calculate_ashrae_comfort_temp,
    calculate_seasonal_comfort_target,
    estimate_cooling_crossover,
)
from .const import ENTITY_DATA_PREHEAT_ADVISOR
from .entity_registry import ENTITY_REGISTRY, get_entity_category
from .format_helpers import (
    format_comfort_model as _format_comfort_model,
)
from .format_helpers import (
    format_confidence as _format_confidence,
)
from .insights_presenter import (
    calculate_historical_deviation_recommendation,
)
from .sensor_helpers import get_outdoor_temperature as _get_outdoor_temp
from .sensor_zone import TadoZoneSensor

if TYPE_CHECKING:
    from datetime import datetime

    from .coordinator import TadoDataUpdateCoordinator
    from .smart_comfort import NextScheduleBlock, SmartComfortManager

_LOGGER = logging.getLogger(__name__)

# Smart comfort sensor thresholds
_DEVIATION_ICON_THRESHOLD = 0.5  # °C: deviation above/below this changes icon
_MIN_HEATING_RATE = 0.1  # °C/h: minimum meaningful heating rate for preheat calculation
_MIN_COOLING_RATE = -0.1  # °C/h: minimum meaningful cooling rate
_DEVIATION_ICON_COLD = -2  # °C: deviation below this shows "cold" icon
_DEVIATION_ICON_HOT = 2  # °C: deviation above this shows "hot" icon


class TadoScheduleDeviationSensor(TadoZoneSensor):
    """Historical temperature comparison sensor (state = ΔT from 7-day same-hour average)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Schedule Deviation Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_schedule_deviation"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_native_unit_of_measurement = "°C"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Attributes
        self._current_temp: float | None = None
        self._historical_avg: float | None = None
        self._sample_count: int = 0
        self._summary: str = ""
        self._recommendation: str = ""

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "current_temperature": self._current_temp,
            "historical_average": self._historical_avg,
            "sample_count": self._sample_count,
            "summary": self._summary,
            **self._base_zone_attributes,
            "recommendation": self._recommendation,
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on comparison."""
        if self._attr_native_value is None:
            return "mdi:chart-timeline-variant"
        if self._attr_native_value > _DEVIATION_ICON_THRESHOLD:  # type: ignore[operator]
            return "mdi:thermometer-chevron-up"
        if self._attr_native_value < -_DEVIATION_ICON_THRESHOLD:  # type: ignore[operator]
            return "mdi:thermometer-chevron-down"
        return "mdi:thermometer-check"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update historical comparison from SmartComfortManager."""
        try:
            manager = self.coordinator.smart_comfort_manager if self.hass else None

            if not manager or not manager.is_enabled:
                self._data_present = False
                return

            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            sensor_data = zone_data.get("sensorDataPoints") or {}
            self._current_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")

            if self._current_temp is None:
                self._data_present = False
                return

            # Get historical comparison
            comparison = manager.get_historical_comparison(
                self._zone_id,
                self._current_temp,
            )

            if comparison is None:
                self._attr_native_value = None
                self._data_present = False
                self._historical_avg = None
                self._sample_count = 0
                self._summary = "Insufficient data"
                return

            self._attr_native_value = comparison.difference
            self._historical_avg = comparison.historical_avg
            self._sample_count = comparison.sample_count
            self._summary = comparison.to_summary()

            self._recommendation = calculate_historical_deviation_recommendation(
                deviation=comparison.difference,
                zone_name=self._zone_name,
                current_temp=self._current_temp,
                historical_avg=comparison.historical_avg,
                sample_count=comparison.sample_count,
            )

            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Smart Comfort Sensor: zone %s historical comparison "
                "update failed (%s), marking unavailable until the "
                "next poll",
                self._zone_id, e,
            )
            self._data_present = False


class TadoNextScheduleTimeSensor(TadoZoneSensor):
    """Next schedule time sensor (state = next scheduled change e.g. "17:00" or "Tomorrow 07:00")."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Next Schedule Time Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_next_schedule"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

        # Attributes
        self._next_temp: float | None = None
        self._is_heating_on: bool = False
        self._is_tomorrow: bool = False
        self._minutes_until: int | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "next_temperature": self._next_temp,
            "is_heating_on": self._is_heating_on,
            "is_tomorrow": self._is_tomorrow,
            "minutes_until": self._minutes_until,
            **self._base_zone_attributes,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update next schedule time from schedule data."""
        try:
            from .smart_comfort import get_next_schedule_change

            _dl = self.coordinator.data_loader
            next_block = get_next_schedule_change(self._zone_id, data_loader=_dl)

            if next_block is None:
                self._attr_native_value = "No schedule"
                self._data_present = True
                self._next_temp = None
                self._is_heating_on = False
                self._is_tomorrow = False
                self._minutes_until = None
                return

            now = dt_util.now()
            self._is_tomorrow = next_block.start_time.date() > now.date()
            self._is_heating_on = next_block.is_heating_on
            self._next_temp = next_block.target_temp

            # Calculate minutes until
            time_diff = next_block.start_time - now
            self._minutes_until = int(time_diff.total_seconds() / 60)

            # Format display value
            time_str = next_block.start_time.strftime("%H:%M")
            if self._is_tomorrow:
                self._attr_native_value = f"Tomorrow {time_str}"
            else:
                self._attr_native_value = time_str

            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Smart Comfort Sensor: zone %s next schedule time "
                "update failed (%s), marking unavailable until the "
                "next poll",
                self._zone_id, e,
            )
            self._data_present = False


class TadoNextScheduleTempSensor(TadoZoneSensor):
    """Next schedule target temperature sensor (state = target °C or "OFF")."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Next Schedule Temp Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_next_sched_temp"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        # No unit_of_measurement so we can show "OFF" as state
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

        # Attributes
        self._schedule_time: str | None = None
        self._is_heating_on: bool = False
        self._current_temp: float | None = None
        self._temp_diff: float | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        attrs = {
            "schedule_time": self._schedule_time,
            "is_heating_on": self._is_heating_on,
            "current_temperature": self._current_temp,
            "temperature_difference": self._temp_diff,
            **self._base_zone_attributes,
        }
        # Add unit only when showing temperature
        if self._is_heating_on and isinstance(self._attr_native_value, (int, float)):
            attrs["unit_of_measurement"] = "°C"
        return attrs

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on heating direction."""
        if self._temp_diff is not None:
            if self._temp_diff > 0:
                return "mdi:thermometer-chevron-up"
            if self._temp_diff < 0:
                return "mdi:thermometer-chevron-down"
        if not self._is_heating_on:
            return "mdi:thermometer-off"
        return "mdi:thermometer"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update next schedule temperature from schedule data."""
        try:
            from .smart_comfort import get_next_schedule_change

            _dl = self.coordinator.data_loader
            next_block = get_next_schedule_change(self._zone_id, data_loader=_dl)

            if next_block is None:
                self._attr_native_value = "No schedule"
                self._data_present = True
                self._schedule_time = None
                self._is_heating_on = False
                self._current_temp = None
                self._temp_diff = None
                return

            self._is_heating_on = next_block.is_heating_on
            self._schedule_time = next_block.start_time.strftime("%H:%M")

            zone_data = self._get_zone_data()
            if zone_data:
                sensor_data = zone_data.get("sensorDataPoints") or {}
                self._current_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")

            if not next_block.is_heating_on or next_block.target_temp is None:
                # Heating OFF block - show "OFF" instead of unknown
                self._attr_native_value = "OFF"
                self._data_present = True
                self._temp_diff = None
                return

            self._attr_native_value = next_block.target_temp

            # Calculate temperature difference
            if self._current_temp is not None:
                self._temp_diff = round(next_block.target_temp - self._current_temp, 1)
            else:
                self._temp_diff = None

            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Smart Comfort Sensor: zone %s next schedule "
                "temperature update failed (%s), marking unavailable "
                "until the next poll",
                self._zone_id, e,
            )
            self._data_present = False


class TadoPreheatAdvisorSensor(TadoZoneSensor):
    """Preheat timing advisor sensor (state = recommended start time, e.g. "06:15")."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Preheat Advisor Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_preheat_advisor"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)

        # Attributes
        self._current_temp: float | None = None
        self._target_temp: float | None = None
        self._target_time: str | None = None
        self._duration_minutes: int | None = None
        self._heating_rate: float | None = None
        self._confidence: str = "unknown"
        self._summary: str = ""
        self._is_tomorrow: bool = False
        self._cooling_rate: float | None = None
        self._predicted_crossover_time: str | None = None
        self._is_cooling_prediction: bool = False

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "current_temperature": self._current_temp,
            "target_temperature": self._target_temp,
            "target_time": self._target_time,
            "duration_minutes": self._duration_minutes,
            "heating_rate": self._heating_rate,
            "confidence": _format_confidence(self._confidence),
            "is_tomorrow": self._is_tomorrow,
            "summary": self._summary,
            "cooling_rate": self._cooling_rate,
            "predicted_crossover_time": self._predicted_crossover_time,
            "is_cooling_prediction": self._is_cooling_prediction,
            **self._base_zone_attributes,
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on confidence."""
        if self._confidence == "high":
            return "mdi:clock-check"
        if self._confidence == "medium":
            return "mdi:clock-alert"
        if self._confidence == "low":
            return "mdi:clock-outline"
        if self._confidence == "no_schedule":
            return "mdi:calendar-remove"
        if self._confidence == "insufficient_data":
            return "mdi:database-off"
        return "mdi:clock-start"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    def _set_simple_status(
        self,
        value: str,
        *,
        confidence: str,
        summary: str,
        target_temp: float | None = None,
        target_time: str | None = None,
        duration_minutes: int | None = None,
        heating_rate: float | None = None,
    ) -> None:
        """Set sensor to a simple status state (no preheat calculation)."""
        self._attr_native_value = value
        self._data_present = True
        self._target_temp = target_temp
        self._target_time = target_time
        self._duration_minutes = duration_minutes
        self._heating_rate = heating_rate
        self._confidence = confidence
        self._summary = summary
        self._cooling_rate = None
        self._predicted_crossover_time = None
        self._is_cooling_prediction = False

    def _resolve_cycle_heating_rate(self) -> tuple[float | None, str | None]:
        """Resolve heating rate and confidence from HeatingCycleCoordinator."""
        heating_cycle_coordinator = self.coordinator.heating_cycle_coordinator
        if not heating_cycle_coordinator:
            return None, None
        zone_data_cycle = heating_cycle_coordinator.get_zone_data(self._zone_id)
        if not zone_data_cycle or zone_data_cycle.get("heating_rate") is None:
            return None, None
        rate = zone_data_cycle.get("heating_rate")
        cycle_count = zone_data_cycle.get("cycle_count", 0)
        if cycle_count >= 5:
            confidence = "high"
        elif cycle_count >= 3:
            confidence = "medium"
        else:
            confidence = "low"
        return rate, confidence

    def _get_ufh_buffer(self) -> int:
        """Get UFH buffer minutes from per-zone config (0 for radiator)."""
        zone_config_mgr = self.coordinator.zone_config_manager
        if not zone_config_mgr:
            return 0
        heating_type = zone_config_mgr.get_zone_value(self._zone_id, "heating_type", "radiator")
        if heating_type == "ufh":
            return int(zone_config_mgr.get_zone_value(self._zone_id, "ufh_buffer_minutes", 30))
        return 0

    def _apply_preheat_timing(
        self,
        start_dt: datetime,
        minutes_needed: int,
        heating_rate: float,
        confidence: str,
        ufh_buffer: int,
    ) -> None:
        """Apply preheat timing result to sensor state."""
        now = dt_util.now()
        self._is_tomorrow = start_dt.date() > now.date()
        time_str = start_dt.strftime("%H:%M")
        self._attr_native_value = f"Tomorrow {time_str}" if self._is_tomorrow else time_str
        self._duration_minutes = minutes_needed
        self._heating_rate = heating_rate
        self._confidence = confidence
        self._cooling_rate = None
        self._predicted_crossover_time = None
        self._is_cooling_prediction = False
        self._summary = (
            f"Start at {self._attr_native_value} ({minutes_needed} min to reach {self._target_temp:.1f}°C)"
        )
        if ufh_buffer > 0:
            self._summary += f" (includes {ufh_buffer} min UFH buffer)"
        self._data_present = True

    def _check_active_cooling_preheat(self, zone_data: dict[str, Any]) -> bool:
        """Check if active setpoint needs proactive cooling-based preheat (returns True if applied)."""
        from datetime import timedelta

        active_setting = zone_data.get("setting") or {}
        active_power = active_setting.get("power")
        active_target = (active_setting.get("temperature") or {}).get("celsius")

        if not (
            active_power == "ON"
            and active_target is not None
            and self._current_temp is not None
            and self._current_temp >= active_target
        ):
            return False

        now = dt_util.now()
        active_deadline = now + timedelta(hours=2)
        active_cooling = self._check_cooling_prediction(active_target, active_deadline, now)
        if active_cooling is None:
            return False

        self._target_temp = active_target
        self._target_time = "Active setpoint"
        self._is_tomorrow = False
        self._apply_cooling_preheat(active_cooling["crossover_dt"], now)
        return True

    def _calculate_preheat_from_block(self, next_block: NextScheduleBlock, manager: SmartComfortManager) -> None:
        """Calculate preheat timing from a schedule block with heating ON."""
        from datetime import timedelta

        ufh_buffer = self._get_ufh_buffer()
        cycle_heating_rate, cycle_confidence = self._resolve_cycle_heating_rate()

        # Use HeatingCycleCoordinator data if available
        if cycle_heating_rate is not None and cycle_heating_rate > _MIN_HEATING_RATE:
            temp_diff = self._target_temp - self._current_temp  # type: ignore[operator]
            minutes_needed = min(int((temp_diff / cycle_heating_rate) * 60) + ufh_buffer, 240)
            recommended_start = next_block.start_time - timedelta(minutes=minutes_needed)
            self._apply_preheat_timing(
                recommended_start, minutes_needed, cycle_heating_rate,
                cycle_confidence, ufh_buffer,  # type: ignore[arg-type]
            )
            return

        # Fallback to SmartComfortManager
        if self._target_temp is None:
            return
        advice = manager.get_preheat_advice(
            self._zone_id, self._target_temp, next_block.start_time, self._current_temp,
        )

        if advice is None:
            temp_diff = self._target_temp - self._current_temp  # type: ignore[operator]
            self._set_simple_status(
                "Insufficient data", confidence="insufficient_data",
                summary=f"Need +{temp_diff:.1f}°C by {self._target_time} (no heating history)",
                target_temp=self._target_temp, target_time=self._target_time,
            )
            return

        # Valid preheat recommendation, apply UFH buffer
        adjusted_duration = min(advice.estimated_duration_minutes + ufh_buffer, 240)
        adjusted_start = next_block.start_time - timedelta(minutes=adjusted_duration)

        now = dt_util.now()
        self._is_tomorrow = adjusted_start.date() > now.date()
        time_str = adjusted_start.strftime("%H:%M")
        self._attr_native_value = f"Tomorrow {time_str}" if self._is_tomorrow else time_str
        self._duration_minutes = adjusted_duration
        self._heating_rate = advice.heating_rate
        self._confidence = advice.confidence
        self._cooling_rate = None
        self._predicted_crossover_time = None
        self._is_cooling_prediction = False
        self._summary = advice.to_summary()
        if ufh_buffer > 0:
            self._summary += f" (includes {ufh_buffer} min UFH buffer)"
        self._data_present = True

    def _handle_schedule_block(self, next_block: NextScheduleBlock, manager: SmartComfortManager) -> None:
        """Handle a schedule block: heating OFF, already at target, or preheat needed."""
        # Next block is heating OFF
        if not next_block.is_heating_on or next_block.target_temp is None:
            now = dt_util.now()
            self._is_tomorrow = next_block.start_time.date() > now.date()
            time_str = next_block.start_time.strftime("%H:%M")
            target_time = f"Tomorrow {time_str}" if self._is_tomorrow else time_str
            self._set_simple_status(
                "Heating OFF", confidence="high",
                summary=f"Heating turns OFF at {target_time}",
                target_time=target_time, duration_minutes=0,
            )
            return

        self._target_temp = next_block.target_temp
        self._is_tomorrow = next_block.start_time.date() > dt_util.now().date()
        time_str = next_block.start_time.strftime("%H:%M")
        self._target_time = f"Tomorrow {time_str}" if self._is_tomorrow else time_str

        # Already at or above target
        if self._current_temp is not None and self._current_temp >= self._target_temp:
            cooling_info = self._check_cooling_prediction(
                self._target_temp, next_block.start_time, dt_util.now(),
            )
            if cooling_info is None:
                self._set_simple_status(
                    "Ready", confidence="high",
                    summary=f"Already at {self._target_temp:.1f}\u00b0C (no preheat needed)",
                    target_temp=self._target_temp, target_time=self._target_time,
                )
                return
            self._apply_cooling_preheat(cooling_info["crossover_dt"], dt_util.now())
            return

        # Need to preheat
        self._calculate_preheat_from_block(next_block, manager)

    @callback
    def update(self) -> None:
        """Update preheat advice based on schedule and heating rate."""
        try:
            from .smart_comfort import get_next_schedule_change

            manager = self.coordinator.smart_comfort_manager if self.hass else None

            if not manager or not manager.is_enabled:
                self._data_present = False
                return

            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            sensor_data = zone_data.get("sensorDataPoints") or {}
            self._current_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")

            if self._current_temp is None:
                self._data_present = False
                return

            # Suppress preheat when home is in AWAY mode
            home_state = (self.coordinator.data or {}).get("home_state")
            if home_state and home_state.get("presence") != "HOME":
                self._set_simple_status(
                    "Away", confidence="away_mode",
                    summary="Preheat suppressed: home is in away mode",
                )
                return

            # Check cooling prediction against CURRENT active target
            if self._check_active_cooling_preheat(zone_data):
                return

            # Get next schedule change
            _dl = self.coordinator.data_loader
            next_block = get_next_schedule_change(self._zone_id, data_loader=_dl)

            if next_block is None:
                self._set_simple_status(
                    "No schedule", confidence="no_schedule",
                    summary="No upcoming schedule changes today",
                )
                return

            self._handle_schedule_block(next_block, manager)

        except Exception as e:
            _LOGGER.debug(
                "Smart Comfort Sensor: zone %s preheat advisor update "
                "failed (%s), marking unavailable until the next poll",
                self._zone_id, e,
            )
            self._data_present = False
        finally:
            # (used by TadoPreheatNowSensor and insight collector)
            if self._data_present:
                self.coordinator.publish_entity_data(
                    self._zone_id,
                    ENTITY_DATA_PREHEAT_ADVISOR,
                    {
                        "state": str(self._attr_native_value) if self._attr_native_value else None,
                        "target_time": self._target_time,
                        "target_temperature": self._target_temp,
                        "current_temperature": self._current_temp,
                        "duration_minutes": self._duration_minutes,
                        "confidence": self._confidence,
                        "is_tomorrow": self._is_tomorrow,
                        "cooling_rate": self._cooling_rate,
                        "predicted_crossover_time": self._predicted_crossover_time,
                        "is_cooling_prediction": self._is_cooling_prediction,
                    },
                )


    def _apply_cooling_preheat(self, crossover_dt: datetime, now: datetime) -> None:
        """Apply cooling-based preheat timing (inertia + UFH buffer before crossover).

        A usable heating rate is required as a gate (without one, only the
        crossover warning is shown), but the preheat buffer itself is
        inertia_minutes + UFH buffer; the heating rate is not part of the math.
        """
        from datetime import timedelta

        heating_rate = None
        inertia_minutes = 0

        heating_cycle_coordinator = self.coordinator.heating_cycle_coordinator
        if heating_cycle_coordinator:
            zone_data_cycle = heating_cycle_coordinator.get_zone_data(self._zone_id)
            if zone_data_cycle:
                heating_rate = zone_data_cycle.get("heating_rate")
                inertia_time = zone_data_cycle.get("inertia_time")
                if inertia_time is not None:
                    inertia_minutes = int(inertia_time)

        if heating_rate is None or heating_rate <= _MIN_HEATING_RATE:
            manager = self.coordinator.smart_comfort_manager
            if manager and manager.get_heating_rate(self._zone_id):
                heating_rate = manager.get_heating_rate(self._zone_id)

        if heating_rate is None or heating_rate <= _MIN_HEATING_RATE:
            # No heating rate: show crossover warning only
            self._attr_native_value = crossover_dt.strftime("%H:%M")
            self._data_present = True
            self._duration_minutes = None
            self._heating_rate = None
            self._confidence = "low"
            self._summary = (
                f"Cooling at {self._cooling_rate:.1f}\u00b0C/h, "
                f"will cross {self._target_temp:.1f}\u00b0C at {self._predicted_crossover_time} "
                f"(no heating rate data for preheat timing)"
            )
            return

        # Calculate preheat duration (inertia + UFH buffer)
        ufh_buffer = 0
        zone_config_mgr = self.coordinator.zone_config_manager
        if zone_config_mgr:
            heating_type = zone_config_mgr.get_zone_value(self._zone_id, "heating_type", "radiator")
            if heating_type == "ufh":
                ufh_buffer = zone_config_mgr.get_zone_value(self._zone_id, "ufh_buffer_minutes", 30)

        total_buffer = min(inertia_minutes + ufh_buffer, 240)
        preheat_start = crossover_dt - timedelta(minutes=total_buffer)

        preheat_start = max(now, preheat_start)

        self._is_tomorrow = preheat_start.date() > now.date()
        time_str = preheat_start.strftime("%H:%M")
        self._attr_native_value = f"Tomorrow {time_str}" if self._is_tomorrow else time_str
        self._duration_minutes = total_buffer
        self._heating_rate = heating_rate
        self._confidence = "medium"
        self._data_present = True
        self._summary = (
            f"Cooling at {self._cooling_rate:.1f}\u00b0C/h, "
            f"will cross {self._target_temp:.1f}\u00b0C at {self._predicted_crossover_time}, "
            f"start preheat at {self._attr_native_value}"
        )
        if ufh_buffer > 0:
            self._summary += f" (includes {ufh_buffer} min UFH buffer)"

    def _check_cooling_prediction(
        self,
        target_temp: float,
        deadline: datetime,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Check if cooling rate predicts temperature will cross target before deadline."""
        from datetime import timedelta

        manager = self.coordinator.smart_comfort_manager
        if not manager:
            return None

        cooling_rate = manager.get_cooling_rate(self._zone_id)

        # No data or stable temperature
        if cooling_rate is None or cooling_rate >= _MIN_COOLING_RATE:
            return None

        # Clamp extreme rates (°C/h floor, matches calculations.COOLING_RATE_MIN)
        cooling_rate = max(cooling_rate, -5.0)

        if self._current_temp is None:
            return None

        hours_to_crossover = estimate_cooling_crossover(
            self._current_temp, target_temp, cooling_rate,
        )
        if hours_to_crossover is None:
            return None

        crossover_dt = now + timedelta(hours=hours_to_crossover)

        # 30-minute buffer: if crossover is well after deadline, no concern
        schedule_deadline = deadline + timedelta(minutes=30)
        if crossover_dt > schedule_deadline:
            return None

        # Cooling prediction is relevant
        self._cooling_rate = cooling_rate
        self._predicted_crossover_time = crossover_dt.strftime("%H:%M")
        self._is_cooling_prediction = True

        return {
            "crossover_dt": crossover_dt,
            "cooling_rate": cooling_rate,
            "hours_to_crossover": hours_to_crossover,
        }

class TadoSmartComfortTargetSensor(TadoZoneSensor):
    """Smart Comfort Target Temperature sensor (ASHRAE 55: 0.31×outdoor + 17.8°C, seasonal fallback)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_name: str, zone_type: str = "HEATING",
    ) -> None:
        """Initialize the Smart Comfort Target Sensor."""
        super().__init__(coordinator, zone_id, zone_name, zone_type)
        _meta = ENTITY_REGISTRY["sensor_comfort_target"]
        self._attr_translation_key = _meta.translation_key
        self._attr_unique_id = f"tado_ce_{coordinator.home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_native_unit_of_measurement = "°C"
        self._attr_icon = _meta.icon
        self._attr_entity_category = get_entity_category(_meta)
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Attributes
        self._current_temp: float | None = None
        self._outdoor_temp: float | None = None
        self._humidity: float | None = None
        self._comfort_model: str = "unknown"
        self._deviation: float | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        return {
            "current_temperature": self._current_temp,
            "outdoor_temperature": self._outdoor_temp,
            "humidity": self._humidity,
            "comfort_model": _format_comfort_model(self._comfort_model),
            "deviation_from_comfort": self._deviation,
            **self._base_zone_attributes,
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon based on deviation from comfort."""
        if self._deviation is None:
            return "mdi:thermometer-auto"
        if self._deviation < _DEVIATION_ICON_COLD:
            return "mdi:thermometer-low"  # Too cold
        if self._deviation > _DEVIATION_ICON_HOT:
            return "mdi:thermometer-high"  # Too hot
        return "mdi:thermometer-check"  # Comfortable

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update()
        self.async_write_ha_state()

    @callback
    def update(self) -> None:
        """Update Smart Comfort target using ASHRAE 55 Adaptive Comfort Model."""
        try:
            if not self.hass:
                self._data_present = False
                return

            # Get config_manager from coordinator (real-time config access)
            config_manager = self.coordinator.config_manager
            if not config_manager:
                self._data_present = False
                return

            # Get zone data
            zone_data = self._get_zone_data()
            if not zone_data:
                self._data_present = False
                return

            sensor_data = zone_data.get("sensorDataPoints") or {}
            inside_temp = sensor_data.get("insideTemperature") or {}
            self._current_temp = inside_temp.get("celsius")

            # Get humidity
            humidity_data = sensor_data.get("humidity") or {}
            self._humidity = humidity_data.get("percentage")

            # Get outdoor temperature
            outdoor_entity = config_manager.get_outdoor_temp_entity()
            self._outdoor_temp = _get_outdoor_temp(self.hass, outdoor_entity, config_manager.get_use_feels_like())

            # Calculate comfort target using ASHRAE 55 or seasonal fallback
            comfort_target = self._calculate_comfort_target()

            if comfort_target is None:
                self._data_present = False
                return

            # Round to 0.5°C (Tado's precision)
            comfort_target = round(comfort_target * 2) / 2

            # Calculate deviation from comfort
            if self._current_temp is not None:
                self._deviation = round(self._current_temp - comfort_target, 1)
            else:
                self._deviation = None

            self._attr_native_value = comfort_target
            self._data_present = True

        except Exception as e:
            _LOGGER.debug(
                "Smart Comfort Sensor: zone %s comfort target update "
                "failed (%s), marking unavailable until the next poll",
                self._zone_id, e,
            )
            self._data_present = False

    def _calculate_comfort_target(self) -> float | None:
        """Calculate comfort target using ASHRAE 55 or seasonal fallback."""
        # Method 1: ASHRAE 55 Adaptive Comfort Model (if outdoor temp available)
        if self._outdoor_temp is not None:
            self._comfort_model = "adaptive"
            return calculate_ashrae_comfort_temp(self._outdoor_temp)

        # Method 2: Seasonal fallback based on latitude
        self._comfort_model = "seasonal"
        return self._get_seasonal_comfort_target()

    def _get_seasonal_comfort_target(self) -> float:
        """Get comfort target based on season and latitude."""
        latitude = 51.5  # Default to London
        if self.hass and hasattr(self.hass.config, "latitude"):
            latitude = self.hass.config.latitude or 51.5

        day_of_year = dt_util.now().timetuple().tm_yday

        return calculate_seasonal_comfort_target(latitude, day_of_year)
