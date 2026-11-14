"""Tado CE optimistic state management: 4-layer stale data defense.

Layer 1: Time-window freshness (_optimistic_set_at)
Layer 2: Sequence numbers (_optimistic_sequence)
Layer 3: Expected-state confirmation (_expected_* fields)
Layer 4: Zone-fetch epoch (_optimistic_zone_fetch_epoch): a poll that started after the write bypasses the
freshness skip and may EXPIRE it; with no such poll a cloud hold is released at the write-protection window.
"""

from __future__ import annotations

from enum import Enum
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.util import dt as dt_util

from .const import DEFAULT_OPTIMISTIC_WINDOW_SECONDS
from .zone_signals import fresher_than

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator
    from .zone_write_ledger import IntentKind, ResendEntry, RestorePlan, WriteIntent, ZoneWriteLedger

_LOGGER = logging.getLogger(__name__)

# All optimistic tracking fields across entity types.
# clear_optimistic_state() resets every field present on the entity to None.
_OPTIMISTIC_FIELDS = (
    # Universal fields (all entity types)
    "_optimistic_set_at",
    "_optimistic_sequence",
    "_optimistic_preserved",
    "_optimistic_zone_fetch_epoch",
    # Climate entities (climate_heating.py, climate_ac.py)
    "_expected_hvac_mode",
    "_expected_hvac_action",
    "_expected_target_temperature",
    "_expected_overlay_present",
    # AC fan and swing axes (climate_ac.py)
    "_expected_fan_mode",
    "_expected_swing_mode",
    "_expected_swing_horizontal_mode",
    # Water heater entities
    "_expected_operation",
    "_expected_temperature",
    # Select entities
    "_expected_mode",
)

_EXPECTED_FIELDS = tuple(field for field in _OPTIMISTIC_FIELDS if field.startswith("_expected_"))


class OptimisticUpdateResult(Enum):
    """Result of optimistic update resolution."""

    ACCEPT_API = "accept_api"
    PRESERVE_OPTIMISTIC = "preserve"
    EXPIRED = "expired"
    DROPPED = "dropped"


def _ledger_slot(entity: object) -> tuple[ZoneWriteLedger, str, str] | None:
    """The ledger, zone id and ledger key for an entity; None for entities without a zone or a ledger."""
    ledger = getattr(getattr(entity, "coordinator", None), "write_ledger", None)
    zone_id = getattr(entity, "_zone_id", None)
    unique_id = getattr(entity, "unique_id", None)
    # HA re-adds the same entity under its new id on a rename, so the key is the unique id, not the entity id.
    key = unique_id if isinstance(unique_id, str) and unique_id else getattr(entity, "entity_id", None)
    if ledger is None or not zone_id or not key:
        return None
    return ledger, zone_id, key


def _record[T](entity: object, action: Callable[[ZoneWriteLedger, str, str], T]) -> T | None:
    """Run one helper call against the zone write ledger; a fault in it is logged and never fails the write."""
    slot = _ledger_slot(entity)
    if slot is None:
        return None
    ledger, zone_id, entity_id = slot
    try:
        result = action(ledger, zone_id, entity_id)
    except Exception:  # broad on purpose: the ledger must never fail the write, and the next arm resyncs the zone
        first = ledger.note_recorder_fault(zone_id)
        if first:
            _LOGGER.warning("%s: write ledger could not record this change", entity_id, exc_info=True)
        else:
            _LOGGER.debug("%s: write ledger could not record this change again", entity_id, exc_info=True)
        return None
    ledger.note_recorder_ok()
    return result


def _null_optimistic_fields(entity: object) -> None:
    for field in _OPTIMISTIC_FIELDS:
        if hasattr(entity, field):
            setattr(entity, field, None)


def clear_optimistic_state(entity: object) -> None:
    """Reset every optimistic-tracking field present on the entity to None."""
    _null_optimistic_fields(entity)
    _record(entity, lambda ledger, zone, eid: ledger.release(zone, entity_id=eid))


def clear_homekit_write(entity: object) -> None:
    """Forget the entity's last write source and the ledger's put record so nothing is resent."""
    # Not part of clear_optimistic_state: the resolver clears that first and then reads the source.
    if hasattr(entity, "_last_write_source"):
        entity._last_write_source = ""
    _record(entity, lambda ledger, zone, eid: ledger.clear_put(zone, entity_id=eid))


