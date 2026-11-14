"""Per-zone ledger of write intents; it holds the HomeKit put record, judges it and keeps the agreement sample."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .cloud_health import CloudHealthState
from .const import (
    HOMEKIT_EXPIRE_WINDOW_SECONDS,
    HOMEKIT_PUT_PENDING_MAX_SECONDS,
    MECH_HK_PUT_AGREE,
    VERIFY_FETCH_BY_KIND,
)
from .error_dispatch import handle_background_write_error
from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError
from .helpers import get_homekit_cloud_lag_seconds, get_zone_state, get_zone_states, predicted_next_reset
from .mechanism_counters import put_agree_bucket, put_agrees
from .optimistic_helpers import OptimisticUpdateResult, _hold_release_seconds
from .ratelimit import async_check_reserve_with_snapshot
from .schedule_helpers import next_schedule_change
from .write_optimizer import ActionDebouncer
from .zone_signals import fetch_covers_write

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterable

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# PUTs a refused resend may try in all, and how long a held one waits for the poll that follows the cloud's return.
DROPPED_BY_NEWER_CHANGE = "another change reached the zone after it"
MAX_RESEND_ATTEMPTS = 3
_RESEND_PUBLISH_WAIT_SECONDS = 120

# A change held over HomeKit is dropped after a day, tried at most three times, and re-judged no sooner than ten minutes after a failed try.
OWED_MAX_AGE = timedelta(hours=24)
MAX_OWED_ATTEMPTS = 3
_OWED_RETRY_SECONDS = 600
_OWED_MAX_WAITS = 3
# Seconds before each zone-fetch probe while the cloud is unreachable and a change is owed; the last step repeats.
_OWED_PROBE_STEPS = (600, 1200, 1800)
_TEMPERATURE_TOLERANCE = 0.05


class IntentKind(StrEnum):
    """What a write changes."""

    TEMPERATURE = "temperature"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    RESUME = "resume"
    TIMER = "timer"
    MODE = "mode"
    FAN = "fan"
    SWING = "swing"
    OPERATION = "operation"


class IntentState(StrEnum):
    """Where a write is in its life."""

    QUEUED = "queued"
    SENDING = "sending"
    SENT_UNKNOWN = "sent_unknown"
    ACKED = "acked"
    HELD_LOCAL = "held_local"
    CONFIRMED = "confirmed"
    DROPPED = "dropped"


# First expected key that names the kind; a write whose keys match none has no kind yet.
_KIND_BY_EXPECTED_KEY = (
    ("hvac_mode", IntentKind.MODE),
    ("operation", IntentKind.OPERATION),
    ("fan_mode", IntentKind.FAN),
    ("swing_mode", IntentKind.SWING),
    ("swing_horizontal_mode", IntentKind.SWING),
    ("target_temperature", IntentKind.TEMPERATURE),
    ("temperature", IntentKind.TEMPERATURE),
)


def kind_for_expected(expected: dict[str, Any] | None) -> IntentKind | None:
    """Name the kind of a write from the expected keys it sets."""
    keys = expected or {}
    return next((kind for key, kind in _KIND_BY_EXPECTED_KEY if key in keys), None)


@dataclass
class OwnerStamp:
    """What the zone looked like when a write was put, so a later judge can tell whose change it sees."""

    write_stamp: datetime | None = None
    write_count: int | None = None
    polled_signature: tuple[Any, ...] | None = None
    fetch_epoch: datetime | None = None


@dataclass
class WriteIntent:
    """One write the integration made to a zone."""

    zone_id: str
    entity_id: str
    seq: int
    source: str
    target: dict[str, Any]
    created_at: datetime
    kind: IntentKind | None = None
    state: IntentState = IntentState.SENDING
    termination: str | None = None
    sent_at: datetime | None = None
    fallback: bool = False
    owner: OwnerStamp | None = None
    # A held change whose HomeKit put may not have landed (it timed out), so the card shows the request until a read corrects it.
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """What a failed write puts back, and the older accepted write whose hold the card keeps waiting for."""

    values: dict[str, Any] = field(default_factory=dict)
    rearm: WriteIntent | None = None


@dataclass
class _PutRecord:
    """The HomeKit put a poll may judge later, with the payload or target a failed put resends."""

    entity_id: str
    at: datetime | None = None
    stamp: datetime | None = None
    source: str = "homekit"
    payload: dict[str, Any] | None = None
    target_temp: float | None = None
    # The zone's write count and polled signature at the put, which a resend of it is judged against.
    count: int | None = None
    polled: tuple[Any, Any, Any] | None = None
    # Set when the put was sent while the cloud was down and held: nothing is resent for it.
    held: bool = False


@dataclass
class PutJudgement:
    """What a poll made of an unconfirmed HomeKit put; the reason is set only when the put is dropped."""

    result: OptimisticUpdateResult
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResendEntry:
    """What a resend carries and what its ownership check compares the zone against: the zone as it was at the HomeKit put."""

    entity: Any
    name: str
    power: str
    celsius: float | None
    count: int
    seq: int | None
    polled: tuple[Any, Any, Any]
    put_at: datetime
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class OwnerVerdict:
    """Whether a resend may go out, and why not when it may not."""

    send: bool
    reason: str


_SEND = OwnerVerdict(True, "nobody else changed the zone")


class HeldAction(StrEnum):
    """What the ownership table makes of a change held over HomeKit once the cloud is back."""

    APPLY = "apply"
    CONFIRMED = "confirmed"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class HeldVerdict:
    """The table's verdict for one owed change and the reason in words."""

    action: HeldAction
    reason: str


@dataclass(frozen=True, slots=True)
class _CloudView:
    """What Tado's latest zone fetch says about a zone."""

    power: str
    celsius: float | None
    overlay: bool
    termination: str | None


@dataclass
class _HeldResend:
    """A refused resend kept for a cloud recovery or one quota reset."""

    send: Callable[[], Coroutine[Any, Any, None]]
    entry: ResendEntry
    cancel_timer: CALLBACK_TYPE | None = None


@dataclass
class _ZoneRecord:
    intents: list[WriteIntent] = field(default_factory=list)
    put: _PutRecord | None = None
    # The newest put's monotonic time and payload, kept until a fetch can say whether Tado agrees.
    sample: tuple[float, dict[str, Any]] | None = None
    # When our last accepted OFF write was made, until a fetch that started past the cloud lag has shown it.
    own_off: datetime | None = None
    # State Tado holds while a debounced write is queued or in flight; only trusted while one is.
    rollback: dict[str, Any] | None = None
    # The hold the queued tap itself set; None for a queued write that stamps nothing (HEAT from OFF).
    stamp: int | None = None
    # A debounced write was announced and its PUT has not started; set at the announce, so the instant between the timer firing and the PUT starting is covered.
    queued: bool = False
    # Debounced writes of the zone whose PUT is running.
    running: int = 0
    # Monotonic time the first tap of the current burst was queued; a retap does not move it.
    queued_since: float | None = None
    # A HomeKit put for this zone may have landed though the entity fell back to the cloud; kept across the re-arm, closed with the entity's release.
    ambiguous_put: bool = False
    # When a write of unknown outcome (timeout, lost connection) was given up; the revert does not clear it, only a covering fetch or the protection window does.
    unknown_at: datetime | None = None
    # What Tado is known to hold, by card attribute: the last poll the card took, then every accepted write.
    accepted: dict[str, Any] = field(default_factory=dict)
    # The newest accepted write that a newer write has since replaced and whose echo has not been seen.
    awaiting: WriteIntent | None = None
    # Fair FIFO: the zone's overlay requests reach Tado in the order they asked for the slot.
    order: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Writes asking for the slot, and the tasks to cancel when the zone is dropped or the entry unloads.
    waiting: int = 0
    waiters: set[asyncio.Task[Any]] = field(default_factory=set)
    # The task inside the slot now, so its own later legs enter without waiting.
    owner: asyncio.Task[Any] | None = None
    # The change sent over HomeKit while the cloud was down, kept past the entity's hold; memory only.
    owed: WriteIntent | None = None
    # PUTs tried for the owed change, publish waits that ended with no newer zone fetch, and the earliest time it is judged again.
    owed_attempts: int = 0
    owed_waits: int = 0
    owed_retry_at: datetime | None = None
    # When the owed change was last sent; a new recovery does not shorten the spacing between tries.
    owed_last_try: datetime | None = None


