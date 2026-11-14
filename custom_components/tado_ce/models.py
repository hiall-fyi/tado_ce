"""Tado CE unified data models: shared dataclasses for temperature readings."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Self, get_type_hints

from .helpers import parse_iso_datetime


class _SerializableMixin:
    """Mixin for dataclasses with datetime fields: provides to_dict/from_dict."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for JSON storage."""
        d = {}
        for f in fields(self):  # type: ignore[arg-type]
            v = getattr(self, f.name)
            d[f.name] = v.isoformat() if isinstance(v, datetime) else v
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize from dictionary."""
        hints = get_type_hints(cls)
        kwargs = {}
        for f in fields(cls):  # type: ignore[arg-type]
            v = data.get(f.name)
            if v is not None and hints.get(f.name) is datetime:
                v = parse_iso_datetime(v)
            if v is not None:
                kwargs[f.name] = v
        return cls(**kwargs)


@dataclass
class HeatingCycleReading(_SerializableMixin):
    """Single temperature measurement during a heating cycle."""

    time: datetime  # UTC
    temp: float


@dataclass
class SmartComfortReading(_SerializableMixin):
    """A single temperature reading with heating context for comfort analysis."""

    timestamp: datetime
    temperature: float
    is_heating: bool  # True if HVAC is actively heating/cooling
    target_temperature: float | None = None