def mark_homekit_put(entity: object, *, payload: dict[str, Any] | None = None) -> None:
    """Record a HomeKit put's source, time and the zone's one write stamp so a later poll can judge it; `payload` is what an EXPIRED put resends."""
    entity._last_write_source = "homekit"  # type: ignore[attr-defined]
    put_at = dt_util.utcnow()
    _record(entity, lambda ledger, zone, eid: ledger.record_put(zone, entity_id=eid, at=put_at))
    if payload is not None:
        stash_resend(entity, payload=payload)


def note_ambiguous_put(entity: object) -> None:
    """Record on the zone that its HomeKit put may have landed though the caller fell back to the cloud."""
    _record(entity, lambda ledger, zone, _eid: ledger.note_ambiguous_put(zone))


def hold_locally(
    entity: object, *, kind: IntentKind, target: dict[str, Any], termination: Mapping[str, Any], ambiguous: bool,
) -> None:
    """Keep the change the bridge took while the cloud was down, beyond the entity's hold."""
    _record(entity, lambda ledger, zone, _eid: ledger.hold_locally(zone, kind=kind, target=target, termination=termination, ambiguous=ambiguous))


def supersede_owed(entity: object) -> None:
    """End the debt of a change held for the entity's zone, for a write that never reaches the arming step (an AUTO the reserve check refuses)."""
    _record(entity, lambda ledger, zone, _eid: ledger.supersede(zone))


def put_time(entity: object) -> datetime | None:
    """When the entity's HomeKit put was made, or None when it has none or a confirming fetch released it."""
    slot = _ledger_slot(entity)
    return None if slot is None else slot[0].put_time(slot[1])


def stash_resend(entity: object, *, payload: dict[str, Any] | None = None, target_temp: float | None = None) -> None:
    """Keep what a HomeKit put that Tado never shows is resent with through the cloud."""
    _record(entity, lambda ledger, zone, eid: ledger.stash_resend(zone, entity_id=eid, payload=payload, target_temp=target_temp))


def resend_entry(entity: Any, *, power: str, celsius: float | None) -> ResendEntry | None:
    """What a resend of `power` and `celsius` is judged against; take it before the put record is cleared."""
    return _record(entity, lambda ledger, zone, eid: ledger.resend_entry(zone, eid, entity, power=power, celsius=celsius))


def take_resend_payload(entity: object) -> dict[str, Any] | None:
    """Hand over the heating payload to resend, once."""
    return _record(entity, lambda ledger, zone, eid: ledger.take_resend_payload(zone, entity_id=eid))


def take_resend_target_temp(entity: object) -> float | None:
    """Hand over the AC target to resend, once."""
    return _record(entity, lambda ledger, zone, eid: ledger.take_resend_target_temp(zone, entity_id=eid))


def _stamp_hold(entity: object, coordinator: TadoDataUpdateCoordinator) -> int:
    seq = coordinator.get_next_sequence()
    entity._optimistic_set_at = time.monotonic()  # type: ignore[attr-defined]
    entity._optimistic_sequence = seq  # type: ignore[attr-defined]
    entity._optimistic_zone_fetch_epoch = coordinator.zone_fetch_epoch_for_write()  # type: ignore[attr-defined]
    if hasattr(entity, "_last_write_sequence"):
        # Survives confirmation, unlike the source, so a held resend can tell a newer write happened
        entity._last_write_sequence = seq
    return seq


def seed_accepted(entity: object, attrs: Mapping[str, Any]) -> None:
    """Start what Tado is known to hold from the card before a write changes it, where no poll or write has set it."""
    _record(entity, lambda ledger, zone, _eid: ledger.seed(zone, attrs))


def observe_accepted(entity: object, attrs: Mapping[str, Any]) -> None:
    """Record the values the card just took from Tado as what Tado holds."""
    _record(entity, lambda ledger, zone, _eid: ledger.observe(zone, attrs))


def accepted_values(entity: object, names: Iterable[str]) -> dict[str, Any]:
    """What Tado is known to hold for each named attribute that has a value."""
    values = _record(entity, lambda ledger, zone, _eid: ledger.accepted_values(zone, names))
    return values if isinstance(values, dict) else {}


def find_intent(entity: object, seq: int | None) -> WriteIntent | None:
    """The zone's live intent that holds sequence `seq`, or None."""
    return _record(entity, lambda ledger, zone, _eid: ledger.find_intent(zone, seq))


