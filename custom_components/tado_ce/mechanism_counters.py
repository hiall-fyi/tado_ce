"""In-memory mechanism counters and a data-age gauge, persisted by the coordinator."""
from __future__ import annotations

import logging
import math
from typing import Any

from . import const
from .const import FRESHNESS_BUCKET_MINUTES, MECHANISM_IDS, PUT_AGREE_BUCKET_SECONDS

_LOGGER = logging.getLogger(__name__)

_VERSION = 1
_HISTORY_DAYS = 14

Cells = dict[str, dict[str, dict[str, int]]]


def _bucket_name(minutes: float) -> str:
    for edge in FRESHNESS_BUCKET_MINUTES:
        if minutes <= edge:
            return f"le_{edge}"
    return f"gt_{FRESHNESS_BUCKET_MINUTES[-1]}"


def put_agree_bucket(seconds: float) -> str:
    """Return the histogram bucket name for seconds from a put to its first agreeing fetch."""
    for edge in PUT_AGREE_BUCKET_SECONDS:
        if seconds <= edge:
            return f"le_{edge}"
    return f"gt_{PUT_AGREE_BUCKET_SECONDS[-1]}"


def put_agrees(payload: dict[str, Any], zone_state: dict[str, Any]) -> bool:
    """Return True when the cloud zone state shows what the put asked for (temperature only while ON)."""
    setting = zone_state.get("setting") or {}
    if not isinstance(setting, dict):
        return False
    power = setting.get("power")
    if "power_on" in payload and power != ("ON" if payload["power_on"] else "OFF"):
        return False
    if power != "ON":
        return "power_on" in payload
    if payload.get("temperature") is None:
        return True
    temperature = setting.get("temperature") or {}
    if not isinstance(temperature, dict):
        return False
    celsius = temperature.get("celsius")
    return isinstance(celsius, (int, float)) and abs(celsius - payload["temperature"]) <= 0.05


def _valid_cell(cell: Any) -> bool:
    if not isinstance(cell, dict):
        return False
    return all(type(cell.get(k)) is int and cell[k] >= 0 for k in ("n", "calls"))


def _add(cells: Cells, mech: str, outcome: str, n: int, calls: int) -> None:
    cell = cells.setdefault(mech, {}).setdefault(outcome, {"n": 0, "calls": 0})
    cell["n"] += n
    cell["calls"] += calls


def _clean_cells(raw: Any) -> Cells:
    """Return only the valid cells of a persisted counts dict, ids renamed through the migration map."""
    clean: Cells = {}
    if not isinstance(raw, dict):
        return clean
    for old_id, outcomes in raw.items():
        mech = const.MECHANISM_ID_MIGRATION.get(old_id, old_id)
        if mech not in MECHANISM_IDS or not isinstance(outcomes, dict):
            continue
        for outcome, cell in outcomes.items():
            if isinstance(outcome, str) and _valid_cell(cell):
                _add(clean, mech, outcome, cell["n"], cell["calls"])
    return clean


def _copy(cells: Cells) -> Cells:
    return {m: {o: dict(c) for o, c in outs.items()} for m, outs in cells.items()}


class MechanismCounters:
    """Counts fetch-mechanism decisions per day and samples cloud data age."""

    def __init__(self) -> None:
        self._today: Cells = {}
        self._history: list[dict[str, Any]] = []
        self._total: Cells = {}
        self._freshness: dict[str, dict[str, int]] = {}
        self._dirty = False
        self._logged = False

    def _log_once(self, where: str) -> None:
        if not self._logged:
            self._logged = True
            _LOGGER.debug("Mechanism counters: %s failed, ignored", where, exc_info=True)

    @property
    def needs_save(self) -> bool:
        """True when counts changed since the last mark_saved."""
        return self._dirty

    def mark_saved(self) -> None:
        """Clear the dirty flag after the coordinator persisted the payload."""
        self._dirty = False

    def count(self, mechanism_id: str, outcome: str, calls: int = 0) -> None:
        """Record one decision for a known mechanism id; never raises."""
        try:
            if mechanism_id not in MECHANISM_IDS or not isinstance(outcome, str):
                return
            if type(calls) is not int or calls < 0:
                return
            _add(self._today, mechanism_id, outcome, 1, calls)
            _add(self._total, mechanism_id, outcome, 1, calls)
            self._dirty = True
        except Exception:  # counters never gate behaviour
            self._log_once("count")

    def observe_age(self, clock: str, minutes: float) -> None:
        """Add one data-age sample to the freshness buckets of a clock."""
        try:
            if not isinstance(clock, str) or isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
                return
            if not math.isfinite(minutes):
                return
            buckets = self._freshness.setdefault(clock, {})
            name = _bucket_name(minutes)
            buckets[name] = buckets.get(name, 0) + 1
            self._dirty = True
        except Exception:  # counters never gate behaviour
            self._log_once("observe_age")

    def roll_day(self, day_label: str) -> None:
        """Move today's counts into history (capped) and start a new day."""
        try:
            if not self._today:
                return
            self._history.append({"day": str(day_label), "counts": _copy(self._today)})
            del self._history[:-_HISTORY_DAYS]
            self._today = {}
            self._dirty = True
        except Exception:  # counters never gate behaviour
            self._log_once("roll_day")

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable store payload."""
        return {
            "version": _VERSION,
            "today": _copy(self._today),
            "history": [{"day": h["day"], "counts": _copy(h["counts"])} for h in self._history],
            "total": _copy(self._total),
            "freshness": {k: dict(v) for k, v in self._freshness.items()},
        }

    def load(self, payload: dict[str, Any] | None) -> None:
        """Add persisted counts into the in-memory ones, keeping only valid cells of known ids."""
        if not isinstance(payload, dict):
            return
        for source, target in ((payload.get("today"), self._today), (payload.get("total"), self._total)):
            for mech, outs in _clean_cells(source).items():
                for outcome, cell in outs.items():
                    _add(target, mech, outcome, cell["n"], cell["calls"])
        self._load_history(payload.get("history"))
        self._load_freshness(payload.get("freshness"))

    def _load_history(self, raw: Any) -> None:
        if not isinstance(raw, list):
            return
        persisted = [
            {"day": e["day"], "counts": counts}
            for e in raw
            if isinstance(e, dict) and isinstance(e.get("day"), str) and (counts := _clean_cells(e.get("counts")))
        ]
        self._history = (persisted + self._history)[-_HISTORY_DAYS:]

    def _load_freshness(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        valid = {f"le_{e}" for e in FRESHNESS_BUCKET_MINUTES} | {f"gt_{FRESHNESS_BUCKET_MINUTES[-1]}"}
        for clock, buckets in raw.items():
            if not isinstance(clock, str) or not isinstance(buckets, dict):
                continue
            mine = self._freshness.setdefault(clock, {})
            for name, n in buckets.items():
                if name in valid and type(n) is int and n >= 0:
                    mine[name] = mine.get(name, 0) + n