class ZoneWriteLedger:
    """Keeps one record per zone; the entity that armed a write is the only one that may close it."""

    def __init__(self, coordinator: TadoDataUpdateCoordinator) -> None:
        # Held, not the reconciler: the reconciler is assigned after the coordinator is built.
        self._coordinator = coordinator
        self._records: dict[str, _ZoneRecord] = {}
        # Zones whose recorder failed part-way; the next arm starts them from an empty record.
        self._stale: set[str] = set()
        self._fault_open = False
        self._sweep_fault_open = False
        # In hold order, so a recovery releases the zones in the order they were refused.
        self._held: dict[str, _HeldResend] = {}
        self._tasks: dict[str, set[asyncio.Future[Any]]] = {}
        # Serialises the reserve check and send of owed re-applies across zones.
        self._reserve_lock = asyncio.Lock()
        self._health: Any = None
        self._health_unsub: CALLBACK_TYPE | None = None
        # The one wait for the next data publish after the cloud returns, and its timeout.
        self._publish_unsub: CALLBACK_TYPE | None = None
        self._publish_timeout: CALLBACK_TYPE | None = None
        # The zone fetch the coordinator's data carried when the wait began.
        self._publish_epoch: datetime | None = None
        # When the cloud last came back; an owed change is judged only against a zone fetch that started after it.
        self._recovered_at: datetime | None = None
        # The tracked timer of the owed-change probe and the step it fires next.
        self._probe_cancel: CALLBACK_TYPE | None = None
        self._probe_step = 0
        # Set by shutdown so no listener or timer is armed again.
        self._closed = False

    def note_recorder_fault(self, zone_id: str) -> bool:
        """Mark the zone for a resync on its next arm; True for the first fault of an episode."""
        self._stale.add(zone_id)
        first = not self._fault_open
        self._fault_open = True
        return first

    def note_recorder_ok(self) -> None:
        """End the fault episode, so the next failure is logged in full again."""
        self._fault_open = False

    def arm(
        self, zone_id: str, *, entity_id: str, seq: int, source: str, expected: dict[str, Any] | None,
        kind: IntentKind | None = None,
    ) -> WriteIntent:
        """Record a new write as the zone's one live intent, replacing any older one."""
        record = self._records.setdefault(zone_id, _ZoneRecord())
        # A new write to the zone ends the debt of an older held one, successful or not.
        self.supersede(zone_id)
        if zone_id in self._stale:
            # A recorder failed part-way, so the put held here may be one the entity already dropped.
            self._stale.discard(zone_id)
            record.put = None
        intent = WriteIntent(
            zone_id=zone_id, entity_id=entity_id, seq=seq, source=source, target=dict(expected or {}),
            created_at=dt_util.utcnow(), kind=kind if kind is not None else kind_for_expected(expected),
        )
        older = record.intents[-1] if record.intents else None
        if older is not None and older.state is IntentState.ACKED:
            # A HomeKit put keeps its resend payload with the put record, which a revert clears, so only a cloud write is held for again.
            if older.source != "homekit":
                record.awaiting = older
        elif older is not None and older.state is IntentState.SENDING and zone_id in self._debouncer_pending() and not record.running:
            # A debounced newer write replaces the tap's call; a non-debounced one does not, and the timer still sends the tap, whose success is then recorded without an intent.
            older.state = IntentState.DROPPED
        record.intents = [intent]
        _LOGGER.debug("Write ledger: zone %s armed seq %s source %s", zone_id, seq, source)
        return intent

    def restart_clock(self, zone_id: str, *, entity_id: str, seq: int) -> bool:
        """Move the entity's live intent to its new sequence; False when it has none."""
        intent = self._own_live(zone_id, entity_id)
        if intent is None:
            return False
        intent.seq = seq
        return True

    def release(self, zone_id: str, *, entity_id: str) -> None:
        """Close the entity's live intent and clear the put time only; a no-op for an entity that never armed."""
        record = self._records.get(zone_id)
        if record is None:
            return
        record.intents = [i for i in record.intents if i.entity_id != entity_id]
        if record.awaiting is not None and record.awaiting.entity_id == entity_id:
            record.awaiting = None
        record.ambiguous_put = False
        put = record.put
        if put is not None and put.entity_id == entity_id:
            # The entity's own clear nulls the put time and leaves the stamp, so the ledger keeps the same half.
            put.at = None
        _LOGGER.debug("Write ledger: zone %s released by %s", zone_id, entity_id)

    def seed(self, zone_id: str, attrs: Mapping[str, Any]) -> None:
        """Start the accepted state from the card as it was before a write, for the names no poll or write has set yet."""
        accepted = self._records.setdefault(zone_id, _ZoneRecord()).accepted
        for name, value in attrs.items():
            accepted.setdefault(name, value)

    def observe(self, zone_id: str, attrs: Mapping[str, Any] | None) -> None:
        """Refresh the accepted state from values the card just took from Tado; an older write's echo no longer matters."""
        if not attrs:
            return
        record = self._records.setdefault(zone_id, _ZoneRecord())
        record.accepted.update(attrs)
        record.awaiting = None

    def accepted_values(self, zone_id: str, names: Iterable[str]) -> dict[str, Any]:
        """The accepted value of each named attribute that has one."""
        record = self._records.get(zone_id)
        if record is None:
            return {}
        return {name: record.accepted[name] for name in names if name in record.accepted}

    def find_intent(self, zone_id: str, seq: int | None) -> WriteIntent | None:
        """The live intent of the zone that holds sequence `seq`, or None when a newer write replaced it or the hold is gone."""
        record = self._records.get(zone_id)
        if record is None or seq is None:
            return None
        return next((i for i in record.intents if i.seq == seq), None)

    def accept(self, intent: WriteIntent | None, assigned: Mapping[str, Any], *, zone_id: str | None = None) -> None:
        """Tado took a PUT: the attributes it carried are now what Tado holds; with no intent (replaced, or a payload read) only `zone_id`'s values change."""
        zone = intent.zone_id if intent is not None else zone_id
        if zone is None:
            return
        if intent is None:
            self._records.setdefault(zone, _ZoneRecord()).accepted.update(assigned)
            return
        record = self._records.get(zone)
        if record is None:
            return
        record.accepted.update(assigned)
        intent.state = IntentState.ACKED
        if record.intents and record.intents[-1] is intent:
            record.awaiting = None
        elif record.intents and intent.source != "homekit":
            # A newer write is already live, so this one is the accepted write whose echo the card still waits for.
            record.awaiting = intent

    def fail(self, intent: WriteIntent | None, names: Iterable[str]) -> RestorePlan:
        """Decide now what a failed write puts back: nothing when a newer write is live, else the accepted state of `names`."""
        record = self._records.get(intent.zone_id) if intent is not None else None
        if intent is None or record is None:
            return RestorePlan()
        intent.state = IntentState.DROPPED
        if not record.intents or record.intents[-1] is not intent:
            return RestorePlan()
        return RestorePlan(self.accepted_values(intent.zone_id, names), record.awaiting)

    def reinstate(self, intent: WriteIntent, seq: int) -> None:
        """Make an accepted write whose echo is still due the zone's live intent again, under a new hold."""
        record = self._records.get(intent.zone_id)
        if record is None:
            return
        intent.seq = seq
        record.intents = [intent]
        record.awaiting = None

    def record_put(self, zone_id: str, *, entity_id: str, at: datetime) -> None:
        """Stamp the zone once for a HomeKit put, then record its time and that stamp, keeping any payload held; the live intent becomes a HomeKit one."""
        reconciler = getattr(self._coordinator, "state_reconciler", None)
        # The only stamper of a put: unstamped, a replayed bridge target overwrites it; the snapshot must follow the stamp.
        if reconciler is not None:
            reconciler.record_local_write(zone_id)
        stamp = reconciler.last_write_stamp(zone_id) if reconciler is not None else None
        put = self._put_for(zone_id, entity_id)
        put.at = at
        put.stamp = stamp
        put.count = self._write_count(zone_id)
        put.polled = self._polled_now(zone_id)
        put.source = "homekit"
        put.held = False
        intent = self._own_live(zone_id, entity_id)
        if intent is not None:
            intent.source = "homekit"
            intent.owner = OwnerStamp(write_stamp=stamp)

    def note_ambiguous_put(self, zone_id: str) -> None:
        """Remember that a HomeKit put for the zone may have landed though the entity falls back to the cloud."""
        self._records.setdefault(zone_id, _ZoneRecord()).ambiguous_put = True

    def ambiguous_put(self, zone_id: str) -> bool:
        """True while a HomeKit put of the zone is of unknown outcome."""
        record = self._records.get(zone_id)
        return record is not None and record.ambiguous_put

    def mark_unknown(self, zone_id: str, sent_at: datetime | None = None) -> None:
        """Mark the zone unconfirmed after a write of unknown outcome, timed now unless given; a later one replaces the earlier time."""
        self._records.setdefault(zone_id, _ZoneRecord()).unknown_at = sent_at if sent_at is not None else dt_util.utcnow()

    def unconfirmed(self, zone_id: str) -> bool:
        """Whether a write of unknown outcome is still unanswered: no fetch has covered it and the protection window has not passed."""
        record = self._records.get(zone_id)
        if record is None or record.unknown_at is None:
            return False
        coordinator = self._coordinator
        sent_at = record.unknown_at
        lag = get_homekit_cloud_lag_seconds(coordinator.hass, coordinator.config_entry.entry_id)
        if (dt_util.utcnow() - sent_at).total_seconds() >= _hold_release_seconds() or fetch_covers_write(coordinator.last_zone_fetch_ts(), sent_at, lag):
            record.unknown_at = None
            return False
        return True

    def stash_resend(
        self, zone_id: str, *, entity_id: str, payload: dict[str, Any] | None = None, target_temp: float | None = None,
    ) -> None:
        """Keep what an EXPIRED put resends through the cloud: the heating payload or the AC target."""
        put = self._put_for(zone_id, entity_id)
        put.payload = payload
        put.target_temp = target_temp

    def put_time(self, zone_id: str) -> datetime | None:
        """When the zone's HomeKit put was made, or None once a confirming fetch released it or none was made."""
        record = self._records.get(zone_id)
        return record.put.at if record is not None and record.put is not None else None

    def verification_due(self, intent: WriteIntent | None) -> bool:
        """Whether the put's kind keeps its verification fetch; a missing intent or kind verifies, as today."""
        kind = intent.kind if intent is not None else None
        return True if kind is None else VERIFY_FETCH_BY_KIND.get(kind.value, True)

    def is_ours(self, zone_id: str, changed_at: datetime) -> bool:
        """Whether a change at `changed_at` falls within the cloud lag of the zone's own put or live write."""
        record = self._records.get(zone_id)
        if record is None:
            return False
        times = [record.put.at if record.put is not None else None]
        times.extend(i.sent_at or i.created_at for i in record.intents)
        coordinator = self._coordinator
        lag = get_homekit_cloud_lag_seconds(coordinator.hass, coordinator.config_entry.entry_id)
        return any(t is not None and abs((changed_at - t).total_seconds()) <= lag for t in times)

    def take_resend_payload(self, zone_id: str, *, entity_id: str) -> dict[str, Any] | None:
        """Hand over the heating payload to resend and forget it, so a second call gets None."""
        put = self._own_put(zone_id, entity_id)
        if put is None:
            return None
        payload, put.payload = put.payload, None
        return payload

    def take_resend_target_temp(self, zone_id: str, *, entity_id: str) -> float | None:
        """Hand over the AC target to resend and forget it, so a second call gets None."""
        put = self._own_put(zone_id, entity_id)
        if put is None:
            return None
        target, put.target_temp = put.target_temp, None
        return target

    def judge_put(
        self, zone_id: str, *, entity_id: str, hass: HomeAssistant | None, entry_id: str | None,
    ) -> PutJudgement | None:
        """Judge an unconfirmed HomeKit put by when the first fetch after it started; None when the entity has no put time."""
        put = self._own_put(zone_id, entity_id)
        if put is None or put.at is None:
            return None
        put_at = put.at
        coordinator = self._coordinator
        lag = get_homekit_cloud_lag_seconds(hass, entry_id)  # type: ignore[arg-type]
        started = coordinator.last_zone_fetch_ts()
        silent_bound = lag + HOMEKIT_EXPIRE_WINDOW_SECONDS
        silent_too_long = (dt_util.utcnow() - put_at).total_seconds() > silent_bound
        no_update = PutJudgement(OptimisticUpdateResult.DROPPED, f"Tado sent no zone update within {silent_bound:.0f} s of it")
        if started is None:
            return no_update if silent_too_long else PutJudgement(OptimisticUpdateResult.PRESERVE_OPTIMISTIC)
        age = (started - put_at).total_seconds()
        if age < lag:
            # A paused poll cannot land a fetch, so silence proves nothing; this fetch cannot judge the put, so only the clock bounds it.
            paused = getattr(coordinator, "polling_paused", False) is True
            return no_update if silent_too_long and not paused else PutJudgement(OptimisticUpdateResult.PRESERVE_OPTIMISTIC)
        if age > lag + HOMEKIT_EXPIRE_WINDOW_SECONDS:
            return PutJudgement(
                OptimisticUpdateResult.DROPPED, f"the zone update that checked it came {age:.0f} s after it, too late to tell whether it was applied",
            )
        reconciler = getattr(coordinator, "state_reconciler", None)
        if reconciler is not None and reconciler.last_write_stamp(zone_id) != put.stamp:
            return PutJudgement(OptimisticUpdateResult.DROPPED, DROPPED_BY_NEWER_CHANGE)
        self.release(zone_id, entity_id=entity_id)
        # Kept for the caller's elapsed-time log; clear_put removes it with the payload.
        put.at = put_at
        return PutJudgement(OptimisticUpdateResult.EXPIRED)

    def clear_put(self, zone_id: str, *, entity_id: str) -> bool:
        """Forget the entity's put record and blank the live intent's source, as the entity does; False when nothing was held."""
        record = self._records.get(zone_id)
        if record is None:
            return False
        held = False
        if record.put is not None and record.put.entity_id == entity_id:
            record.put = None
            held = True
        intent = self._own_live(zone_id, entity_id)
        if intent is not None:
            intent.source = ""
            held = True
        return held

    def note_saved(self, zone_id: str, payload: dict[str, Any] | None) -> None:
        """Start the agreement sample for a put that saved a cloud write; an earlier unanswered one is superseded."""
        record = self._records.get(zone_id)
        # An untimed write (no payload) still replaces what the earlier put asked for.
        if record is not None and record.sample is not None:
            record.sample = None
            self._coordinator.mechanism_counters.count(MECH_HK_PUT_AGREE, "superseded", calls=0)
        if payload:
            self._records.setdefault(zone_id, _ZoneRecord()).sample = (time.monotonic(), payload)

    def resolve_agreement(self, zone_data: dict[str, Any] | list[Any] | None, fetch_start: float | None) -> None:
        """Bucket each sampled put this fetch can answer once its zone agrees; sweep samples past the pending limit."""
        if fetch_start is None or not any(r.sample is not None for r in self._records.values()):
            return
        states = get_zone_states({"zones": zone_data})
        counters = self._coordinator.mechanism_counters
        for zone_id, record in list(self._records.items()):
            if record.sample is None:
                continue
            stamp, payload = record.sample
            # A put made after the fetch began cannot be answered by that fetch.
            if stamp > fetch_start:
                continue
            age = fetch_start - stamp
            state = states.get(zone_id)
            if age > HOMEKIT_PUT_PENDING_MAX_SECONDS:
                counters.count(MECH_HK_PUT_AGREE, "unagreed", calls=0)
            elif isinstance(state, dict) and put_agrees(payload, state):
                counters.count(MECH_HK_PUT_AGREE, put_agree_bucket(age), calls=0)
            else:
                continue
            record.sample = None

    def agreement_samples(self) -> dict[str, dict[str, Any]]:
        """The payload of each put still waiting for a fetch to answer it, for diagnostics and tests."""
        return {z: r.sample[1] for z, r in self._records.items() if r.sample is not None}

    def forget_zone(self, zone_id: str) -> bool:
        """Drop everything held for a zone that is gone; True when it held a record."""
        self.cancel_resends(zone_id)
        self._tasks.pop(zone_id, None)
        self._stale.discard(zone_id)
        record = self._records.pop(zone_id, None)
        if record is not None:
            self._cancel_waiters(record)
        if record is not None and record.sample is not None:
            # Same bucket the 900 s sweep gives a put no fetch ever answered.
            self._coordinator.mechanism_counters.count(MECH_HK_PUT_AGREE, "unagreed", calls=0)
        self._after_owed_change()
        return record is not None

    def note_own_power(self, zone_id: str, *, powered_off: bool) -> None:
        """Remember an accepted OFF write for the zone; an accepted ON write or AUTO forgets it."""
        if powered_off:
            self._records.setdefault(zone_id, _ZoneRecord()).own_off = dt_util.utcnow()
            return
        record = self._records.get(zone_id)
        if record is not None:
            record.own_off = None

    def own_off_mark(self, zone_id: str) -> bool:
        """Whether an own-OFF mark exists for the zone; unlike `own_off_pending` it never pops a stale one."""
        record = self._records.get(zone_id)
        return record is not None and record.own_off is not None

    def own_off_pending(self, zone_id: str) -> bool:
        """Whether our own accepted OFF write is still newer than any zone fetch that could show it."""
        record = self._records.get(zone_id)
        if record is None or record.own_off is None:
            return False
        coordinator = self._coordinator
        fetched = coordinator.last_zone_fetch_ts()
        if fetched is None:
            return True
        lag = get_homekit_cloud_lag_seconds(coordinator.hass, coordinator.config_entry.entry_id)
        if fetch_covers_write(fetched, record.own_off, lag):
            # Stale mark: a fetch that started at least the cloud lag after the write has shown the zone's power
            record.own_off = None
            return False
        return True

    def queue(self, zone_id: str, rollback: dict[str, Any] | None, stamp: int | None) -> None:
        """Announce a debounced write about to be queued: the state Tado holds and the hold its tap set."""
        record = self._records.setdefault(zone_id, _ZoneRecord())
        # Read before this tap joins the queue: only the first tap of a burst starts the age.
        if not self.queued_or_running(zone_id):
            record.queued_since = time.monotonic()
        record.rollback = rollback
        record.stamp = stamp
        record.queued = True
        # Hooked here, where the first announce needs it, so a debouncer built later (or swapped) is covered too.
        debouncer = getattr(self._coordinator, "action_debouncer", None)
        if isinstance(debouncer, ActionDebouncer):
            debouncer.on_cancel = self.note_timer_cancelled

    def queued(self, zone_id: str) -> bool:
        """Whether a write of the zone was announced, or is waiting for the zone's send slot, and its PUT has not started."""
        record = self._records.get(zone_id)
        return record is not None and (record.queued or record.waiting > 0)

    def note_timer_cancelled(self, zone_id: str) -> None:
        """The timer dropped the zone's waiting write unsent, so nothing is queued any more."""
        record = self._records.get(zone_id)
        if record is not None:
            record.queued = False

    def queued_age(self, zone_id: str) -> float | None:
        """Seconds since the first tap of the zone's current queued burst, or None when none is recorded."""
        record = self._records.get(zone_id)
        if record is None or record.queued_since is None:
            return None
        return time.monotonic() - record.queued_since

    def begin_send(self, zone_id: str) -> None:
        """A queued write's PUT starts running (QUEUED to SENDING)."""
        record = self._records.setdefault(zone_id, _ZoneRecord())
        record.running += 1
        # A newer tap announced while this one fired is still in the timer, and the flag follows it.
        record.queued = zone_id in self._debouncer_pending()

    def end_send(self, zone_id: str) -> None:
        """A running PUT ended; the baseline goes once nothing else is running and nothing is queued."""
        record = self._records.get(zone_id)
        if record is None:
            return
        # A PUT that ends after its zone was dropped finds a fresh record, so the count stops at zero.
        record.running = max(record.running - 1, 0)
        if not self.queued_or_running(zone_id):
            record.rollback = None
            record.stamp = None
            record.queued_since = None

    def queued_or_running(self, zone_id: str) -> bool:
        """Whether a write of the zone is waiting in the timer or for the send slot, or its PUT is running."""
        record = self._records.get(zone_id)
        return record is not None and (record.queued or record.running > 0 or record.waiting > 0)

    @asynccontextmanager
    async def zone_order(self, zone_id: str) -> AsyncIterator[bool]:
        """Hold the zone's send slot for one overlay request; yields True when an earlier request made it wait."""
        record = self._records.setdefault(zone_id, _ZoneRecord())
        task = asyncio.current_task()
        # The task that holds the slot goes on to its next leg (a HomeKit put, then its cloud fallback) without queuing behind itself.
        if task is not None and record.owner is task:
            yield False
            return
        # The wait belongs to the burst the hold bound reads, so the first waiter starts its age.
        if record.queued_since is None:
            record.queued_since = time.monotonic()
        # A free lock with a woken waiter still to run makes this one queue behind it, so the count is read too.
        waited = record.order.locked() or record.waiting > 0
        record.waiting += 1
        if task is not None:
            record.waiters.add(task)
        try:
            await record.order.acquire()
        finally:
            record.waiting -= 1
            if task is not None:
                record.waiters.discard(task)
            if not self.queued_or_running(zone_id):
                record.queued_since = None
        record.owner = task
        try:
            yield waited
        finally:
            record.owner = None
            record.order.release()

    def busy(self, zone_id: str) -> bool:
        """Whether a write of the zone holds the send slot, waits for it, or is queued."""
        record = self._records.get(zone_id)
        return record is not None and (record.order.locked() or self.queued_or_running(zone_id))

    @staticmethod
    def _cancel_waiters(record: _ZoneRecord) -> None:
        """Cancel the writes still waiting for the zone's send slot."""
        for task in tuple(record.waiters):
            task.cancel()

    def pending_rollback(self, zone_id: str) -> dict[str, Any] | None:
        """The state Tado holds while a debounced write is queued or in flight for the zone, else None."""
        record = self._records.get(zone_id)
        if record is None or record.rollback is None or not self.queued_or_running(zone_id):
            return None
        # An earlier write accepted after the tap was queued is what Tado holds now.
        return {**record.rollback, **self.accepted_values(zone_id, record.rollback)}

    def queued_tap_stamp(self, zone_id: str) -> int | None:
        """The hold of the tap still waiting in the timer, else None (a PUT already running cannot be dropped)."""
        # The timer, not the flag: once it has fired the task exists and a cancel can no longer stop it.
        if zone_id not in self._debouncer_pending():
            return None
        record = self._records.get(zone_id)
        return record.stamp if record is not None else None

    def cancel_queued(self, zone_id: str) -> dict[str, Any] | None:
        """Cancel the zone's queued write and return the rollback it held."""
        # Read before the cancel: the cancel empties the pending set the read checks.
        cancelled = self.pending_rollback(zone_id)
        record = self._records.get(zone_id)
        if record is not None:
            record.rollback = None
            record.stamp = None
            # A write still waiting for the send slot keeps the age its hold bound reads.
            if record.waiting == 0:
                record.queued_since = None
            record.queued = False
        debouncer = getattr(self._coordinator, "action_debouncer", None)
        if debouncer is not None:
            debouncer.cancel(zone_id)
        return cancelled

    def _debouncer_pending(self) -> Any:
        """The zones the timer still holds a write for; empty when the coordinator has no debouncer."""
        return getattr(getattr(self._coordinator, "action_debouncer", None), "pending_zones", ())

    def _write_count(self, zone_id: str) -> int:
        """How many writes from any writer the zone has been stamped with."""
        reconciler = getattr(self._coordinator, "state_reconciler", None)
        return int(reconciler.write_count(zone_id)) if reconciler is not None else 0

    def _polled_now(self, zone_id: str) -> tuple[Any, Any, Any]:
        """The latest polled zone's power, target temperature and overlay type; read from the poll, never from the card."""
        state = get_zone_state(getattr(self._coordinator, "data", None), zone_id)
        if not isinstance(state, dict):
            return None, None, None
        setting = state.get("setting")
        setting = setting if isinstance(setting, dict) else {}
        temperature = setting.get("temperature")
        celsius = temperature.get("celsius") if isinstance(temperature, dict) else None
        return setting.get("power"), celsius, state.get("overlayType")

    def resend_entry(self, zone_id: str, entity_id: str, entity: Any, *, power: str, celsius: float | None) -> ResendEntry:
        """The entry for a resend of `power` and `celsius`: the zone as it was at the entity's HomeKit put, else as it is now."""
        put = self._own_put(zone_id, entity_id)
        if put is not None and put.count is not None and put.polled is not None and put.at is not None:
            count, polled, put_at = put.count, put.polled, put.at
        else:
            count, polled, put_at = self._write_count(zone_id), self._polled_now(zone_id), dt_util.utcnow()
        return ResendEntry(
            entity=entity, name=getattr(entity, "_zone_name", zone_id), power=power, celsius=celsius,
            count=count, seq=entity._last_write_sequence, polled=polled, put_at=put_at,
        )

    def owned(self, zone_id: str, entry: ResendEntry) -> OwnerVerdict:
        """Whether the resend may go out: only while nobody but the HomeKit change has touched the zone; a drop logs why and what Tado shows."""
        verdict = self._judge_resend(zone_id, entry)
        if not verdict.send:
            _LOGGER.info("%s: resend dropped, %s", entry.name, verdict.reason)
        return verdict

    def _judge_resend(self, zone_id: str, entry: ResendEntry) -> OwnerVerdict:
        now = self._polled_now(zone_id)
        shown = f"Tado shows power {now[0]}, target {now[1]}, overlay {now[2]}"
        if self._write_count(zone_id) != entry.count or entry.entity._last_write_sequence != entry.seq:
            return OwnerVerdict(False, f"another change reached the zone after it ({shown})")
        if now == (None, None, None):
            return OwnerVerdict(False, f"Tado's zone data is not available, and nothing is written on unknown ({shown})")
        if now[0] == entry.power and (entry.power != "ON" or now[1] == entry.celsius):
            return OwnerVerdict(False, f"Tado already shows it ({shown})")
        if now != entry.polled:
            return OwnerVerdict(False, f"the zone was changed in Tado after it ({shown})")
        return self._device_verdict(zone_id, entry, shown)

    def _device_verdict(self, zone_id: str, entry: ResendEntry, shown: str) -> OwnerVerdict:
        """A HomeKit device that holds another value, changed after the put, was changed on the device or in Apple Home."""
        provider = getattr(self._coordinator, "homekit_provider", None)
        if provider is None:
            return _SEND
        readings: list[tuple[Any, Any]] = [(provider.homekit_power(zone_id), entry.power)]
        if entry.power == "ON":
            readings.append((provider.get_target_temperature(zone_id), entry.celsius))
        for reading, wanted in readings:
            moved = self._moved_after(reading, wanted, entry.put_at)
            if moved is not None:
                return OwnerVerdict(False, f"the HomeKit device was changed to {moved} after it ({shown})")
        return _SEND

    @staticmethod
    def _moved_after(reading: Any, wanted: Any, put_at: datetime) -> Any:
        """The value a HomeKit reading holds when it is not `wanted` and changed after `put_at`; None when it is unreadable, as wanted or older."""
        if not (isinstance(reading, tuple) and len(reading) == 3) or reading[0] is None or reading[0] == wanted:
            return None
        changed = reading[1]
        # A differing value changed before the put or with no time is a device that never took our value, which is what a resend is for.
        return reading[0] if isinstance(changed, datetime) and changed > put_at else None

    def spawn_task(self, zone_id: str, coro: Coroutine[Any, Any, None], *, hass: HomeAssistant) -> asyncio.Future[Any]:
        """Start `coro` as a task of the zone that `cancel_resends` cancels."""
        task: asyncio.Future[Any] = hass.async_create_task(coro)
        tasks = self._tasks.setdefault(zone_id, set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def hold_resend(
        self, zone_id: str, send: Callable[[], Coroutine[Any, Any, None]], *, entry: ResendEntry, retry_at: datetime | None = None,
    ) -> None:
        """Keep a refused resend and judge it once, at the first data publish after CloudHealth stops holding or at `retry_at`; a later refusal replaces it."""
        self._drop_held(zone_id)
        entity = entry.entity
        held = _HeldResend(send=send, entry=entry)
        if retry_at is not None:

            @callback
            def _on_time(_now: datetime) -> None:
                if self._held.get(zone_id) is not held:
                    return
                # The poll paused for the quota left the data old: judge at the next zone publish, as after a cloud outage.
                held.cancel_timer = None
                self._sync_listener()
                self._arm_publish_wait()

            held.cancel_timer = async_track_point_in_utc_time(entity.hass, _on_time, retry_at)
        self._held[zone_id] = held
        self._sync_listener()

    async def gate_resend(
        self,
        zone_id: str,
        label: str,
        resend: Callable[[bool, ResendEntry], Coroutine[Any, Any, None]],
        *,
        entry: ResendEntry,
        quota_rearmed: bool,
    ) -> bool:
        """Return True when an EXPIRED resend may go out now; else hold it for the cloud or one quota reset, or drop it."""
        entity = entry.entity
        coordinator = entity.coordinator
        name = entity._zone_name
        if coordinator.cloud_health.holds():
            _LOGGER.debug("%s: %s EXPIRED-retry cloud resend held until the cloud recovers", label, name)
            self.hold_resend(zone_id, lambda: resend(quota_rearmed, entry), entry=entry)
            return False
        if not self.owned(zone_id, entry).send:
            return False
        blocked, reason, snapshot = await async_check_reserve_with_snapshot(entity.hass, coordinator=coordinator)
        if not blocked:
            return True
        reset = predicted_next_reset(snapshot.get("last_reset_utc") if isinstance(snapshot, Mapping) else None)
        # The timer and payload live in memory, so a restart drops the resend
        if quota_rearmed or reset is None or reset <= dt_util.utcnow():
            _LOGGER.info("%s: %s EXPIRED-retry cloud resend skipped: %s", label, name, reason)
            return False
        _LOGGER.info("%s: %s EXPIRED-retry cloud resend held until the daily limit resets: %s", label, name, reason)
        self.hold_resend(zone_id, lambda: resend(True, entry), entry=entry, retry_at=reset)
        return False

    def hold_refused_resend(
        self, zone_id: str, label: str, resend: Callable[[bool, ResendEntry], Coroutine[Any, Any, None]], *, entry: ResendEntry, quota_rearmed: bool,
    ) -> None:
        """Hold a resend whose PUT Tado refused (rate limit, blocked) for the next cloud recovery, up to MAX_RESEND_ATTEMPTS PUTs in all."""
        if entry.attempts >= MAX_RESEND_ATTEMPTS:
            _LOGGER.info("%s: %s resend dropped, Tado refused it %s times", label, entry.name, entry.attempts)
            return
        # The refused attempt took a new write sequence of its own; the zone's other marks are the put's.
        retry = replace(entry, attempts=entry.attempts + 1, seq=entry.entity._last_write_sequence)
        _LOGGER.debug("%s: %s resend refused by Tado, held until the cloud recovers", label, entry.name)
        self.hold_resend(zone_id, lambda: resend(quota_rearmed, retry), entry=retry)

    def cancel_resend_tasks(self, zone_id: str) -> None:
        """Cancel the zone's running resend tasks."""
        for task in tuple(self._tasks.get(zone_id, ())):
            task.cancel()

    def cancel_resends(self, zone_id: str) -> None:
        """Drop the zone's held resend and cancel its running resend tasks."""
        self._drop_held(zone_id)
        self.cancel_resend_tasks(zone_id)
        self._sync_listener()

    def shutdown(self) -> None:
        """Cancel every held resend and resend task and release the CloudHealth listener."""
        for zone_id in (*self._held, *self._tasks):
            self.cancel_resends(zone_id)
        self._tasks.clear()
        # The debouncer was cancelled just before, so no announced write will start.
        for record in self._records.values():
            record.queued = False
            self._cancel_waiters(record)
        self._closed = True
        self._cancel_probe()
        self._sync_listener()

    def rebind_health(self) -> None:
        """Move the listener to the coordinator's current CloudHealth after a client swap."""
        if self._health_unsub is None:
            return
        self._health_unsub()
        self._health = self._coordinator.cloud_health
        self._health_unsub = self._health.add_listener(self.on_cloud_change)

    @callback
    def on_cloud_change(self, _change: Any) -> None:
        """Wait for the next data publish once the cloud is back; the listener fires before the poll that made the call has published."""
        health = self._health
        if health is None or self._closed:
            return
        if health.state is CloudHealthState.UNREACHABLE:
            self._start_probe()
        # An owed change is judged only after a REST success (state OK); a held resend as before, once nothing holds.
        owed = health.state is CloudHealthState.OK and self._has_owed()
        resends = not health.holds() and any(held.cancel_timer is None for held in self._held.values())
        if owed:
            self._recovered_at = dt_util.utcnow()
            for record in self._records.values():
                record.owed_waits = 0
                record.owed_retry_at = None
        if self._publish_unsub is not None or not (owed or resends):
            return
        self._arm_publish_wait("owed_write" if owed else "resend_recovery")

    def _arm_publish_wait(self, reason: str = "resend_recovery", *, ask: bool = True) -> None:
        """Wait, once, for a publish with a newer zone fetch, and ask for that fetch: a HomeKit setup would not make one for up to the cloud-sync dial."""
        if self._publish_unsub is not None:
            return
        coordinator = self._coordinator
        self._publish_epoch = coordinator.last_zone_fetch_ts()
        self._publish_unsub = coordinator.async_add_listener(self._on_publish)
        if not ask:
            return
        self._publish_timeout = async_call_later(coordinator.hass, _RESEND_PUBLISH_WAIT_SECONDS, self._on_publish_timeout)
        # A refused request still ends at the timeout, which drops the resend and keeps an owed change.
        coordinator.hass.async_create_task(coordinator.async_request_zone_fetch(reason))

    @callback
    def _on_publish(self) -> None:
        """Judge held resends and owed changes at the first publish that carries a newer zone fetch than the one when the cloud returned."""
        fetched = self._coordinator.last_zone_fetch_ts()
        # A failed poll, a bridge update or a poll without zone states publishes the zones it had before, which would read as unchanged.
        if fetched == self._publish_epoch:
            return
        health = self._health
        owed_ready: list[str] = []
        owed_waiting = False
        if health is not None and health.state is CloudHealthState.OK:
            for zone_id, record in self._records.items():
                if record.owed is None:
                    continue
                if self._started_after_recovery(fetched, record.owed):
                    owed_ready.append(zone_id)
                else:
                    owed_waiting = True
        if owed_waiting:
            # The fetch that brought the cloud back started before it; the next one is the one to judge against.
            self._publish_epoch = fetched
        else:
            self._disarm_publish()
        if health is None or health.holds():
            return
        ready = [(zone_id, held) for zone_id, held in self._held.items() if held.cancel_timer is None]
        for zone_id, _ in ready:
            del self._held[zone_id]
        self._sync_listener()
        # Each released resend waits for the one before, so its reserve check sees that PUT's lower remaining count.
        previous: asyncio.Future[Any] | None = None
        for zone_id, held in ready:
            try:
                previous = self._release(zone_id, held, previous) or previous
            except Exception:  # broad on purpose: one zone's failed release must not strand the others
                _LOGGER.warning("Write ledger: zone %s held resend could not be released", zone_id, exc_info=True)
        for zone_id in owed_ready:
            try:
                self._judge_owed(zone_id)
            except Exception:  # broad on purpose: one zone's failed judgement must not strand the others, its entry stays owed for the next publish
                _LOGGER.warning("Write ledger: zone %s owed change could not be judged, it is kept for the next check", zone_id, exc_info=True)

    def _started_after_recovery(self, fetched: datetime | None, entry: WriteIntent) -> bool:
        """Whether the zone fetch began after both the cloud's return and the owed change."""
        recovered = self._recovered_at
        return (
            isinstance(fetched, datetime) and isinstance(recovered, datetime) and isinstance(entry.created_at, datetime)
            and fetched > recovered and fetched > entry.created_at
        )

    @callback
    def _on_publish_timeout(self, _now: datetime) -> None:
        """Drop the held resends when no poll published within the wait, keep owed changes and ask once more, unless the cloud is down again and a later recovery will judge them."""
        self._publish_timeout = None
        self._disarm_publish()
        health = self._health
        if health is None or health.holds():
            return
        if health.state is CloudHealthState.OK and self._has_owed():
            retry_at = dt_util.utcnow() + timedelta(seconds=_OWED_RETRY_SECONDS)
            for zone_id, record in self._records.items():
                if record.owed is not None:
                    record.owed_waits += 1
                    record.owed_retry_at = retry_at
                    _LOGGER.info(
                        "Zone %s: no zone update followed the cloud's return within %s s, the held change is kept and judged at the next one",
                        zone_id, _RESEND_PUBLISH_WAIT_SECONDS,
                    )
            coordinator = self._coordinator
            coordinator.hass.async_create_task(coordinator.async_request_zone_fetch("owed_write"))
        for zone_id, held in [(z, h) for z, h in self._held.items() if h.cancel_timer is None]:
            del self._held[zone_id]
            _LOGGER.info(
                "%s: held resend dropped, Tado sent no zone update within %s s of the wait starting, nothing is written on unknown",
                held.entry.name, _RESEND_PUBLISH_WAIT_SECONDS,
            )
        self._sync_listener()

    def _disarm_publish(self) -> None:
        """Remove the wait for the next data publish and its timeout."""
        if self._publish_unsub is not None:
            self._publish_unsub()
            self._publish_unsub = None
        if self._publish_timeout is not None:
            self._publish_timeout()
            self._publish_timeout = None

    def _drop_held(self, zone_id: str) -> None:
        held = self._held.pop(zone_id, None)
        if held is not None and held.cancel_timer is not None:
            held.cancel_timer()

    def _sync_listener(self) -> None:
        """Keep exactly one CloudHealth listener while a resend or an owed change waits on the cloud, none otherwise."""
        wanted = not self._closed and (any(held.cancel_timer is None for held in self._held.values()) or self._has_owed())
        if not wanted:
            self._disarm_publish()
        if wanted and self._health_unsub is None:
            self._health = self._coordinator.cloud_health
            self._health_unsub = self._health.add_listener(self.on_cloud_change)
        elif not wanted and self._health_unsub is not None:
            self._health_unsub()
            self._health_unsub = None
            self._health = None

    def _release(self, zone_id: str, held: _HeldResend, previous: asyncio.Future[Any] | None = None) -> asyncio.Future[Any] | None:
        """Run a held resend after `previous` has ended, unless the zone was changed by anyone else since the HomeKit put; return its task."""
        if not self.owned(zone_id, held.entry).send:
            return None

        async def _after_previous() -> None:
            if previous is not None:
                # wait, never await: a cancelled or failed previous task must not stop this zone
                await asyncio.wait([previous])
            await held.send()

        return self.spawn_task(zone_id, _after_previous(), hass=held.entry.entity.hass)

    def live(self, zone_id: str) -> WriteIntent | None:
        """The newest live intent of a zone, or None."""
        record = self._records.get(zone_id)
        return record.intents[-1] if record is not None and record.intents else None

    def hold_locally(
        self, zone_id: str, *, kind: IntentKind, target: dict[str, Any], termination: Mapping[str, Any], ambiguous: bool,
    ) -> WriteIntent:
        """Keep a change the bridge took while the cloud was down; a newer one replaces it, and the entity's release leaves it."""
        record = self._records.setdefault(zone_id, _ZoneRecord())
        live = record.intents[-1] if record.intents else None
        held_target = dict(target)
        # The timer length is part of what is owed, so the intent keeps it beside the termination type.
        if termination.get("durationInSeconds") is not None:
            held_target["duration_seconds"] = termination["durationInSeconds"]
        intent = WriteIntent(
            zone_id=zone_id, entity_id=live.entity_id if live is not None else "", seq=live.seq if live is not None else 0,
            source="homekit", target=held_target, created_at=dt_util.utcnow(), kind=kind, state=IntentState.HELD_LOCAL,
            termination=termination.get("type"), ambiguous=ambiguous,
        )
        # The put just stamped the zone, so a later stamp is another writer's.
        intent.owner = OwnerStamp(write_count=self._write_count(zone_id))
        record.owed = intent
        record.owed_attempts = 0
        record.owed_last_try = None
        record.owed_waits = 0
        record.owed_retry_at = None
        if record.put is not None and record.put.entity_id == intent.entity_id:
            record.put.held = True
        self._arm_owed_recovery()
        self._save_owed()
        return intent

    def owed(self, zone_id: str) -> WriteIntent | None:
        """The change held for the zone while the cloud was down, or None."""
        record = self._records.get(zone_id)
        return record.owed if record is not None else None

    def owes_change(self, zone_id: str) -> bool:
        """True while a change held for the zone is still owed."""
        return self.owed(zone_id) is not None

    def put_is_held(self, zone_id: str, entity_id: str) -> bool:
        """Whether the entity's current HomeKit put is a held one (sent while the cloud was down, nothing is resent for it)."""
        put = self._own_put(zone_id, entity_id)
        return put is not None and put.held

    def owes_power_on(self, zone_id: str) -> bool:
        """Whether a held write of ours switched the zone on and no zone fetch newer than it has landed, so a cloud OFF is still stale."""
        owed = self.owed(zone_id)
        if owed is None or not self.owes_on(zone_id):
            return False
        return not fetch_covers_write(self._coordinator.last_zone_fetch_ts(), owed.created_at, 0.0)

    def owed_target(self, zone_id: str) -> float | None:
        """The target temperature of the zone's owed change, or None when none is owed or it switches the zone off."""
        owed = self.owed(zone_id)
        value = owed.target.get("temperature") if owed is not None else None
        return float(value) if isinstance(value, int | float) else None

    def _has_owed(self) -> bool:
        return any(record.owed is not None for record in self._records.values())

    def supersede(self, zone_id: str) -> None:
        """A newer write to the zone ends the debt of its owed change at the attempt, whether or not the newer write lands."""
        record = self._records.get(zone_id)
        if record is not None and record.owed is not None:
            self._end_owed(zone_id, record.owed, IntentState.DROPPED, "a newer change was made to the zone", clear_off_mark=False)

    def _end_owed(self, zone_id: str, entry: WriteIntent, state: IntentState, why: str, *, clear_off_mark: bool) -> None:
        """End the zone's owed change with one INFO line; the card attribute follows the entry, and an OFF the entry set goes with it."""
        record = self._records.get(zone_id)
        if record is None or record.owed is not entry:
            return
        record.owed = None
        record.owed_attempts = 0
        record.owed_last_try = None
        record.owed_waits = 0
        record.owed_retry_at = None
        entry.state = state
        _LOGGER.info("Zone %s: the change held over HomeKit while Tado was unreachable is %s: %s", zone_id, state.value, why)
        if clear_off_mark and entry.kind is IntentKind.POWER_OFF:
            self.note_own_power(zone_id, powered_off=False)
        self._after_owed_change()

    def _after_owed_change(self) -> None:
        """Drop the listener and the probe once nothing is owed, and keep the store in step."""
        self._sync_listener()
        if not self._has_owed():
            self._cancel_probe()
        self._save_owed()

    def sweep(self, now: datetime) -> None:
        """Run the sweep without ever failing the poll that calls it; the first fault of an episode is a WARNING, the rest DEBUG."""
        try:
            self._sweep(now)
        except Exception:  # broad on purpose: a ledger fault must not fail every poll, and the next sweep tries again  # broad on purpose: a ledger fault must not fail every poll, and the next sweep tries again
            level = logging.DEBUG if self._sweep_fault_open else logging.WARNING
            self._sweep_fault_open = True
            _LOGGER.log(level, "Write ledger: the write ledger sweep failed", exc_info=True)
        else:
            self._sweep_fault_open = False

    def _sweep(self, now: datetime) -> None:
        """Drop owed changes older than a day, then arm the judgement of those whose cloud is back; runs at the top of every poll."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        for zone_id, record in list(self._records.items()):
            entry = record.owed
            if entry is None:
                continue
            created = entry.created_at
            if not isinstance(created, datetime) or created.tzinfo is None:
                self._end_owed(zone_id, entry, IntentState.DROPPED, "its time is unknown, and nothing is written on unknown", clear_off_mark=True)
            elif now - created > OWED_MAX_AGE:
                self._end_owed(zone_id, entry, IntentState.DROPPED, "it is older than 24 hours", clear_off_mark=True)
        self._arm_due_owed(now)

    def _arm_due_owed(self, now: datetime) -> None:
        """Arm the one publish wait when the cloud is back and an owed change is due, which covers a window that ended with no REST success event."""
        if self._closed or self._publish_unsub is not None or self._coordinator.cloud_health.state is not CloudHealthState.OK:
            return
        due = [r for r in self._records.values() if r.owed is not None and (r.owed_retry_at is None or r.owed_retry_at <= now)]
        if not due:
            return
        self._sync_listener()
        self._recovered_at = now
        # After three timeouts the wait asks for nothing and has no timeout: the next zone fetch that starts after now judges the change at no cost.
        self._arm_publish_wait("owed_write", ask=any(r.owed_waits < _OWED_MAX_WAITS for r in due))

    def owned_held(self, zone_id: str, entry: WriteIntent) -> HeldVerdict:
        """The ownership table for a change held over HomeKit: F a foreign write stamp, D the device now, C the zone as the first fetch after the recovery shows it."""
        power, target = self._entry_power(entry), entry.target.get("temperature")
        owner = entry.owner
        if power not in ("ON", "OFF") or (power == "ON" and not isinstance(target, int | float)) or owner is None or owner.write_count is None:
            return self._drop("the change is incomplete, and nothing is written on unknown")
        if self._write_count(zone_id) != owner.write_count:
            return self._drop("another change reached the zone after it")
        cloud = self._cloud_view(zone_id)
        if cloud is None:
            return self._drop("Tado's zone data is not available, and nothing is written on unknown")
        d_power, d_target = self._device_view(zone_id)
        shown = f"Tado shows power {cloud.power}, target {cloud.celsius}, overlay {cloud.termination if cloud.overlay else 'none'}"
        if d_power is not None and d_power != power:
            return self._drop(f"the HomeKit device reads {d_power}, so it never took the change or was changed after it ({shown})")
        if power == "ON" and d_target is not None and abs(d_target - float(target or 0)) > _TEMPERATURE_TOLERANCE:
            return self._drop(f"the HomeKit device holds {d_target}, so it never took the target or was changed after it ({shown})")
        device_holds = d_power == power and (power == "OFF" or d_target is not None)
        if not cloud.overlay:
            if device_holds:
                return HeldVerdict(HeldAction.APPLY, f"Tado shows the schedule and never received the change ({shown})")
            return self._drop(f"the HomeKit device cannot be read, and nothing is written on unknown ({shown})")
        same = cloud.power == power and (power == "OFF" or (cloud.celsius is not None and abs(cloud.celsius - float(target or 0)) <= _TEMPERATURE_TOLERANCE))
        if not same:
            if power == "ON" and cloud.power == "OFF" and d_power == "ON":
                # The bridge may not have uploaded its local ON yet, so a cloud OFF beside a device that holds the ON is not an app change.
                return HeldVerdict(HeldAction.APPLY, f"Tado has not received the switch-on yet ({shown})")
            return self._drop(f"the zone was changed in Tado after it ({shown})")
        if cloud.termination == "MANUAL":
            return HeldVerdict(HeldAction.APPLY, f"Tado shows it as a manual setting ({shown})")
        if cloud.termination is not None and cloud.termination == entry.termination:
            return HeldVerdict(HeldAction.CONFIRMED, f"Tado already shows it ({shown})")
        return self._drop(f"the zone's ending in Tado is not the one asked for ({shown})")

    @staticmethod
    def _entry_power(entry: WriteIntent) -> Any:
        """The power an owed change asks for."""
        return entry.target.get("power")

    @staticmethod
    def _drop(reason: str) -> HeldVerdict:
        return HeldVerdict(HeldAction.DROP, reason)

    def _cloud_view(self, zone_id: str) -> _CloudView | None:
        """The zone's power, target and overlay from the published zone data; None when it cannot be read."""
        power, celsius, _ = self._polled_now(zone_id)
        if power not in ("ON", "OFF"):
            return None
        state = get_zone_state(getattr(self._coordinator, "data", None), zone_id)
        overlay = state.get("overlay") if isinstance(state, dict) else None
        termination = overlay.get("termination") if isinstance(overlay, dict) else None
        ending = termination.get("type") if isinstance(termination, dict) else None
        return _CloudView(
            power=power, celsius=float(celsius) if isinstance(celsius, int | float) else None,
            overlay=isinstance(overlay, dict), termination=ending if isinstance(ending, str) else None,
        )

    def _device_view(self, zone_id: str) -> tuple[str | None, float | None]:
        """The HomeKit device's power and target now; None for what cannot be read."""
        provider = getattr(self._coordinator, "homekit_provider", None)
        if provider is None or not provider.is_connected:
            return None, None
        power = provider.homekit_power(zone_id)[0]
        target = provider.get_target_temperature(zone_id)[0]
        return (power if power in ("ON", "OFF") else None), (float(target) if isinstance(target, int | float) else None)

    def _judge_owed(self, zone_id: str) -> None:
        """Judge the zone's owed change against the table and end it, or send what it owes; never raises."""
        record = self._records.get(zone_id)
        entry = record.owed if record is not None else None
        if record is None or entry is None:
            return
        # A wait another recovery started must not judge a change again inside the spacing between tries.
        if record.owed_retry_at is not None and dt_util.utcnow() < record.owed_retry_at:
            return
        verdict = self.owned_held(zone_id, entry)
        if verdict.action is HeldAction.DROP:
            self._end_owed(zone_id, entry, IntentState.DROPPED, verdict.reason, clear_off_mark=True)
            return
        if verdict.action is HeldAction.CONFIRMED:
            self._end_owed(zone_id, entry, IntentState.CONFIRMED, verdict.reason, clear_off_mark=True)
            return
        now = dt_util.utcnow()
        ending = entry.termination
        resume = False
        if ending == "TADO_MODE":
            after = next_schedule_change(zone_id, getattr(self._coordinator, "data_loader", None), entry.created_at)
            resume = after is not None and after <= now
        elif ending == "MANUAL":
            self._end_owed(zone_id, entry, IntentState.CONFIRMED, f"it is a manual setting, so there is nothing to apply ({verdict.reason})", clear_off_mark=True)
            return
        else:
            # A timed change cannot be held (the writers refuse it), so one found here is unexpected.
            self._end_owed(zone_id, entry, IntentState.DROPPED, "its ending is not one this release applies, and nothing is written on unknown", clear_off_mark=True)
            return
        # Not judged again before the try has finished or ten minutes have passed.
        record.owed_retry_at = now + timedelta(seconds=_OWED_RETRY_SECONDS)
        _LOGGER.info("Zone %s: %s, so the held change is %s", zone_id, verdict.reason, "ended by resuming the schedule" if resume else "sent to Tado")
        self.spawn_task(zone_id, self._reapply(zone_id, entry, resume=resume), hass=self._coordinator.hass)

    async def _reapply(self, zone_id: str, entry: WriteIntent, *, resume: bool) -> None:
        """Send the owed change, or delete the overlay to resume the schedule, inside the zone's send slot and behind the reserve gate."""
        coordinator = self._coordinator
        async with self.zone_order(zone_id):
            record = self._records.get(zone_id)
            if record is None or record.owed is not entry:
                return
            if coordinator.cloud_health.state is not CloudHealthState.OK:
                # The next return of the cloud judges it again.
                record.owed_retry_at = None
                _LOGGER.info("Zone %s: the cloud is unreachable again, the held change waits for its return", zone_id)
                return
            # The one place the reserve lock is taken: the next zone's check must see the calls this zone's PUT used (the lock is never held while taking another zone's order).
            async with self._reserve_lock:
                # No user asked for this write now, so it meets the automatic-writer predicate as well as the last-calls reserve.
                held = coordinator.is_quota_spend_held()
                blocked, reason, snapshot = (True, "the quota is held", None) if held else await async_check_reserve_with_snapshot(
                    coordinator.hass, coordinator=coordinator,
                )
                if record.owed is not entry:
                    return
                if blocked:
                    reset = predicted_next_reset(snapshot.get("last_reset_utc") if isinstance(snapshot, Mapping) else None)
                    now = dt_util.utcnow()
                    record.owed_retry_at = reset if reset is not None and reset > now else now + timedelta(seconds=_OWED_PROBE_STEPS[-1])
                    _LOGGER.info("Zone %s: the held change waits for the daily limit: %s", zone_id, reason)
                    return
                last = record.owed_last_try
                if last is not None and (dt_util.utcnow() - last).total_seconds() < _OWED_RETRY_SECONDS:
                    record.owed_retry_at = last + timedelta(seconds=_OWED_RETRY_SECONDS)
                    return
                record.owed_last_try = dt_util.utcnow()
                record.owed_attempts += 1
                attempt = record.owed_attempts
                sent = False
                try:
                    async with asyncio.timeout(10):
                        sent = await self._send_owed(zone_id, entry, resume=resume)
                except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as exc:
                    # A refusal says nothing about the change: the try is given back and the entry kept until the cloud has recovered.
                    record.owed_attempts -= 1
                    record.owed_retry_at = dt_util.utcnow() + timedelta(seconds=_OWED_RETRY_SECONDS)
                    handle_background_write_error(
                        exc, coordinator.config_entry, coordinator.hass, coordinator,
                        f"Zone {zone_id}: Tado refused the held change ({type(exc).__name__}), it is kept and sent again after recovery",
                    )
                    return
                except Exception:  # broad on purpose: a failed try is counted and retried, and must not end the task unlogged
                    _LOGGER.warning("Zone %s: sending the held change to Tado failed", zone_id, exc_info=True)
            if not sent:
                if attempt >= MAX_OWED_ATTEMPTS:
                    self._end_owed(zone_id, entry, IntentState.DROPPED, f"Tado did not take it in {attempt} tries", clear_off_mark=True)
                else:
                    record.owed_retry_at = dt_util.utcnow() + timedelta(seconds=_OWED_RETRY_SECONDS)
                    _LOGGER.info("Zone %s: Tado did not take the held change (try %s of %s), it is kept", zone_id, attempt, MAX_OWED_ATTEMPTS)
                return
            # The write set the zone's power, so the mark follows it as a cloud write's does.
            self.note_own_power(zone_id, powered_off=self._entry_power(entry) == "OFF" and not resume)
            self._end_owed(zone_id, entry, IntentState.CONFIRMED, "applied" if not resume else "the schedule was resumed", clear_off_mark=False)
        await coordinator.async_request_zone_fetch("owed_write")

    async def _send_owed(self, zone_id: str, entry: WriteIntent, *, resume: bool) -> bool:
        client = self._coordinator.api_client
        if resume:
            sent = bool(await client.delete_zone_overlay(zone_id))
        else:
            if self._entry_power(entry) == "ON":
                body: dict[str, Any] = {"type": "HEATING", "power": "ON", "temperature": {"celsius": entry.target.get("temperature")}}
            else:
                body = {"type": "HEATING", "power": "OFF"}
            sent = bool(await client.set_zone_overlay(zone_id, body, {"type": entry.termination}))
        reconciler = getattr(self._coordinator, "state_reconciler", None)
        if sent and reconciler is not None:
            reconciler.record_local_write(zone_id)
        return sent

    def _start_probe(self) -> None:
        """Start the zone-fetch probe chain while a change is owed; one chain at a time."""
        if self._probe_cancel is not None or self._closed or not self._has_owed():
            return
        self._probe_step = 0
        self._schedule_probe()

    def _schedule_probe(self) -> None:
        delay = _OWED_PROBE_STEPS[min(self._probe_step, len(_OWED_PROBE_STEPS) - 1)]
        self._probe_cancel = async_call_later(self._coordinator.hass, delay, self._on_probe)

    @callback
    def _on_probe(self, _now: datetime) -> None:
        """Ask for one zone fetch while the cloud is unreachable and a change is owed, then wait for the next step."""
        self._probe_cancel = None
        coordinator = self._coordinator
        if self._closed or not self._has_owed() or coordinator.cloud_health.state is not CloudHealthState.UNREACHABLE:
            return
        self._probe_step += 1
        self._schedule_probe()
        coordinator.hass.async_create_task(coordinator.async_request_zone_fetch("owed_probe"))

    def _cancel_probe(self) -> None:
        if self._probe_cancel is not None:
            self._probe_cancel()
            self._probe_cancel = None

    def intents(self, zone_id: str) -> tuple[WriteIntent, ...]:
        """Every live intent of a zone, oldest first; arm replaces the list, so a zone holds at most one."""
        record = self._records.get(zone_id)
        return tuple(record.intents) if record is not None else ()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """A plain copy of every zone record for diagnostics and tests."""
        return {
            zone_id: {
                "intents": [
                    {
                        "entity_id": i.entity_id, "seq": i.seq, "source": i.source, "kind": i.kind,
                        "state": i.state, "target": dict(i.target),
                    }
                    for i in record.intents
                ],
                "put": None if record.put is None else {
                    "entity_id": record.put.entity_id, "at": record.put.at,
                    "stamp": record.put.stamp, "source": record.put.source,
                    "payload": record.put.payload, "target_temp": record.put.target_temp,
                },
            }
            for zone_id, record in self._records.items()
        }

    def _own_put(self, zone_id: str, entity_id: str) -> _PutRecord | None:
        record = self._records.get(zone_id)
        put = record.put if record is not None else None
        return put if put is not None and put.entity_id == entity_id else None

    def _put_for(self, zone_id: str, entity_id: str) -> _PutRecord:
        record = self._records.setdefault(zone_id, _ZoneRecord())
        if record.put is None or record.put.entity_id != entity_id:
            record.put = _PutRecord(entity_id=entity_id)
        return record.put

    def _own_live(self, zone_id: str, entity_id: str) -> WriteIntent | None:
        intent = self.live(zone_id)
        return intent if intent is not None and intent.entity_id == entity_id else None

    def _arm_owed_recovery(self) -> None:
        """Listen for the cloud's return and, while it is unreachable, probe it."""
        self._sync_listener()
        if self._coordinator.cloud_health.state is CloudHealthState.UNREACHABLE:
            self._start_probe()

    def _save_owed(self) -> None:
        """Queue the delayed save of the owed entries; the loader is gone once the entry is torn down."""
        loader = getattr(self._coordinator, "data_loader", None)
        if loader is not None:
            loader.save_write_ledger(self.to_store())

    def to_store(self) -> dict[str, Any]:
        """The complete HELD_LOCAL owed entries by zone, in the shape the store keeps; attempts, waits and timers are not kept."""
        entries: dict[str, Any] = {}
        for zone_id, record in self._records.items():
            entry = record.owed
            if entry is None or not self._is_storable(entry):
                continue
            target: dict[str, Any] = {"power": entry.target["power"]}
            for key in ("temperature", "duration_seconds"):
                if isinstance(entry.target.get(key), int | float):
                    target[key] = entry.target[key]
            entries[zone_id] = {
                "created_at": entry.created_at.isoformat(), "kind": str(entry.kind), "target": target,
                "termination": entry.termination, "ambiguous": entry.ambiguous,
            }
        return {"entries": entries}

    @staticmethod
    def _is_storable(entry: WriteIntent) -> bool:
        """Whether the entry has everything the judgement needs, so a stored one is never dropped as incomplete."""
        power, temperature = entry.target.get("power"), entry.target.get("temperature")
        return (
            entry.state is IntentState.HELD_LOCAL and isinstance(entry.kind, IntentKind)
            and isinstance(entry.created_at, datetime) and entry.created_at.tzinfo is not None
            and power in ("ON", "OFF") and (power == "OFF" or isinstance(temperature, int | float))
            and isinstance(entry.termination, str) and bool(entry.termination)
        )

    def from_store(self, data: Any) -> None:
        """Take back the owed entries a store kept; a row that is unreadable or older than a day is skipped, never raised."""
        rows = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            return
        now, restored, dropped = dt_util.utcnow(), 0, 0
        for zone_id, row in rows.items():
            try:
                entry = self._entry_from_row(str(zone_id), row)
            except ValueError as err:
                _LOGGER.warning("Write ledger: a stored held change for zone %s is unreadable and is dropped: %s", zone_id, err)
                dropped += 1
                continue
            if entry.created_at > now:
                # A future time is never aged out or judged, so nothing could end the entry.
                _LOGGER.warning("Write ledger: a stored held change for zone %s is dated in the future and is dropped", zone_id)
                dropped += 1
                continue
            if now - entry.created_at > OWED_MAX_AGE:
                dropped += 1
                continue
            record = self._records.setdefault(str(zone_id), _ZoneRecord())
            record.owed = entry
            if entry.kind is IntentKind.POWER_OFF:
                # Same mark note_own_power sets for a live OFF: our OFF is newer than any fetch that could show it.
                record.own_off = now
            restored += 1
        if restored or dropped:
            _LOGGER.info("Write ledger: %d held change(s) restored after the restart, %d dropped (unreadable or older than 24 hours)", restored, dropped)
        if restored:
            self._arm_owed_recovery()
        if dropped:
            self._save_owed()

    def _entry_from_row(self, zone_id: str, row: Any) -> WriteIntent:
        """Build the owed entry of one stored row, stamped with the zone's write count now; ValueError names what is wrong."""
        if not isinstance(row, dict):
            raise ValueError("the row is not a mapping")  # noqa: TRY004 - one error type for the caller
        created_raw = row.get("created_at")
        if not isinstance(created_raw, str):
            raise ValueError("the time is missing")
        created = datetime.fromisoformat(created_raw)
        if created.tzinfo is None:
            raise ValueError("the time has no zone")
        kind = IntentKind(str(row.get("kind")))
        stored = row.get("target")
        if not isinstance(stored, dict):
            raise ValueError("the target is not a mapping")
        power = stored.get("power")
        if power not in ("ON", "OFF"):
            raise ValueError("the power is not ON or OFF")
        target: dict[str, Any] = {"power": power}
        for key in ("temperature", "duration_seconds"):
            value = stored.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                target[key] = value
        if power == "ON" and "temperature" not in target:
            raise ValueError("the target temperature is missing")
        termination, ambiguous = row.get("termination"), row.get("ambiguous")
        if not isinstance(termination, str) or not termination:
            raise ValueError("the ending is missing")
        if not isinstance(ambiguous, bool):
            raise ValueError("the ambiguous flag is not a boolean")
        # A fresh reconciler counts from zero, so a write that reaches the zone after this load is the one the stamp cannot match.
        return WriteIntent(
            zone_id=zone_id, entity_id="", seq=0, source="homekit", target=target, created_at=created, kind=kind,
            state=IntentState.HELD_LOCAL, termination=termination, ambiguous=ambiguous,
            owner=OwnerStamp(write_count=self._write_count(zone_id)),
        )

    def owes_on(self, zone_id: str) -> bool:
        """Whether a held write of ours that switches the zone on is still owed."""
        owed = self.owed(zone_id)
        return owed is not None and owed.state is IntentState.HELD_LOCAL and owed.kind is not IntentKind.POWER_OFF


def ordered_zone_write(coordinator: object, zone_id: str) -> AbstractAsyncContextManager[bool]:
    """The zone's send slot for one overlay request; no slot for a non-zone call or a coordinator without a ledger."""
    ledger = getattr(coordinator, "write_ledger", None)
    if not zone_id or not isinstance(ledger, ZoneWriteLedger):
        return nullcontext(False)
    return ledger.zone_order(zone_id)
