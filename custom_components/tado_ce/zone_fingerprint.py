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
    """Return zone_ids whose device fingerprint changed, plus the fresh map.

    Fingerprint = `[sorted shortSerialNo list, sorted currentFwVersion list]`
    per capability zone. A changed serial set (hardware swap / re-pair) or
    firmware version (new fan / swing modes) means the cached capabilities may
    be stale. connectionState is deliberately excluded so an offline / online
    flip does not trigger a capabilities re-fetch (quota waste).

    A zone absent from `prev_fp` is treated as no baseline, so it is not
    flagged changed (no false positive on first poll / fresh install); the
    returned map is JSON-serialisable for sidecar persistence. A combi
    hot-water zone reports no devices, so its fingerprint is empty and never
    changes.

    Uses shortSerialNo because the full serial isn't in zones_info device
    records; colliding short serials would mask a change on re-pair, and the
    refresh button is the manual fallback for that vendor-data edge.
    """
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
