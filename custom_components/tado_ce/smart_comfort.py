"""Tado CE Smart Comfort: per-zone heating-rate analytics and preheat advice (3-tier load: cache → recorder → statistics)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import (
    WEATHER_COMPENSATION_PRESETS,
)
from .models import SmartComfortReading

try:
    from .sensor_helpers import get_outdoor_temperature as _get_outdoor_temp
except (ImportError, ModuleNotFoundError):
    # Fallback for isolated test loading via _load_module() with synthetic package
    # names (e.g. "_test_sc_.smart_comfort").  sensor_helpers has no relative
    # imports so it can be loaded standalone.
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _sh_path = str(_Path(__file__).resolve().parent / "sensor_helpers.py")
    _sh_spec = _ilu.spec_from_file_location("_isolated_sensor_helpers", _sh_path)
    _sh_mod = _ilu.module_from_spec(_sh_spec)  # type: ignore[arg-type]
    _sh_spec.loader.exec_module(_sh_mod)  # type: ignore[union-attr]
    _get_outdoor_temp = _sh_mod.get_outdoor_temperature

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)

# Configuration
DEFAULT_HISTORY_DAYS = 7  # Default: keep 7 days of history
RECORDER_HISTORY_HOURS = 24  # Load 24 hours from recorder on startup
MIN_DATA_POINTS = 3  # Minimum points needed for rate calculation
MIN_TIME_SPAN_MINUTES = 15  # Minimum time span for meaningful rate
CACHE_SAVE_INTERVAL_MINUTES = 15  # Save cache every 15 minutes

# Smart Comfort thresholds
TEMP_EPSILON = 0.05  # °C: minimum temperature difference to consider meaningful
TEMP_NEAR_TARGET = 0.1  # °C: close enough to target temperature
DEVIATION_NORMAL_THRESHOLD = 0.3  # °C: deviation below this is "Normal"
MIN_RATE_THRESHOLD = 0.01  # °C/h: minimum meaningful heating/cooling rate
DEDUP_TIME_WINDOW = 300  # seconds (5 min): skip duplicate readings within this window
SEGMENT_MAX_GAP_HOURS = 2  # hours: max gap between readings for continuous segment
SEGMENT_MIN_TEMP_RISE = 0.05  # °C: minimum rise to count as heating
SEGMENT_MIN_TIME_HOURS = 0.01  # hours: minimum time for rate calculation
RATE_MIN_VALID = 0.1  # °C/h: minimum valid heating rate
RATE_MAX_VALID = 10.0  # °C/h: maximum valid heating rate (TRV can be 5-8°C/h)
REGRESSION_DENOMINATOR_EPSILON = 0.0001  # minimum denominator for linear regression
MIDNIGHT_WRAPAROUND_MINUTES = 720  # 12 hours: threshold for midnight wraparound
CONFIDENCE_HIGH_READINGS = 10  # readings needed for high confidence
CONFIDENCE_MEDIUM_READINGS = 5  # readings needed for medium confidence
BASELINE_CHANGE_THRESHOLD = 0.05  # °C/h: minimum change for baseline calculation

from .schedule_helpers import _get_day_blocks


@dataclass
class NextScheduleBlock:
    """Next scheduled temperature change for a zone."""

    start_time: datetime
    target_temp: float | None
    is_heating_on: bool
    block_end_time: datetime

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return {
            "start_time": self.start_time.isoformat(),
            "target_temp": self.target_temp,
            "is_heating_on": self.is_heating_on,
            "block_end_time": self.block_end_time.isoformat(),
        }


def _find_next_block_in_day(
    day_blocks: list[dict[str, Any]],
    current_time_str: str,
    check_date: datetime,
) -> NextScheduleBlock | None:
    """Return the first block on `check_date` that starts after `current_time_str`."""
    for block in day_blocks:
        block_start = block.get("start", "00:00")
        if block_start <= current_time_str:
            continue

        setting = block.get("setting") or {}
        power = setting.get("power", "OFF")
        temp_data = setting.get("temperature")
        block_end = block.get("end", "00:00")

        # Parse start time into datetime
        start_hour, start_min = map(int, block_start.split(":"))
        start_datetime = check_date.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)

        # Parse end time
        end_hour, end_min = map(int, block_end.split(":"))
        if block_end == "00:00":
            end_datetime = (check_date + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            end_datetime = check_date.replace(hour=end_hour, minute=end_min, second=0, microsecond=0)

        target_temp = None
        if power == "ON" and temp_data:
            target_temp = temp_data.get("celsius")

        return NextScheduleBlock(
            start_time=start_datetime,
            target_temp=target_temp,
            is_heating_on=(power == "ON"),
            block_end_time=end_datetime,
        )
    return None


def get_next_schedule_change(
    zone_id: str,
    current_time: datetime | None = None,
    look_ahead_days: int = 2,
    data_loader: DataLoader | None = None,
) -> NextScheduleBlock | None:
    """Find the next upcoming schedule block (heating-on or target rise).

    Looks at today first, then forward up to `look_ahead_days` so a
    morning preheat advisor finds tomorrow's first block when nothing
    remains today.
    """
    schedule = data_loader.get_zone_schedule(zone_id) if data_loader is not None else None

    if current_time is None:
        try:
            current_time = dt_util.now()
        except ImportError:
            current_time = datetime.now(UTC)

    if not schedule:
        _LOGGER.debug(
            "Smart Comfort: zone %s has no schedule loaded, cannot find next block",
            zone_id,
        )
        return None

    blocks = schedule.get("blocks") or {}
    schedule_type = schedule.get("type", "ONE_DAY")

    for day_offset in range(look_ahead_days):
        check_date = current_time + timedelta(days=day_offset)
        day_blocks = _get_day_blocks(blocks, schedule_type, check_date.weekday())
        if not day_blocks:
            continue

        time_str = current_time.strftime("%H:%M") if day_offset == 0 else "00:00"
        result = _find_next_block_in_day(day_blocks, time_str, check_date)
        if result:
            return result

    _LOGGER.debug(
        "Smart Comfort: zone %s has no schedule blocks in the next %d day(s)",
        zone_id, look_ahead_days,
    )
    return None




@dataclass
class HistoricalComparison:
    """Result of comparing current temperature to a historical window."""

    current_temp: float
    historical_avg: float
    difference: float
    sample_count: int
    comparison_window_minutes: int

    def to_summary(self) -> str:
        """Return a one-line user-facing description of the comparison."""
        if abs(self.difference) < DEVIATION_NORMAL_THRESHOLD:
            return f"Normal (avg {self.historical_avg:.1f}°C)"
        if self.difference > 0:
            return f"{self.difference:+.1f}°C warmer than usual"
        return f"{self.difference:.1f}°C cooler than usual"


@dataclass
class PreheatAdvice:
    """Recommended start time so heating reaches the target by `target_time`."""

    recommended_start_time: datetime
    target_time: datetime
    target_temp: float
    current_temp: float
    estimated_duration_minutes: int
    heating_rate: float
    confidence: str

    def to_summary(self) -> str:
        """Return a one-line user-facing recommendation."""
        if self.estimated_duration_minutes == 0:
            return f"Already at {self.target_temp:.1f}°C (no preheat needed)"
        start_str = self.recommended_start_time.strftime("%H:%M")
        return f"Start at {start_str} ({self.estimated_duration_minutes} min to reach {self.target_temp:.1f}°C)"


class ZoneHistory:
    """Rolling temperature history and rate calculation for one zone."""

    def __init__(self, zone_id: str, zone_name: str, history_days: int = DEFAULT_HISTORY_DAYS) -> None:
        """Initialise the zone's history tracker."""
        self.zone_id = zone_id
        self.zone_name = zone_name
        self.readings: list[SmartComfortReading] = []
        self._history_days = history_days
        self._last_heating_rate: float | None = None
        self._last_cooling_rate: float | None = None
        self._rate_updated_at: datetime | None = None
        # Baseline rates from long-term statistics (the third tier of the
        # 3-tier load strategy: cache → recorder → statistics).
        self._baseline_heating_rate: float | None = None
        self._baseline_cooling_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return {
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "readings": [r.to_dict() for r in self.readings],
            "history_days": self._history_days,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoneHistory:
        """Restore from a serialised dict, deduplicating any near-duplicate readings."""
        history_days = data.get("history_days", DEFAULT_HISTORY_DAYS)
        zone = cls(data["zone_id"], data["zone_name"], history_days)

        # Load and deduplicate readings
        raw_readings = [SmartComfortReading.from_dict(r) for r in data.get("readings", [])]

        raw_readings.sort(key=lambda r: r.timestamp)

        # Deduplicate: skip if same temp/heating and < 5 min apart
        deduplicated: list[Any] = []
        for reading in raw_readings:
            if deduplicated:
                last = deduplicated[-1]
                time_diff = (reading.timestamp - last.timestamp).total_seconds()
                # Skip duplicate: same temp, same heating state, < 5 min
                if (
                    abs(reading.temperature - last.temperature) < TEMP_EPSILON
                    and reading.is_heating == last.is_heating
                    and time_diff < DEDUP_TIME_WINDOW
                ):
                    continue
            deduplicated.append(reading)

        zone.readings = deduplicated
        return zone

    def set_history_days(self, days: int) -> None:
        """Set the retention window and immediately prune older readings."""
        self._history_days = days
        self._prune_old_readings()

    def add_reading(self, reading: SmartComfortReading) -> None:
        """Append a reading, deduplicating same-state samples within 5 minutes.

        Frequent polling would otherwise bloat the cache with identical
        rows; the dedup check keeps real transitions and time-spaced
        snapshots.
        """
        if self.readings:
            last = self.readings[-1]
            time_diff = (reading.timestamp - last.timestamp).total_seconds()

            if (
                abs(reading.temperature - last.temperature) < TEMP_EPSILON
                and reading.is_heating == last.is_heating
                and time_diff < DEDUP_TIME_WINDOW
            ):
                return

        self.readings.append(reading)
        self._prune_old_readings()

    def _prune_old_readings(self) -> None:
        """Drop readings older than the retention window."""
        cutoff = dt_util.utcnow() - timedelta(days=self._history_days)
        self.readings = [r for r in self.readings if r.timestamp > cutoff]

    def get_heating_rate(self) -> float | None:
        """Return the zone's heating rate in °C/h, or None when uncertain.

        Tries three strategies in order: rising segments from
        is_heating=True readings, rising segments from all readings
        (covers HA-automation setups where is_heating stays False), and
        the long-term baseline. Picks the first that yields a rate
        above MIN_RATE_THRESHOLD.
        """
        heating_readings = [r for r in self.readings if r.is_heating]
        rate = self._calculate_heating_rate_segments(heating_readings)

        if rate is not None and rate > MIN_RATE_THRESHOLD:
            self._last_heating_rate = rate
            self._rate_updated_at = dt_util.utcnow()
            return rate

        if len(heating_readings) == 0 and len(self.readings) >= MIN_DATA_POINTS:
            rate = self._calculate_heating_rate_segments(self.readings)
            if rate is not None and rate > MIN_RATE_THRESHOLD:
                self._last_heating_rate = rate
                self._rate_updated_at = dt_util.utcnow()
                return rate

        if self._baseline_heating_rate is not None:
            return self._baseline_heating_rate

        return None

    def get_cooling_rate(self) -> float | None:
        """Return the zone's cooling/heat-loss rate in °C/h, or None when uncertain.

        Linear regression over is_heating=False readings, clamped to ≤ 0
        because heat loss cannot cause a temperature rise (a positive
        slope means sensor lag or an external heat source like sun).
        Falls back to the long-term baseline when no real-time rate is
        available.
        """
        cooling_readings = [r for r in self.readings if not r.is_heating]
        rate = self._calculate_rate(cooling_readings)

        if rate is not None and rate > 0:
            rate = 0.0

        if rate is None and self._baseline_cooling_rate is not None:
            return self._baseline_cooling_rate

        return rate

    def _calculate_heating_rate_segments(self, readings: list[SmartComfortReading]) -> float | None:
        """Return the average rate of rising-temperature segments, in °C/h.

        Linear regression over all heating readings would be skewed by
        flat post-target periods. Segmenting on actual rises avoids
        that. Returns None if no valid segment fits the
        RATE_MIN_VALID..RATE_MAX_VALID window.
        """
        if len(readings) < MIN_DATA_POINTS:
            return None

        sorted_readings = sorted(readings, key=lambda r: r.timestamp)

        deduped = []
        last_ts = None
        for r in sorted_readings:
            if last_ts is None or r.timestamp != last_ts:
                deduped.append(r)
                last_ts = r.timestamp

        if len(deduped) < MIN_DATA_POINTS:
            return None

        segment_rates = []

        for i in range(1, len(deduped)):
            prev = deduped[i - 1]
            curr = deduped[i]

            time_diff_hours = (curr.timestamp - prev.timestamp).total_seconds() / 3600
            temp_diff = curr.temperature - prev.temperature

            # Gap > 2h breaks segment continuity, likely overnight idle
            # or sensor outage rather than a real heating cycle.
            if time_diff_hours > SEGMENT_MAX_GAP_HOURS:
                continue

            if temp_diff > SEGMENT_MIN_TEMP_RISE and time_diff_hours > SEGMENT_MIN_TIME_HOURS:
                rate = temp_diff / time_diff_hours
                # TRV starting from cold can hit 5-8 °C/h, so the upper
                # bound is 10. Anything beyond is sensor noise.
                if RATE_MIN_VALID <= rate <= RATE_MAX_VALID:
                    segment_rates.append(rate)

        if not segment_rates:
            return None

        avg_rate = sum(segment_rates) / len(segment_rates)
        return round(avg_rate, 2)

    def _calculate_rate(self, readings: list[SmartComfortReading]) -> float | None:
        """Return the linear-regression slope of the readings in °C/h, or None."""
        if len(readings) < MIN_DATA_POINTS:
            return None

        time_span = (readings[-1].timestamp - readings[0].timestamp).total_seconds()
        if time_span < MIN_TIME_SPAN_MINUTES * 60:
            return None

        # Simple linear regression: x = hours since the first reading,
        # y = temperature, slope = °C/h.
        n = len(readings)
        base_time = readings[0].timestamp

        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0

        for r in readings:
            x = (r.timestamp - base_time).total_seconds() / 3600
            y = r.temperature
            sum_x += x
            sum_y += y
            sum_xy += x * y
            sum_x2 += x * x

        denominator = n * sum_x2 - sum_x * sum_x
        if abs(denominator) < REGRESSION_DENOMINATOR_EPSILON:
            return None

        slope = (n * sum_xy - sum_x * sum_y) / denominator

        return round(slope, 2)

    def get_time_to_target(self, current_temp: float, target_temp: float, zone_type: str = "HEATING") -> int | None:
        """Estimate minutes to reach target_temp, capped at 8 hours.

        Returns 0 when already at target. Uses heating rate for HEATING
        zones and cooling rate for AIR_CONDITIONING. Returns None when
        no rate is available.
        """
        diff = target_temp - current_temp

        if abs(diff) < TEMP_NEAR_TARGET:
            return 0

        if zone_type == "HEATING":
            if diff <= 0:
                return 0
            rate = self.get_heating_rate()
        else:
            if diff >= 0:
                return 0
            rate = self.get_cooling_rate()

        if rate is None or abs(rate) < MIN_RATE_THRESHOLD:
            return None

        hours = abs(diff) / abs(rate)
        minutes = int(hours * 60)

        return min(minutes, 480)


    def get_historical_comparison(
        self,
        current_temp: float,
        comparison_window_minutes: int = 30,
    ) -> HistoricalComparison | None:
        """Compare `current_temp` against the historical average at this time of day.

        Looks at readings from prior days within ±comparison_window of
        the current minute. Returns None when fewer than two historical
        samples land in the window.
        """
        if len(self.readings) < MIN_DATA_POINTS:
            return None

        try:
            now = dt_util.now()
        except (ValueError, TypeError):
            now = datetime.now(UTC)
        current_time_minutes = now.hour * 60 + now.minute

        historical_temps = []
        cutoff = now - timedelta(days=self._history_days)

        for reading in self.readings:
            if reading.timestamp.date() == now.date():
                continue

            if reading.timestamp < cutoff:
                continue

            reading_time_minutes = reading.timestamp.hour * 60 + reading.timestamp.minute
            time_diff = abs(reading_time_minutes - current_time_minutes)

            # Wrap so 23:50 and 00:10 read as 20 minutes apart, not 23 h 40 m.
            if time_diff > MIDNIGHT_WRAPAROUND_MINUTES:
                time_diff = 1440 - time_diff

            if time_diff <= comparison_window_minutes:
                historical_temps.append(reading.temperature)

        if len(historical_temps) < 2:
            return None

        historical_avg = sum(historical_temps) / len(historical_temps)
        difference = current_temp - historical_avg

        return HistoricalComparison(
            current_temp=round(current_temp, 1),
            historical_avg=round(historical_avg, 1),
            difference=round(difference, 1),
            sample_count=len(historical_temps),
            comparison_window_minutes=comparison_window_minutes,
        )

    def get_preheat_advice(
        self,
        target_temp: float,
        target_time: datetime,
        current_temp: float | None = None,
    ) -> PreheatAdvice | None:
        """Recommend a preheat start time so the room hits target by `target_time`.

        Confidence is high / medium / low based on how many heating
        readings the rate was derived from. Returns a zero-duration
        advice when already at target. Falls back to the long-term
        baseline rate (low confidence) when no real-time rate exists.
        """
        if current_temp is None:
            if not self.readings:
                return None
            current_temp = self.readings[-1].temperature

        if current_temp >= target_temp:
            return PreheatAdvice(
                recommended_start_time=dt_util.utcnow(),
                target_time=target_time,
                target_temp=target_temp,
                current_temp=current_temp,
                estimated_duration_minutes=0,
                heating_rate=0,
                confidence="high",
            )

        heating_rate = self.get_heating_rate()

        heating_readings = [r for r in self.readings if r.is_heating]
        if heating_rate is None:
            if self._baseline_heating_rate is not None:
                heating_rate = self._baseline_heating_rate
                confidence = "low"
            else:
                return None
        elif len(heating_readings) >= CONFIDENCE_HIGH_READINGS:
            confidence = "high"
        elif len(heating_readings) >= CONFIDENCE_MEDIUM_READINGS:
            confidence = "medium"
        else:
            confidence = "low"

        if heating_rate <= MIN_RATE_THRESHOLD:
            return None

        temp_diff = target_temp - current_temp
        hours_needed = temp_diff / heating_rate
        minutes_needed = int(hours_needed * 60)

        # Cap so a low rate + cold room doesn't suggest a 6-hour
        # preheat. Anything beyond 4 hours is impractical and likely
        # masks a setup problem.
        minutes_needed = min(minutes_needed, 240)

        recommended_start = target_time - timedelta(minutes=minutes_needed)

        return PreheatAdvice(
            recommended_start_time=recommended_start,
            target_time=target_time,
            target_temp=target_temp,
            current_temp=current_temp,
            estimated_duration_minutes=minutes_needed,
            heating_rate=heating_rate,
            confidence=confidence,
        )


class SmartComfortManager:
    """Coordinates Smart Comfort analytics across every zone in one home."""

    def __init__(
        self, hass: HomeAssistant | None = None, home_id: str = "", history_days: int = DEFAULT_HISTORY_DAYS,
        data_loader: DataLoader | None = None,
    ) -> None:
        """Initialise the manager."""
        self._zones: dict[str, ZoneHistory] = {}
        self._enabled = False
        self._hass = hass
        self._home_id = home_id
        self._history_days = history_days
        self._data_loader = data_loader
        self._last_save_time: datetime | None = None
        # Weather compensation settings
        self._outdoor_temp_entity: str = ""
        self._weather_compensation: str = "none"
        self._use_feels_like: bool = False

    def set_history_days(self, days: int) -> None:
        """Update history retention for all zones to the new day count."""
        self._history_days = days
        for zone in self._zones.values():
            zone.set_history_days(days)
        _LOGGER.info(
            "Smart Comfort: history retention set to %d day(s)", days,
        )

    def save_to_file(self) -> bool:
        """Persist zone history through the DataLoader store (debounced)."""
        if not self._zones:
            return True

        if not self._data_loader:
            _LOGGER.debug(
                "Smart Comfort: no data loader available, cannot save cache",
            )
            return False

        try:
            data = {
                "saved_at": dt_util.utcnow().isoformat(),
                "history_days": self._history_days,
                "zones": {zone_id: zone.to_dict() for zone_id, zone in self._zones.items()},
            }

            self._data_loader.save_auxiliary("smart_comfort_cache", data)

            self._last_save_time = dt_util.utcnow()

            total_readings = sum(len(z.readings) for z in self._zones.values())
            _LOGGER.debug(
                "Smart Comfort: cached %d zone(s), %d reading(s) total",
                len(self._zones), total_readings,
            )
            return True

        except (OSError, ValueError) as e:
            _LOGGER.warning(
                "Smart Comfort: could not save history cache (%s), "
                "data preserved in memory, will retry on next save",
                e,
            )
            return False

    async def async_load(self) -> int:
        """Restore zone history from the DataLoader store, returning reading count."""
        if not self._data_loader:
            return 0

        try:
            data = await self._data_loader.async_load_auxiliary("smart_comfort_cache")
            if data is None:
                _LOGGER.debug(
                    "Smart Comfort: no cache file yet, starting fresh",
                )
                return 0

            if not isinstance(data, dict):
                _LOGGER.warning(
                    "Smart Comfort: history cache had unexpected format, "
                    "ignoring and starting fresh",
                )
                return 0

            zones_data = data.get("zones") or {}
            total_readings = 0

            for zone_id, zone_data in zones_data.items():
                try:
                    zone = ZoneHistory.from_dict(zone_data)
                except (KeyError, TypeError, ValueError):
                    _LOGGER.warning(
                        "Smart Comfort: skipping malformed cache record for zone %s",
                        zone_id,
                        exc_info=True,
                    )
                    continue
                zone.set_history_days(self._history_days)

                if zone.readings:
                    self._zones[zone_id] = zone
                    total_readings += len(zone.readings)

            saved_at = data.get("saved_at", "unknown")
            _LOGGER.info(
                "Smart Comfort: restored %d zone(s) with %d reading(s) "
                "from cache (last saved %s)",
                len(self._zones), total_readings, saved_at,
            )
            return total_readings

        except (OSError, ValueError) as e:
            _LOGGER.warning(
                "Smart Comfort: could not load history cache (%s), "
                "starting fresh, history will rebuild from new readings",
                e,
            )
            return 0


    def maybe_save(self) -> None:
        """Persist if at least CACHE_SAVE_INTERVAL_MINUTES have passed since last save."""
        if self._last_save_time is None:
            self.save_to_file()
            return

        elapsed = dt_util.utcnow() - self._last_save_time
        if elapsed.total_seconds() >= CACHE_SAVE_INTERVAL_MINUTES * 60:
            self.save_to_file()

    def configure_weather(
        self,
        outdoor_temp_entity: str = "",
        weather_compensation: str = "none",
        use_feels_like: bool = False,
    ) -> None:
        """Update the manager's weather-compensation settings."""
        self._outdoor_temp_entity = outdoor_temp_entity
        self._weather_compensation = weather_compensation
        self._use_feels_like = use_feels_like
        _LOGGER.info(
            "Smart Comfort: weather compensation configured "
            "(entity=%s, preset=%s, feels-like=%s)",
            outdoor_temp_entity,
            weather_compensation,
            use_feels_like,
        )

    def enable(self) -> None:
        """Turn Smart Comfort tracking on."""
        self._enabled = True
        _LOGGER.info("Smart Comfort: tracking enabled")

    def disable(self) -> None:
        """Turn Smart Comfort tracking off."""
        self._enabled = False
        _LOGGER.info("Smart Comfort: tracking disabled")

    @property
    def is_enabled(self) -> bool:
        """Return True when Smart Comfort tracking is active."""
        return self._enabled

    def get_zone(self, zone_id: str, zone_name: str = "") -> ZoneHistory:
        """Return the zone's history tracker, creating one on first access."""
        if zone_id not in self._zones:
            self._zones[zone_id] = ZoneHistory(zone_id, zone_name or f"Zone {zone_id}", self._history_days)
        return self._zones[zone_id]

    def record_temperature(
        self,
        zone_id: str,
        zone_name: str,
        temperature: float,
        is_heating: bool,
        target_temperature: float | None = None,
    ) -> None:
        """Record a temperature reading for one zone (called on each state update)."""
        if not self._enabled:
            return

        zone = self.get_zone(zone_id, zone_name)
        reading = SmartComfortReading(
            timestamp=dt_util.utcnow(),
            temperature=temperature,
            is_heating=is_heating,
            target_temperature=target_temperature,
        )
        zone.add_reading(reading)

        self.maybe_save()

        _LOGGER.debug(
            "Smart Comfort: %s recorded temp=%.1f°C heating=%s target=%s",
            zone_name, temperature, is_heating, target_temperature,
        )

    def get_heating_rate(self, zone_id: str) -> float | None:
        """Return the zone's heating rate in °C/h, or None when uncertain."""
        if zone_id not in self._zones:
            return None
        return self._zones[zone_id].get_heating_rate()

    def get_cooling_rate(self, zone_id: str) -> float | None:
        """Return the zone's cooling rate in °C/h, or None when uncertain."""
        if zone_id not in self._zones:
            return None
        return self._zones[zone_id].get_cooling_rate()



    def get_time_to_target(
        self,
        zone_id: str,
        current_temp: float,
        target_temp: float,
        zone_type: str = "HEATING",
    ) -> int | None:
        """Return estimated minutes to reach target_temp, or None when uncertain."""
        if zone_id not in self._zones:
            return None
        return self._zones[zone_id].get_time_to_target(current_temp, target_temp, zone_type)

    def get_historical_comparison(
        self,
        zone_id: str,
        current_temp: float,
        comparison_window_minutes: int = 30,
    ) -> HistoricalComparison | None:
        """Return how `current_temp` compares to the zone's historical average."""
        if zone_id not in self._zones:
            return None
        return self._zones[zone_id].get_historical_comparison(
            current_temp,
            comparison_window_minutes,
        )

    def get_preheat_advice(
        self,
        zone_id: str,
        target_temp: float,
        target_time: datetime,
        current_temp: float | None = None,
    ) -> PreheatAdvice | None:
        """Return a preheat-start recommendation for the zone, or None."""
        if zone_id not in self._zones:
            return None
        return self._zones[zone_id].get_preheat_advice(
            target_temp,
            target_time,
            current_temp,
        )


    def get_compensated_rate(self, base_rate: float, for_heating: bool = True) -> float:
        """Apply weather compensation to a heating or cooling rate.

        Returns base_rate unchanged when compensation is "none" or the
        outdoor temperature is unavailable. Heating slows in cold
        weather (factor < 1), cooling speeds up. Warm weather inverts.
        """
        if self._weather_compensation == "none":
            return base_rate

        outdoor_temp = _get_outdoor_temp(
            self._hass,  # type: ignore[arg-type]
            self._outdoor_temp_entity,
            self._use_feels_like,
        )
        if outdoor_temp is None:
            return base_rate

        preset = WEATHER_COMPENSATION_PRESETS.get(
            self._weather_compensation,
            WEATHER_COMPENSATION_PRESETS["none"],
        )
        cold_thresh, cold_factor, warm_thresh, warm_factor = preset

        # For heating: cold weather = slower heating (more heat loss)
        # For cooling: cold weather = faster cooling (more heat loss)
        factor = 1.0

        if cold_thresh is not None and outdoor_temp < cold_thresh:
            if for_heating:
                # Cold weather: heating takes longer (divide by factor)
                factor = 1.0 / cold_factor
            else:
                # Cold weather: cooling is faster (multiply by factor)
                factor = cold_factor
        elif warm_thresh is not None and outdoor_temp > warm_thresh:
            if for_heating:
                # Warm weather: heating is faster (multiply by factor)
                factor = 1.0 / warm_factor
            else:
                # Warm weather: cooling is slower (divide by factor)
                factor = warm_factor

        compensated = base_rate * factor

        _LOGGER.debug(
            "Smart Comfort: weather compensation outdoor=%.1f°C preset=%s "
            "factor=%.2f base=%.2f°C/h compensated=%.2f°C/h",
            outdoor_temp,
            self._weather_compensation,
            factor,
            base_rate,
            compensated,
        )

        return round(compensated, 2)


def _process_entity_history(
    history: list[Any],
    zone: ZoneHistory,
) -> int:
    """Convert recorder history states into readings, returning the count added."""
    points_added = 0
    for state in history:
        try:
            if state.state in ("unavailable", "unknown"):
                continue

            attrs = state.attributes
            current_temp = attrs.get("current_temperature")
            if current_temp is None:
                continue

            hvac_action = attrs.get("hvac_action", "idle")
            is_heating = hvac_action in ("heating", "cooling")
            target_temp = attrs.get("temperature")

            timestamp = state.last_changed
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=dt_util.UTC)

            reading = SmartComfortReading(
                timestamp=timestamp,
                temperature=float(current_temp),
                is_heating=is_heating,
                target_temperature=float(target_temp) if target_temp else None,
            )
            zone.readings.append(reading)
            points_added += 1

        except (ValueError, TypeError, AttributeError) as e:
            _LOGGER.debug(
                "Smart Comfort: skipping unreadable history state (%s)", e,
            )
            continue
    return points_added


