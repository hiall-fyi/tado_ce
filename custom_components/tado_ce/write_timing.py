"""Tado CE write timing: opt-in counters of how long Tado takes to show a write, counts only."""

from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .data_loader import STORE_VERSION
from .optimistic_helpers import ROUTE_AT_STAMP_ATTR

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .config_manager import ConfigurationManager
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)

STORE_NAME: Final = "write_timing"
SCHEMA_VERSION: Final = 1
COUNT_CAP: Final = 10000
MAX_DAYS: Final = 365

ROUTES: Final = ("homekit", "cloud")
ZONE_KINDS: Final = ("heating", "ac", "hot_water")
OUTCOMES: Final = ("confirmed", "different", "expired")
# (upper bound in seconds, label); anything at or above the last bound is "180+".
_BUCKET_BOUNDS: Final = (
    (10, "0-10"),
    (20, "10-20"),
    (30, "20-30"),
    (45, "30-45"),
    (60, "45-60"),
    (90, "60-90"),
    (120, "90-120"),
    (180, "120-180"),
)
_LAST_BUCKET: Final = "180+"
AGE_BUCKETS: Final = (*(label for _, label in _BUCKET_BOUNDS), _LAST_BUCKET)
_UNSET: Final = object()

Counts = dict[str, dict[str, dict[str, dict[str, int]]]]


def age_bucket(age_seconds: float) -> str:
    """Return the bucket label for the seconds between a write and the fetch that judged it."""
    for bound, label in _BUCKET_BOUNDS:
        if age_seconds < bound:
            return label
    return _LAST_BUCKET


async def async_remove_saved_counts(hass: HomeAssistant, home_id: str | None) -> None:
    """Delete this home's saved write timing counters, and nothing else."""
    if home_id:
        await Store(hass, STORE_VERSION, f"tado_ce/{STORE_NAME}_{home_id}").async_remove()


