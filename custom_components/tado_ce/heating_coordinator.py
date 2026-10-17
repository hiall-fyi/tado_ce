"""Tado CE heating cycle coordinator: cross-zone analysis."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .heating_analyzer import HeatingCycleAnalyzer
from .heating_detector import HeatingCycleDetector
from .heating_storage import HeatingCycleStorage
from .helpers import mask_home_id
from .thermal_analyzer import ThermalAnalyzer

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .heating_models import HeatingCycle, HeatingCycleConfig

_LOGGER = logging.getLogger(__name__)



class HeatingCycleCoordinator(DataUpdateCoordinator):
    """Coordinate heating cycle detection and analysis for all zones."""

    def __init__(
        self,
        hass: HomeAssistant,
        home_id: str,
        config: HeatingCycleConfig,
    ) -> None:
        """Initialise the coordinator (manual updates only, no polling)."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"heating_cycle_{home_id}",
            update_interval=None,
        )
        self._home_id = home_id
        self._config = config
        self._storage = HeatingCycleStorage(hass, home_id)
        self._analyzer = HeatingCycleAnalyzer(config.min_cycles)
        self._second_order = ThermalAnalyzer(config.min_cycles)
        self._detectors: dict[str, HeatingCycleDetector] = {}
        self._zone_data: dict[str, dict[str, Any]] = {}
        self._zone_states: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def _async_update_data(self) -> dict[str, Any]:
        """Return cached zone data: DataUpdateCoordinator hook (manual updates only)."""
        return self._zone_data

    async def async_setup(self) -> None:
        """Load storage, resume any active cycles, and prime per-zone metrics."""
        _LOGGER.debug(
            "Heating Cycle: setting up cycle tracker for home %s",
            mask_home_id(self._home_id),
        )

        await self._storage.async_load()

        active_cycles = await self._storage.get_active_cycles()
        if active_cycles:
            _LOGGER.debug(
                "Heating Cycle: resuming %d active cycle(s) from storage",
                len(active_cycles),
            )

        for zone_id, cycle in active_cycles.items():
            detector = self._get_or_create_detector(zone_id)
            detector.resume_cycle(cycle)
            _LOGGER.debug(
                "Heating Cycle: zone %s resumed cycle started at %s",
                zone_id,
                cycle.start_time.isoformat(),
            )

        all_zone_ids = await self._storage.get_all_zone_ids()
        for zone_id in all_zone_ids:
            await self._async_update_zone_data(zone_id)

        _LOGGER.debug(
            "Heating Cycle: setup complete (%d zone(s) tracked)",
            len(all_zone_ids),
        )

    def _get_or_create_detector(self, zone_id: str) -> HeatingCycleDetector:
        """Return the detector for this zone, creating one if needed."""
        if zone_id not in self._detectors:
            self._detectors[zone_id] = HeatingCycleDetector(zone_id, self._config)
        return self._detectors[zone_id]

    async def on_zone_update(
        self,
        zone_id: str,
        target_temp: float,
        current_temp: float,
        timestamp: datetime | None = None,
        *,
        source: str = "entity",
    ) -> None:
        """Atomically apply a setpoint + temperature update for one zone (single lock prevents interleaving)."""
        if timestamp is None:
            timestamp = dt_util.utcnow()

        _LOGGER.debug(
            "Heating Cycle: zone %s update, target=%.1f°C, current=%.1f°C",
            zone_id, target_temp, current_temp,
        )

        # Cache for sensors so native_value can stay sync.
        self._zone_states[zone_id] = {
            "current_temp": current_temp,
            "target_temp": target_temp,
            "timestamp": timestamp,
            "source": source,
        }

        self.async_set_updated_data(self._zone_data)

        async with self._lock:
            detector = self._get_or_create_detector(zone_id)

            cycle_started = detector.check_setpoint_change(target_temp, timestamp, current_temp)

            if cycle_started:
                _LOGGER.debug(
                    "Heating Cycle: zone %s new cycle started "
                    "(target=%.1f°C, current=%.1f°C)",
                    zone_id, target_temp, current_temp,
                )

            detector.on_temperature_update(current_temp, timestamp)

            completed_cycle = detector.check_cycle_complete()
            if completed_cycle:
                await self._storage.save_cycle(
                    zone_id, completed_cycle,
                    window_days=self._config.rolling_window_days,
                )
                _LOGGER.debug(
                    "Heating Cycle: zone %s cycle completed and saved",
                    zone_id,
                )
                await self._async_update_zone_data(zone_id)

    @callback
    def clear_zone_state(self, zone_id: str, *, source: str | None = None) -> None:
        """Drop the cached temperatures for a zone that has no room reading, only if `source` (when given) wrote them."""
        state = self._zone_states.get(zone_id)
        if state is None or (source is not None and state.get("source") != source):
            return
        del self._zone_states[zone_id]
        _LOGGER.debug("Heating Cycle: zone %s has no room temperature, cleared its cached reading", zone_id)
        self.async_set_updated_data(self._zone_data)

    async def check_timeouts(self) -> None:
        """Time out any active cycles whose completion criteria stalled."""
        async with self._lock:
            for zone_id, detector in self._detectors.items():
                if detector.check_cycle_timeout():
                    _LOGGER.debug(
                        "Heating Cycle: zone %s cycle timed out",
                        zone_id,
                    )
                    await self._async_update_zone_data(zone_id)

    async def _async_update_zone_data(self, zone_id: str) -> None:
        """Recompute first-order + second-order metrics for one zone."""
        cycles = await self._storage.get_cycles(zone_id, self._config.rolling_window_days)

        metrics = self._analyzer.analyze_cycles(cycles)

        acceleration = self._second_order.calculate_acceleration(cycles)
        approach_factor = self._second_order.calculate_approach_factor(cycles)

        if metrics:
            self._zone_data[zone_id] = {
                **metrics,
                "acceleration": acceleration,
                "approach_factor": approach_factor,
            }
            _LOGGER.debug(
                "Heating Cycle: zone %s metrics, inertia=%.1f min, "
                "rate=%.2f °C/h, accel=%s °C/h², approach=%s%%, "
                "confidence=%.2f",
                zone_id,
                metrics["inertia_time"],
                metrics["heating_rate"],
                acceleration,
                approach_factor,
                metrics["confidence_score"],
            )
        else:
            self._zone_data[zone_id] = {
                "inertia_time": None,
                "heating_rate": None,
                "confidence_score": 0.0,
                "cycle_count": len(cycles),
                "completed_count": len(cycles),
                "acceleration": None,
                "approach_factor": None,
            }
            _LOGGER.debug(
                "Heating Cycle: zone %s has only %d cycle(s), not enough "
                "data for metrics yet",
                zone_id, len(cycles),
            )

        self.async_set_updated_data(self._zone_data)

    def get_zone_data(self, zone_id: str) -> dict[str, Any] | None:
        """Return computed metrics for one zone, or None if no data yet."""
        return self._zone_data.get(zone_id)

    def get_zone_state(self, zone_id: str) -> dict[str, Any] | None:
        """Return cached current/target temperature snapshot: lets sensors read sync without blocking on storage."""
        return self._zone_states.get(zone_id)

    async def get_cycles(self, zone_id: str) -> list[HeatingCycle]:
        """Return completed cycles for one zone inside the rolling window."""
        return await self._storage.get_cycles(zone_id, self._config.rolling_window_days)

    def get_active_cycle(self, zone_id: str) -> HeatingCycle | None:
        """Return the currently-running cycle for one zone, if any."""
        detector = self._detectors.get(zone_id)
        if detector:
            return detector.get_active_cycle()
        return None

    def estimate_preheat_time(
        self,
        zone_id: str,
        current_temp: float,
        target_temp: float,
    ) -> float | None:
        """Estimate preheat time in minutes, or None when there isn't enough data."""
        metrics = self._zone_data.get(zone_id)
        if not metrics:
            return None

        return self._analyzer.estimate_preheat_time(current_temp, target_temp, metrics)