async def async_load_history_from_recorder(
    hass: HomeAssistant,
    manager: SmartComfortManager,
    climate_entity_ids: list[str],
    entity_to_zone_id: dict[str, str] | None = None,
) -> int:
    """Prime the manager with the last 24 h of climate history from the recorder.

    Lets the integration produce sensible rates immediately after a
    restart instead of waiting hours for fresh readings.
    `entity_to_zone_id` is required because slugified entity names
    don't always match Tado's numeric zone IDs.
    """
    if not climate_entity_ids or not entity_to_zone_id:
        return 0

    try:
        from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
        from homeassistant.components.recorder.history import get_significant_states

        end_time = dt_util.utcnow()
        start_time = end_time - timedelta(hours=RECORDER_HISTORY_HOURS)

        _LOGGER.debug(
            "Smart Comfort: loading last %dh of history for %d climate entity(ies)",
            RECORDER_HISTORY_HOURS, len(climate_entity_ids),
        )

        def _get_history() -> list[dict[str, Any]]:
            return get_significant_states(  # type: ignore[return-value]
                hass, start_time, end_time, climate_entity_ids,
                significant_changes_only=False,
            )

        states = await get_instance(hass).async_add_executor_job(_get_history)

        if not states:
            _LOGGER.debug("Smart Comfort: no history found in recorder")
            return 0

        total_points = 0

        for entity_id, history in states.items():  # type: ignore[attr-defined]
            if not history:
                continue

            entity_name = entity_id.replace("climate.", "")
            zone_id = entity_to_zone_id.get(entity_name)
            if not zone_id:
                _LOGGER.debug(
                    "Smart Comfort: no zone mapping for entity %s, skipping",
                    entity_name,
                )
                continue

            zone_name = entity_name.replace("_", " ").title()
            zone = manager.get_zone(zone_id, zone_name)
            points_added = _process_entity_history(history, zone)

            zone.readings.sort(key=lambda r: r.timestamp)
            zone._prune_old_readings()

            if points_added > 0:
                _LOGGER.debug(
                    "Smart Comfort: %s loaded %d history point(s), %d kept after pruning",
                    zone_name, points_added, len(zone.readings),
                )
                total_points += len(zone.readings)

        _LOGGER.info(
            "Smart Comfort: loaded %d historical reading(s) from the recorder",
            total_points,
        )
        return total_points

    except ImportError:
        _LOGGER.debug("Smart Comfort: recorder component not available, skipping history load")
        return 0
    except Exception as e:
        _LOGGER.warning(
            "Smart Comfort: could not load history from recorder (%s), "
            "rates will rebuild from new readings",
            e,
        )
        return 0


