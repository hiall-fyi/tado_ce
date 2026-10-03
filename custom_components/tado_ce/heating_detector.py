"""Tado CE heating-cycle detector: per-zone state machine delimiting one heating cycle (start → inertia → target/timeout)."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.util import dt as dt_util

from .heating_models import HeatingCycle, HeatingCycleConfig, HeatingCycleReading

_LOGGER = logging.getLogger(__name__)

# Maximum cycle duration before timeout
MAX_CYCLE_DURATION = timedelta(hours=6)


class HeatingCycleDetector:
    """Detect heating cycle start, end, and interruptions for a single zone."""

    def __init__(self, zone_id: str, config: HeatingCycleConfig) -> None:
        self._zone_id = zone_id
        self._config = config
        self._active_cycle: HeatingCycle | None = None
        self._last_target_temp: float | None = None

    def check_setpoint_change(
        self,
        new_target: float,
        timestamp: datetime,
        current_temp: float | None = None,
    ) -> bool:
        """Check if setpoint increased (potential cycle start); returns True when a cycle was started."""
        _LOGGER.debug(
            "Heating Detector: zone %s check_setpoint_change, "
            "last_target=%s, new_target=%.1f, current_temp=%s",
            self._zone_id,
            self._last_target_temp,
            new_target,
            current_temp,
        )

        # First time initialization after HA restart
        if self._last_target_temp is None:
            self._last_target_temp = new_target

            # If current_temp is provided and below target,
            # zone is already heating - start a cycle
            if current_temp is not None and current_temp < new_target - 0.1:
                _LOGGER.info(
                    "Heating Detector: zone %s active heating "
                    "detected after restart (current %.1f°C < "
                    "target %.1f°C), starting cycle",
                    self._zone_id,
                    current_temp,
                    new_target,
                )
                self._active_cycle = HeatingCycle(
                    zone_id=self._zone_id,
                    start_time=timestamp,
                    end_time=None,
                    start_temp=current_temp,
                    target_temp=new_target,
                    first_rise_time=None,
                    first_rise_temp=None,
                    temperature_readings=[HeatingCycleReading(time=timestamp, temp=current_temp)],
                    completed=False,
                    interrupted=False,
                    interrupt_reason=None,
                )
                return True
            return False

        if new_target > self._last_target_temp:
            _LOGGER.debug(
                "Heating Detector: zone %s setpoint raised %.1f → "
                "%.1f°C, starting a new cycle",
                self._zone_id,
                self._last_target_temp,
                new_target,
            )
            if self._active_cycle:
                # Mark the previous cycle as interrupted:
                # downstream analyser ignores interrupted cycles.
                self._active_cycle.interrupted = True
                self._active_cycle.interrupt_reason = "manual_setpoint_change"
                _LOGGER.debug(
                    "Heating Detector: zone %s interrupted the "
                    "previous active cycle (setpoint change)",
                    self._zone_id,
                )

            # Start new cycle
            self._active_cycle = HeatingCycle(
                zone_id=self._zone_id,
                start_time=timestamp,
                end_time=None,
                start_temp=None,  # Will be set on first temp update
                target_temp=new_target,
                first_rise_time=None,
                first_rise_temp=None,
                temperature_readings=[],
                completed=False,
                interrupted=False,
                interrupt_reason=None,
            )
            self._last_target_temp = new_target

            _LOGGER.info(
                "Heating Detector: zone %s started a new cycle, "
                "target %.1f°C",
                self._zone_id,
                new_target,
            )
            return True

        self._last_target_temp = new_target
        return False

    def on_temperature_update(self, temp: float, timestamp: datetime) -> None:
        """Process temperature update."""
        if not self._active_cycle:
            return

        if self._active_cycle.start_temp is None:
            self._active_cycle.start_temp = temp
            _LOGGER.debug(
                "Heating Detector: zone %s cycle start_temp set to "
                "%.1f°C",
                self._zone_id,
                temp,
            )

        # Deduplicate: HomeKit events and cloud polls can land
        # within the same second, creating duplicate timestamps
        # that distort rate calculations. Update the last reading
        # in-place when the gap is < 2s.
        if self._active_cycle.temperature_readings:
            last = self._active_cycle.temperature_readings[-1]
            if abs((timestamp - last.time).total_seconds()) < 2:
                self._active_cycle.temperature_readings[-1] = HeatingCycleReading(
                    time=timestamp, temp=temp,
                )
                return

        if len(self._active_cycle.temperature_readings) < 100:
            self._active_cycle.temperature_readings.append(
                HeatingCycleReading(time=timestamp, temp=temp),
            )
        else:
            # Cap at 100 readings to bound memory; once full,
            # replace the last reading rather than dropping new
            # data, which keeps the most recent point fresh.
            self._active_cycle.temperature_readings[-1] = HeatingCycleReading(
                time=timestamp,
                temp=temp,
            )

        # Inertia / first-rise detection: once the temperature
        # has climbed by `inertia_threshold_celsius`, record the
        # timestamp so the analyser can compute lag.
        if self._active_cycle.first_rise_time is None and self._active_cycle.start_temp is not None:
            temp_increase = temp - self._active_cycle.start_temp
            if temp_increase >= self._config.inertia_threshold_celsius:
                self._active_cycle.first_rise_time = timestamp
                self._active_cycle.first_rise_temp = temp
                _LOGGER.debug(
                    "Heating Detector: zone %s first rise observed "
                    "at %.1f°C (+%.2f°C from start)",
                    self._zone_id,
                    temp,
                    temp_increase,
                )

    def check_cycle_complete(self) -> HeatingCycle | None:
        """Check if active cycle is complete; returns the completed cycle when target is reached, None otherwise."""
        if not self._active_cycle:
            return None

        if not self._active_cycle.temperature_readings:
            return None

        current_temp = self._active_cycle.temperature_readings[-1].temp
        if current_temp >= self._active_cycle.target_temp:
            # Target reached
            self._active_cycle.end_time = dt_util.utcnow()

            # Validate: Only mark as completed if there was actual heating
            # (start_temp < target_temp and meaningful temperature rise)
            start_temp = self._active_cycle.start_temp
            target_temp = self._active_cycle.target_temp

            if start_temp is not None and start_temp < target_temp - 0.1:
                # Valid heating cycle - temperature actually needed to rise
                self._active_cycle.completed = True
                completed = self._active_cycle
                self._active_cycle = None

                _LOGGER.info(
                    "Heating Detector: zone %s cycle completed, "
                    "duration %.1f min, %.1f°C → %.1f°C",
                    self._zone_id,
                    (completed.end_time - completed.start_time).total_seconds() / 60,  # type: ignore[operator]
                    start_temp,
                    target_temp,
                )
                return completed
            _LOGGER.debug(
                "Heating Detector: zone %s discarding cycle, "
                "start_temp (%.1f°C) was already at or above target "
                "(%.1f°C), no actual heating occurred",
                self._zone_id,
                start_temp or 0,
                target_temp,
            )
            self._active_cycle = None
            return None

        return None

    def check_cycle_timeout(self) -> bool:
        """Check if active cycle has timed out; returns True when timed out and cycle was closed."""
        if not self._active_cycle:
            return False

        age = dt_util.utcnow() - self._active_cycle.start_time
        if age > MAX_CYCLE_DURATION:
            self._active_cycle.interrupted = True
            self._active_cycle.interrupt_reason = "timeout"
            self._active_cycle.end_time = dt_util.utcnow()

            _LOGGER.debug(
                "Heating Detector: zone %s cycle timed out after "
                "%.1f hour(s), marking interrupted, expected target "
                "never reached",
                self._zone_id,
                age.total_seconds() / 3600,
            )

            self._active_cycle = None
            return True

        return False

    def resume_cycle(self, cycle: HeatingCycle) -> None:
        """Re-attach an in-progress cycle after HA restart so analysis continues."""
        self._active_cycle = cycle
        self._last_target_temp = cycle.target_temp

        _LOGGER.info(
            "Heating Detector: zone %s resumed in-progress cycle "
            "started %s",
            self._zone_id,
            cycle.start_time.isoformat(),
        )

    def get_active_cycle(self) -> HeatingCycle | None:
        """Get currently active cycle."""
        return self._active_cycle