def accept_write(entity: object, intent: WriteIntent | None, assigned: Mapping[str, Any]) -> None:
    """Record that Tado took the write, so its assigned attributes are what Tado holds; a write whose intent was replaced still lands."""
    if intent is None:
        record_sent_attrs(entity, assigned)
        return
    _record(entity, lambda ledger, _zone, _eid: ledger.accept(intent, assigned))


def record_sent_attrs(entity: object, attrs: Mapping[str, Any]) -> None:
    """Record the attributes an accepted PUT carried as what Tado holds."""
    _record(entity, lambda ledger, zone, _eid: ledger.accept(None, attrs, zone_id=zone))


def fail_write(entity: object, intent: WriteIntent | None, names: Iterable[str]) -> RestorePlan | None:
    """What a failed write puts back, decided now; None when the zone has no ledger."""
    return _record(entity, lambda ledger, _zone, _eid: ledger.fail(intent, names))


def rearm_hold(entity: Any, intent: WriteIntent) -> None:
    """Hold the card again for an accepted write whose echo is still due, after a newer write failed and cleared the hold."""
    seq = _stamp_hold(entity, entity.coordinator)
    for key, value in intent.target.items():
        setattr(entity, f"_expected_{key}", value)
    _record(entity, lambda ledger, _zone, _eid: ledger.reinstate(intent, seq))


def restart_hold_clock(entity: object, coordinator: TadoDataUpdateCoordinator) -> None:
    """Give the held write a new time, sequence and zone-fetch epoch; the expected values stay as they are."""
    seq = _stamp_hold(entity, coordinator)
    _record(entity, lambda ledger, zone, eid: ledger.restart_clock(zone, entity_id=eid, seq=seq))


async def set_optimistic_fields(
    entity: object,
    coordinator: TadoDataUpdateCoordinator,
    *,
    source: Literal["homekit", "cloud"],
    expected: dict[str, Any] | None = None,
    preserved_attrs: dict[str, Any] | None = None,
) -> None:
    """Mark an entity as having an in-flight optimistic update.

    Stamps the entity with the current monotonic time, a coordinator
    sequence number, and the current zone-fetch epoch, records each
    key in `expected` as `_expected_{key}` on the entity, optionally
    stores extra attributes to preserve during the window (AC fan /
    swing modes etc.), and marks the entity fresh in the coordinator.
    The mark must be awaited before any async_write_ha_state() call
    so the next poll sees the updated freshness flag.
    """
    seq = _stamp_hold(entity, coordinator)
    if hasattr(entity, "_last_write_source"):
        entity._last_write_source = source

    # A new write replaces the previous expectations instead of inheriting the keys it omits.
    for field in _EXPECTED_FIELDS:
        if hasattr(entity, field):
            setattr(entity, field, None)

    if expected:
        for key, value in expected.items():
            setattr(entity, f"_expected_{key}", value)

    if preserved_attrs is not None:
        entity._optimistic_preserved = preserved_attrs  # type: ignore[attr-defined]

    _record(entity, lambda ledger, zone, eid: ledger.arm(zone, entity_id=eid, seq=seq, source=source, expected=expected))

    await coordinator.mark_entity_fresh(entity.entity_id)  # type: ignore[attr-defined]

    _LOGGER.debug(
        "%s: optimistic update set, expected=%s, seq=%s",
        getattr(entity, "_zone_name", entity.entity_id),  # type: ignore[attr-defined]
        expected,
        entity._optimistic_sequence,  # type: ignore[attr-defined]
    )


def is_within_optimistic_window(
    hass: HomeAssistant,
    optimistic_set_at: float | None,
    entry_id: str | None = None,
) -> bool:
    """Return True when the elapsed time since `optimistic_set_at` is still inside the window."""
    if optimistic_set_at is None:
        return False
    from .helpers import get_optimistic_window

    elapsed = time.monotonic() - optimistic_set_at
    return elapsed < get_optimistic_window(hass, entry_id=entry_id) if hass else elapsed < DEFAULT_OPTIMISTIC_WINDOW_SECONDS


def fetch_landed_since_write(entity: object) -> bool:
    """Return True when the published zone fetch started after the entity's own write."""
    if getattr(entity, "_optimistic_sequence", None) is None:
        return False
    coordinator = getattr(entity, "coordinator", None)
    if coordinator is None:
        return False
    # No fetch had started at the write, so any landed fetch began after it.
    return fresher_than(coordinator.last_zone_fetch_ts(), getattr(entity, "_optimistic_zone_fetch_epoch", None))


