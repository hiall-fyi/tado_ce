"""Detect Tado-side zone-id changes between coordinator polls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import CAPABILITY_ZONE_TYPES


@dataclass(frozen=True, slots=True)
class ZoneFingerprintDelta:
    """Per-poll diff of the zone-id set."""

    added: frozenset[str]
    removed: frozenset[str]
    is_first_poll: bool
    is_empty_response: bool


class ZoneFingerprintTracker:
    """Diff `zoneStates.keys()` against the previous snapshot."""

    def __init__(self) -> None:
        """Initialise with no baseline."""
        self._previous: frozenset[str] | None = None

    @property
    def zone_ids(self) -> frozenset[str]:
        """Zone ids of the latest non-empty zoneStates; empty before the first one."""
        return self._previous or frozenset()

    def update(self, zone_states: dict[str, Any] | None) -> ZoneFingerprintDelta:
        """Record the current zone-id set and return the delta versus the previous one."""
        is_first = self._previous is None

        if not zone_states:
            return ZoneFingerprintDelta(
                added=frozenset(),
                removed=frozenset(),
                is_first_poll=is_first,
                is_empty_response=True,
            )

        current = frozenset(zone_states.keys())
        previous = self._previous if self._previous is not None else current
        delta = ZoneFingerprintDelta(
            added=current - previous,
            removed=previous - current,
            is_first_poll=is_first,
            is_empty_response=False,
        )
        self._previous = current
        return delta


def zone_device_fingerprints_changed(
    zones_info: list[Any],
    prev_fp: dict[str, Any],
) -> tuple[set[str], dict[str, list[list[str]]]]:
    """Return zone_ids whose serial or firmware set changed against prev_fp, plus the fresh fingerprint map."""
    changed: set[str] = set()
    fresh: dict[str, list[list[str]]] = {}
    for zone in zones_info:
        if zone.get("type") not in CAPABILITY_ZONE_TYPES:
            continue
        zone_id = str(zone.get("id"))
        devices = zone.get("devices") or []
        serials = sorted(d.get("shortSerialNo") for d in devices if d.get("shortSerialNo"))
        fws = sorted(d.get("currentFwVersion") for d in devices if d.get("currentFwVersion"))
        fp = [serials, fws]
        fresh[zone_id] = fp
        if zone_id in prev_fp and prev_fp[zone_id] != fp:
            changed.add(zone_id)
    return changed, fresh