class WriteTimingRecorder:
    """Counts, per route, zone kind, age bucket and outcome, how a zone fetch judged a write."""

    def __init__(self, config_manager: ConfigurationManager | None, data_loader: DataLoader | None) -> None:
        """Bind the opt-in option reader and the store owner; collecting starts empty."""
        self._config_manager = config_manager
        self._data_loader = data_loader
        self._counts: Counts = {}
        self._started_at: float | None = None
        self._warned: set[str] = set()
        # One entry per entity: the (write sequence, fetch) last counted, so a re-judging of the same fetch adds nothing.
        self._last_judged: dict[object, tuple[object, datetime]] = {}

    def is_enabled(self) -> bool:
        """Return True only when the user turned the option on."""
        manager = self._config_manager
        return manager is not None and manager.get_write_timing_enabled() is True

    def _warn_once(self, category: str, message: str, *args: object) -> None:
        if category in self._warned:
            return
        self._warned.add(category)
        _LOGGER.warning(message, *args)

    def _forget(self) -> None:
        self._counts = {}
        self._started_at = None
        self._last_judged = {}

    async def async_load(self) -> None:
        """Load the saved counters when the option is on; delete them when it is off."""
        loader = self._data_loader
        if loader is None:
            return
        try:
            if not self.is_enabled():
                await self.async_clear()
                return
            self._adopt(await loader.async_load_auxiliary(STORE_NAME))
            if self._started_at is None:
                self._started_at = time.time()
        except Exception:
            # Setup calls this bare: a store of an unexpected shape or any read error must never fail setup, so start empty.
            self._forget()
            self._warn_once("load", "Write Timing: could not read the saved counters, starting empty")
            _LOGGER.debug("Write Timing: load failure detail", exc_info=True)

    def _adopt(self, saved: object) -> None:
        """Take over saved counters of the current schema, keeping only known keys and capped integers."""
        self._forget()
        if not isinstance(saved, dict) or saved.get("schema") != SCHEMA_VERSION:
            return
        counts = self._clean_counts(saved.get("counts"))
        started = saved.get("started_at")
        self._counts = counts
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            self._started_at = float(started)

    @staticmethod
    def _clean_counts(raw: Any) -> Counts:
        clean: Counts = {}
        for route in ROUTES:
            for kind in ZONE_KINDS:
                for bucket in AGE_BUCKETS:
                    for outcome in OUTCOMES:
                        value = raw[route][kind][bucket][outcome] if _has_path(raw, route, kind, bucket, outcome) else None
                        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                            clean.setdefault(route, {}).setdefault(kind, {}).setdefault(bucket, {})[outcome] = min(value, COUNT_CAP)
        return clean

    async def async_clear(self) -> None:
        """Forget the counters and delete the store file; touches only this recorder's own store."""
        self._forget()
        loader = self._data_loader
        if loader is None:
            return
        try:
            await loader.async_remove_auxiliary(STORE_NAME)
        except Exception:
            # A failed delete must not block setup; memory is already empty.
            self._warn_once("clear", "Write Timing: could not delete the saved counters")
            _LOGGER.debug("Write Timing: clear failure detail", exc_info=True)

    def record(self, entity: object, outcome: str) -> None:
        """Count one judging of a write; never raises into a poll."""
        try:
            self._record(entity, outcome)
        except Exception:
            # A counter must never break a poll, so any failure is logged once and dropped.
            self._warn_once("record", "Write Timing: a judging could not be counted")
            _LOGGER.debug("Write Timing: record failure detail", exc_info=True)

    def _record(self, entity: object, outcome: str) -> None:
        if not self.is_enabled() or outcome not in OUTCOMES:
            return
        kind = getattr(entity, "_write_timing_zone_kind", None)
        set_at = getattr(entity, "_optimistic_set_at", None)
        route = self._route_of(entity)
        if kind not in ZONE_KINDS or route is None or not isinstance(set_at, (int, float)):
            return
        fetched_at = self._newer_fetch(entity)
        if fetched_at is None:
            return
        who = getattr(entity, "entity_id", None) or id(entity)
        marker = (getattr(entity, "_optimistic_sequence", None), fetched_at)
        if self._last_judged.get(who) == marker:
            return
        self._last_judged[who] = marker
        # Age to the fetch that judged, not to now: the judging can run on a later listener update.
        behind_fetch = (dt_util.utcnow() - fetched_at).total_seconds()
        bucket = age_bucket(max(0.0, time.monotonic() - set_at - behind_fetch))
        cell = self._counts.setdefault(route, {}).setdefault(kind, {}).setdefault(bucket, {})
        cell[outcome] = min(cell.get(outcome, 0) + 1, COUNT_CAP)
        if self._started_at is None:
            self._started_at = time.time()
        self._save()

    @staticmethod
    def _route_of(entity: object) -> str | None:
        """Return homekit or cloud, or None when this write never set its own route."""
        route = getattr(entity, "_last_write_source", None)
        if route not in ROUTES:
            return None
        stamped = getattr(entity, ROUTE_AT_STAMP_ATTR, _UNSET)
        # Unchanged since the write was stamped and not cloud: a homekit route left by an earlier write.
        if stamped is not _UNSET and route == stamped and route != "cloud":
            return None
        return str(route)

    @staticmethod
    def _newer_fetch(entity: object) -> datetime | None:
        """Return the start of the judged fetch when both epochs exist and it started after the write."""
        written = getattr(entity, "_optimistic_zone_fetch_epoch", None)
        coordinator = getattr(entity, "coordinator", None)
        if written is None or coordinator is None:
            return None
        current = coordinator.last_zone_fetch_ts()
        if not isinstance(current, datetime) or not current > written:
            return None
        return current

    def _save(self) -> None:
        loader = self._data_loader
        if loader is None:
            return
        try:
            loader.save_auxiliary(
                STORE_NAME,
                {"schema": SCHEMA_VERSION, "started_at": self._started_at, "counts": self._snapshot()},
            )
        except Exception:
            # A storage failure keeps the in-memory counters running.
            self._warn_once("save", "Write Timing: could not save the counters, keeping them in memory")
            _LOGGER.debug("Write Timing: save failure detail", exc_info=True)

    def diagnostics(self, integration_version: str, ha_version: str) -> dict[str, Any]:
        """Return the diagnostics value: only `{"enabled": False}` when off, else the approved field list."""
        if not self.is_enabled():
            return {"enabled": False}
        days = 0
        if self._started_at is not None:
            days = max(0, min(MAX_DAYS, int((time.time() - self._started_at) // 86400)))
        return {
            "schema": SCHEMA_VERSION,
            "integration_version": integration_version,
            "ha_version": ha_version,
            "collecting_since_days": days,
            "counts": self._snapshot(),
        }

    def _snapshot(self) -> dict[str, Any]:
        """Return the full fixed table in fixed order, zeros included, so neither shape nor order says what happened first."""
        return {
            route: {
                kind: {
                    bucket: {
                        outcome: self._counts.get(route, {}).get(kind, {}).get(bucket, {}).get(outcome, 0)
                        for outcome in OUTCOMES
                    }
                    for bucket in AGE_BUCKETS
                }
                for kind in ZONE_KINDS
            }
            for route in ROUTES
        }


def _has_path(raw: Any, *keys: str) -> bool:
    """Return True when every level of `raw` down `keys` is a dict holding the next key."""
    node = raw
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True