def _calculate_zone_baseline(
    sensor_stats: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Derive baseline heating/cooling rates from hourly statistics, or None.

    Uses the median of significant rises and falls so a couple of
    sensor outliers don't skew the baseline. Requires at least 24
    hourly samples (one full day).
    """
    if len(sensor_stats) < 24:
        return None

    temp_changes = []
    for i in range(1, len(sensor_stats)):
        prev_mean = sensor_stats[i - 1].get("mean")
        curr_mean = sensor_stats[i].get("mean")
        if prev_mean is not None and curr_mean is not None:
            temp_changes.append(curr_mean - prev_mean)

    if not temp_changes:
        return None

    heating_changes = sorted(c for c in temp_changes if c > BASELINE_CHANGE_THRESHOLD)
    cooling_changes = sorted(c for c in temp_changes if c < -BASELINE_CHANGE_THRESHOLD)

    baseline_heating = round(heating_changes[len(heating_changes) // 2], 2) if heating_changes else None
    baseline_cooling = round(cooling_changes[len(cooling_changes) // 2], 2) if cooling_changes else None

    return {
        "baseline_heating_rate": baseline_heating,
        "baseline_cooling_rate": baseline_cooling,
        "data_points": len(sensor_stats),
        "heating_samples": len(heating_changes),
        "cooling_samples": len(cooling_changes),
    }


async def async_load_baseline_from_statistics(
    hass: HomeAssistant,
    manager: SmartComfortManager,
    zone_sensor_mapping: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Seed each zone's baseline rates from 7 days of long-term statistics.

    Tier 3 of the 3-tier load strategy (cache → recorder → statistics).
    The baseline only fires when a zone has no real-time rate yet, so
    a fresh install or an unused zone still produces sensible
    estimates from the start.
    """
    if not zone_sensor_mapping:
        return {}

    try:
        from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
        from homeassistant.components.recorder.statistics import statistics_during_period

        end_time = dt_util.utcnow()
        start_time = end_time - timedelta(days=7)
        statistic_ids = list(zone_sensor_mapping.values())

        _LOGGER.debug(
            "Smart Comfort: loading 7-day baseline statistics for %d sensor(s)",
            len(statistic_ids),
        )

        def _get_statistics() -> dict[str, Any]:
            return statistics_during_period(
                hass, start_time, end_time,
                statistic_ids=statistic_ids,  # type: ignore[arg-type]
                period="hour",
                units={"temperature": "°C"},
                types={"mean", "min", "max"},
            )

        stats = await get_instance(hass).async_add_executor_job(_get_statistics)

        if not stats:
            _LOGGER.debug("Smart Comfort: no long-term statistics available yet")
            return {}

        results = {}

        for zone_id, sensor_id in zone_sensor_mapping.items():
            if sensor_id not in stats:
                continue

            sensor_stats = stats[sensor_id]
            baseline = _calculate_zone_baseline(sensor_stats)
            if baseline is None:
                _LOGGER.debug(
                    "Smart Comfort: zone %s only has %d statistic point(s), "
                    "not enough for a baseline yet",
                    zone_id, len(sensor_stats),
                )
                continue

            results[zone_id] = baseline

            zone = manager.get_zone(zone_id)
            zone._baseline_heating_rate = baseline["baseline_heating_rate"]
            zone._baseline_cooling_rate = baseline["baseline_cooling_rate"]

            _LOGGER.debug(
                "Smart Comfort: zone %s baseline from %d hour(s), "
                "heating=%s°C/h, cooling=%s°C/h",
                zone_id, baseline["data_points"],
                baseline["baseline_heating_rate"],
                baseline["baseline_cooling_rate"],
            )

        return results

    except ImportError as e:
        _LOGGER.debug(
            "Smart Comfort: statistics API not available (%s), skipping baseline load",
            e,
        )
        return {}
    except Exception as e:
        _LOGGER.warning(
            "Smart Comfort: could not load baseline statistics (%s), "
            "rates will rely on real-time readings instead",
            e,
        )
        return {}