def _has_fresher_zone_poll(entity: object) -> bool:
    """Return True when a zone fetch has landed since the write, or when there is no coordinator to ask."""
    if getattr(entity, "coordinator", None) is None:
        return True
    return fetch_landed_since_write(entity)


def _request_still_queued(entity: object) -> bool:
    """Return True from the announce of a Smart Actions write until its PUT starts."""
    slot = _ledger_slot(entity)
    queued = None if slot is None else slot[0].queued(slot[1])
    if isinstance(queued, bool):
        return queued
    # No ledger (or a stand-in for one): the timer's own set is the only record of a waiting write.
    debouncer = getattr(getattr(entity, "coordinator", None), "action_debouncer", None)
    return debouncer is not None and getattr(entity, "_zone_id", None) in debouncer.pending_zones


def _queued_exemption_applies(entity: object) -> bool:
    """Return True while a request is still queued and its burst is younger than the hold release."""
    if not _request_still_queued(entity):
        return False
    slot = _ledger_slot(entity)
    age = None if slot is None else slot[0].queued_age(slot[1])
    # Retaps restart the debouncer but not the burst's age, so a stream of taps cannot pin the card for ever.
    return not isinstance(age, float) or age < _hold_release_seconds()


def _hold_release_seconds() -> float:
    """Return how long an unconfirmed write may be held while no zone fetch lands."""
    from .state_reconciler import WRITE_PROTECTION_WINDOW

    return WRITE_PROTECTION_WINDOW.total_seconds()


def is_held_locally(entity: object) -> bool:
    """Whether the entity's own HomeKit put is a held one (sent while the cloud was down, so nothing is resent for it)."""
    slot = _ledger_slot(entity)
    return slot is not None and slot[0].put_is_held(slot[1], slot[2]) is True


def _record_write_timing(entity: object, outcome: str) -> None:
    """Hand one judging to the coordinator's write timing recorder, if it has one."""
    recorder = getattr(getattr(entity, "coordinator", None), "write_timing", None)
    # A write still queued behind the debounce has not been sent, so no fetch has judged it yet.
    if recorder is not None and not _request_still_queued(entity):
        recorder.record(entity, outcome)


def _drop_homekit_write(entity: object, reason: str) -> OptimisticUpdateResult:
    """Forget a HomeKit write nothing will resend, so the next poll shows Tado's value."""
    name = getattr(entity, "_zone_name", getattr(entity, "entity_id", "?"))
    if is_held_locally(entity):
        _LOGGER.info("%s: the room is showing what the thermostat reports; the change made while Tado was unreachable is still held", name)
    else:
        _LOGGER.info("%s: HomeKit change dropped, %s, showing Tado's reported state", name, reason)
    clear_optimistic_state(entity)
    clear_homekit_write(entity)
    return OptimisticUpdateResult.DROPPED


def _judge_homekit_write(entity: object, entry_id: str | None) -> OptimisticUpdateResult | None:
    """Ask the ledger to judge an unconfirmed HomeKit put; None leaves other writes to the generic logic."""
    slot = _ledger_slot(entity)
    if slot is None or getattr(entity, "_last_write_source", "") != "homekit":
        return None
    ledger, zone_id, entity_id = slot
    judged = ledger.judge_put(zone_id, entity_id=entity_id, hass=getattr(entity, "hass", None), entry_id=entry_id)
    if judged is None:
        return None
    if judged.result is OptimisticUpdateResult.DROPPED:
        from .zone_write_ledger import DROPPED_BY_NEWER_CHANGE  # the ledger module imports this one

        # Only a newer change makes the state different; the other drops are give-ups, counted as expired.
        _record_write_timing(entity, "different" if judged.reason == DROPPED_BY_NEWER_CHANGE else "expired")
        return _drop_homekit_write(entity, judged.reason or "")
    if judged.result is OptimisticUpdateResult.EXPIRED:
        _record_write_timing(entity, "expired")
        # The ledger already released its hold and kept the put time for the caller's log.
        _null_optimistic_fields(entity)
    elif judged.result is OptimisticUpdateResult.PRESERVE_OPTIMISTIC:
        # The recorder counts it only when a fetch newer than the write exists.
        _record_write_timing(entity, "different")
    return judged.result


