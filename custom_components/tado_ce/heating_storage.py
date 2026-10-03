"""Tado CE heating-cycle storage: per-home cycle history in HA Store with one-shot migration from v3.x JSON file."""

from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .heating_models import HeatingCycle
from .helpers import parse_iso_datetime
from .storage import async_migrate_json_to_store

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SAVE_DELAY = 15


class HeatingCycleStorage:
    """Persist heating cycles to HA Store with debounced writes and migration."""

    def __init__(self, hass: HomeAssistant, home_id: str) -> None:
        self._hass = hass
        self._home_id = home_id
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORE_VERSION,
            f"tado_ce/heating_cycles_{home_id}",
        )
        # Old file path for migration
        self._old_storage_path = Path(
            hass.config.path(
                f".storage/tado_ce/heating_cycle_history_{home_id}.json",
            ),
        )
        self._data: dict[str, Any] = {"zones": {}}

    async def async_load(self) -> None:
        """Load cycle history from Store, migrating from the v3.x JSON file when needed."""
        from .helpers import mask_home_id

        try:
            stored = await self._store.async_load()
            if stored is not None:
                self._data = self._migrate_data_format(stored)
                _LOGGER.debug(
                    "Heating Storage: loaded cycle history for home "
                    "%s: %d zone(s)",
                    mask_home_id(self._home_id),
                    len(self._data.get("zones", {})),
                )
                return

            migrated = await async_migrate_json_to_store(
                self._hass, self._old_storage_path, self._store,
                label="heating_cycles",
            )
            if migrated is not None and isinstance(migrated, dict):
                migrated.pop("version", None)
                self._data = self._migrate_data_format(migrated)
                return

            _LOGGER.debug(
                "Heating Storage: no existing cycle history found "
                "for home %s, starting fresh",
                mask_home_id(self._home_id),
            )

        except HomeAssistantError:
            _LOGGER.warning(
                "Heating Storage: cycle history Store is corrupt, "
                "starting fresh for this session, will retry on next "
                "reload",
                exc_info=True,
            )
            self._data = {"zones": {}}
        except OSError:
            _LOGGER.warning(
                "Heating Storage: cycle history could not be read "
                "from disk, starting fresh for this session, will "
                "retry on next reload",
                exc_info=True,
            )
            self._data = {"zones": {}}

    def _migrate_data_format(self, loaded_data: dict[str, Any]) -> dict[str, Any]:
        """Lift old `{zone_id: [cycles]}` shape into new `{zones: {zone_id: {cycles: [...]}}}`."""
        if "zones" in loaded_data:
            # Already new shape: drop the legacy "version" key
            # if present so it doesn't leak into save_cycle.
            loaded_data.pop("version", None)
            return loaded_data

        _LOGGER.info(
            "Heating Storage: migrating cycle history from legacy "
            "format",
        )
        new_data: dict[str, Any] = {"zones": {}}

        for zone_id, cycles in loaded_data.items():
            if zone_id == "version":
                continue
            if isinstance(cycles, list):
                new_data["zones"][zone_id] = {"cycles": cycles}
                _LOGGER.debug(
                    "Heating Storage: migrated zone %s with %d "
                    "cycle(s)",
                    zone_id,
                    len(cycles),
                )

        return new_data

    def _schedule_save(self) -> None:
        """Schedule a debounced save to Store."""
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)

    async def save_cycle(self, zone_id: str, cycle: HeatingCycle, *, window_days: int = 7) -> None:
        """Save completed cycle for a zone."""
        if zone_id not in self._data["zones"]:
            self._data["zones"][zone_id] = {"cycles": []}

        self._data["zones"][zone_id]["cycles"].append(cycle.to_dict())

        _LOGGER.debug(
            "Heating Storage: stored cycle for zone %s: %s → %s "
            "(completed=%s, interrupted=%s)",
            zone_id,
            cycle.start_time.isoformat(),
            cycle.end_time.isoformat() if cycle.end_time else "active",
            cycle.completed,
            cycle.interrupted,
        )

        # Cleanup old cycles (keep 2x configured rolling window)
        await self._cleanup_old_cycles(zone_id, window_days)

        # Schedule debounced save
        self._schedule_save()

    async def get_cycles(
        self,
        zone_id: str,
        window_days: int = 7,
    ) -> list[HeatingCycle]:
        """Get cycles for a zone within rolling window."""
        if zone_id not in self._data["zones"]:
            return []

        cutoff = dt_util.utcnow() - timedelta(days=window_days)
        cycles_list = self._data["zones"][zone_id]["cycles"]
        cycles = []
        bad_indices: set[int] = set()

        for idx, cycle_dict in enumerate(cycles_list):
            try:
                cycle = HeatingCycle.from_dict(cycle_dict)
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning(
                    "Heating Storage: dropping malformed cycle record for zone %s",
                    zone_id,
                    exc_info=True,
                )
                bad_indices.add(idx)
                continue
            # Only include completed, non-interrupted cycles within window
            if cycle.start_time >= cutoff and cycle.completed and not cycle.interrupted:
                cycles.append(cycle)

        if bad_indices:
            # Also drop from storage so an idle zone doesn't relog this forever.
            self._data["zones"][zone_id]["cycles"] = [
                c for i, c in enumerate(cycles_list) if i not in bad_indices
            ]
            self._schedule_save()

        return cycles

    async def get_active_cycles(self) -> dict[str, HeatingCycle]:
        """Get all active cycles (for resume after restart)."""
        active = {}
        any_dropped = False

        for zone_id, zone_data in self._data["zones"].items():
            cycles_list = zone_data["cycles"]
            bad_indices: set[int] = set()

            for idx, cycle_dict in enumerate(cycles_list):
                try:
                    cycle = HeatingCycle.from_dict(cycle_dict)
                except (KeyError, TypeError, ValueError):
                    _LOGGER.warning(
                        "Heating Storage: dropping malformed cycle record for zone %s",
                        zone_id,
                        exc_info=True,
                    )
                    bad_indices.add(idx)
                    continue
                if not cycle.completed and not cycle.interrupted:
                    active[zone_id] = cycle
                    break  # Only one active cycle per zone

            if bad_indices:
                # Also drop from storage; only indices scanned before the
                # break above are removed, unscanned tail records are left alone.
                zone_data["cycles"] = [
                    c for i, c in enumerate(cycles_list) if i not in bad_indices
                ]
                any_dropped = True

        if active:
            _LOGGER.info(
                "Heating Storage: resuming %d active cycle(s) after "
                "restart",
                len(active),
            )

        if any_dropped:
            self._schedule_save()

        return active

    async def get_all_zone_ids(self) -> list[str]:
        """Get all zone IDs with stored cycle data."""
        return list(self._data["zones"].keys())

    async def _cleanup_old_cycles(self, zone_id: str, window_days: int = 7) -> None:
        """Remove cycles older than 2x configured rolling window."""
        cutoff = dt_util.utcnow() - timedelta(days=window_days * 2)

        cycles = self._data["zones"][zone_id]["cycles"]
        original_count = len(cycles)

        def _still_within_window(c: dict[str, Any]) -> bool:
            try:
                return parse_iso_datetime(c["start_time"]) >= cutoff
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning(
                    "Heating Storage: dropping malformed cycle record for zone %s",
                    zone_id,
                    exc_info=True,
                )
                return False

        self._data["zones"][zone_id]["cycles"] = [
            c for c in cycles if _still_within_window(c)
        ]

        removed_count = original_count - len(self._data["zones"][zone_id]["cycles"])
        if removed_count > 0:
            _LOGGER.debug(
                "Heating Storage: pruned %d old cycle(s) for zone %s",
                removed_count,
                zone_id,
            )