def resolve_optimistic_update(
    entity: object,
    *,
    api_values: dict[str, Any],
    entry_id: str | None = None,
) -> OptimisticUpdateResult:
    """Decide whether to keep optimistic state or accept the API-reported values.

    Three layers, in order: sequence + expected-state confirmation
    (preferred), time-window fallback (when the entity has no
    sequence), and expiry (sequence set but window elapsed without
    confirmation, and a zone poll newer than the write has landed; with no
    such poll a cloud-source hold is released at the write-protection window).
    A `_expected_*` field of None counts as "don't check this key".
    """
    seq = getattr(entity, "_optimistic_sequence", None)
    set_at = getattr(entity, "_optimistic_set_at", None)

    if seq is None and set_at is None:
        return OptimisticUpdateResult.ACCEPT_API

    if seq is not None:
        all_confirmed = True
        for key, api_val in api_values.items():
            expected_val = getattr(entity, f"_expected_{key}", None)
            if expected_val is not None and expected_val != api_val:
                all_confirmed = False
                break

        if all_confirmed:
            _LOGGER.debug(
                "%s: API confirmed optimistic state, clearing tracking",
                getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
            )
            _record_write_timing(entity, "confirmed")
            clear_optimistic_state(entity)
            return OptimisticUpdateResult.ACCEPT_API

        verdict = _judge_homekit_write(entity, entry_id)
        if verdict is not None:
            return verdict

        # Window elapsed without confirmation: the write likely
        # failed silently, so accept API state to avoid the entity
        # getting stuck on a value Tado never agreed to — unless no
        # fresher zone poll has landed yet to prove that.
        hass: HomeAssistant | None = getattr(entity, "hass", None)
        window_elapsed = set_at is not None and not is_within_optimistic_window(
            hass, set_at, entry_id=entry_id,  # type: ignore[arg-type]
        )
        # While polling is paused no fetch can land, so the absence of one proves nothing about the write.
        polling_paused = getattr(getattr(entity, "coordinator", None), "polling_paused", False) is True
        if window_elapsed and not _has_fresher_zone_poll(entity):
            if (
                not polling_paused
                and set_at is not None
                and time.monotonic() - set_at >= _hold_release_seconds()
            ):
                _LOGGER.info(
                    "%s: no zone fetch landed within %.0f s of the write, "
                    "releasing the hold and showing Tado's last reported state",
                    getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
                    _hold_release_seconds(),
                )
                clear_optimistic_state(entity)
                return OptimisticUpdateResult.ACCEPT_API
            _LOGGER.debug(
                "%s: optimistic window elapsed but no fresher zone poll "
                "yet, holding",
                getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
            )
            return OptimisticUpdateResult.PRESERVE_OPTIMISTIC

        # A request the Smart Actions debouncer still holds has not been sent, so no poll can have judged it.
        if window_elapsed and _queued_exemption_applies(entity):
            _LOGGER.debug(
                "%s: optimistic window elapsed but the request is not sent yet, holding",
                getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
            )
            return OptimisticUpdateResult.PRESERVE_OPTIMISTIC

        if window_elapsed:
            _LOGGER.warning(
                "%s: optimistic window expired without API confirmation, "
                "accepting Tado's reported state",
                getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
            )
            _record_write_timing(entity, "expired")
            clear_optimistic_state(entity)
            return OptimisticUpdateResult.EXPIRED

        _record_write_timing(entity, "different")
        _LOGGER.debug(
            "%s: preserving optimistic state. API has not yet confirmed",
            getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
        )
        return OptimisticUpdateResult.PRESERVE_OPTIMISTIC

    # Time-window fallback (entity has no sequence, e.g. some switches).
    hass = getattr(entity, "hass", None)
    if hass and is_within_optimistic_window(hass, set_at, entry_id=entry_id):
        _LOGGER.debug(
            "%s: preserving optimistic state, still inside time window",
            getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
        )
        return OptimisticUpdateResult.PRESERVE_OPTIMISTIC

    clear_optimistic_state(entity)
    return OptimisticUpdateResult.ACCEPT_API


def zone_owes_change(entity: object) -> bool:
    """Whether a change held for the entity's zone is still owed, so the card's data cannot say what the room holds."""
    return _record(entity, lambda ledger, zone, _eid: ledger.owes_change(zone)) is True
