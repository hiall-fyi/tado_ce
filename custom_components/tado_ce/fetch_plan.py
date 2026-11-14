"""Pure fetch plan: the items one poll will spend API calls on, and what they cost."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, Any

from .const import (
    CAPABILITY_ZONE_TYPES,
    FORCEABLE_FETCH_HOME_STATE,
    FORCEABLE_FETCH_MOBILE,
    FORCEABLE_FETCH_WEATHER,
    MAX_SCHEDULE_FETCH_CALLS,
    OFFSET_DRIFT_REFRESH_SECONDS,
    is_climate_zone,
)

if TYPE_CHECKING:
    from .config_manager import ConfigurationManager

_LOGGER = logging.getLogger(__name__)

ZONE_SKIP_SLACK_SECONDS = 1.0

KIND_ZONE_STATES = "zone_states"
KIND_WEATHER = "weather"
KIND_HOME_STATE = "home_state"
KIND_MOBILE_QUICK = "mobile_quick"
KIND_ZONES_INFO = "zones_info"
KIND_MOBILE_FULL = "mobile_full"
KIND_DEVICES = "devices"
KIND_OFFSETS_FULL = "offsets_full"
KIND_CAPABILITIES = "capabilities"
KIND_HEATING_CIRCUITS = "heating_circuits"
KIND_CIRCUIT_CONTROL = "circuit_control"
KIND_SVC_SCHEDULES = "svc_schedules"
KIND_OFFSETS_DRIFT = "offsets_drift"
KIND_CAPABILITIES_FORCED = "capabilities_forced"
KIND_OFFSETS_FIRST_LOAD = "offsets_first_load"

GROUP_FULL_SYNC = "full_sync"

GAP_FULL_SYNC = "full_sync"
GAP_PARTIAL = "full_partial"

# 2**20 already exceeds every cap, so a runaway fail count cannot overflow the float product.
_MAX_GAP_DOUBLINGS = 20

GATE_MUST = "must"
GATE_HARD_RESERVE = "hard_reserve"
GATE_SOFT_LOW = "soft_low"
GATE_LIVE_SVC = "live_svc"

PHASE_SYNC = "sync"
PHASE_POST = "post"


@dataclass(frozen=True)
class FetchItem:
    """One planned fetch: what, how many calls, which gate admits it and why."""

    kind: str
    calls: int
    gate: str
    reason: str
    phase: str = PHASE_SYNC
    group: str = ""
    forced: bool = False
    zone_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FetchPlan:
    """The priced set of fetches for one poll, with what was skipped or shed."""

    items: tuple[FetchItem, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    shed: tuple[tuple[str, str], ...] = ()
    unpriced: bool = False
    partial: tuple[str, ...] = ()
    homekit_connected: bool = False
    force_zone: bool = False
    forced_types: frozenset[str] = field(default_factory=frozenset)
    snapshot_shape: str | None = None
    probe_only: bool = False

    @property
    def cost(self) -> int:
        """Calls of every item, sheddable ones included."""
        return sum(i.calls for i in self.items)

    @property
    def must_cost(self) -> int:
        """Calls of the sync-phase, ungrouped, must-gate items (the pause decision reads this)."""
        return sum(i.calls for i in self.items if i.phase == PHASE_SYNC and not i.group and i.gate == GATE_MUST)

    @property
    def full_cost(self) -> int:
        """Calls of the grouped (full-sync) items."""
        return sum(i.calls for i in self.items if i.group)

    @property
    def post_cost(self) -> int:
        """Calls of the post-sync items."""
        return sum(i.calls for i in self.items if i.phase == PHASE_POST)

    @property
    def variable_max(self) -> int:
        """Worst-case schedule calls for the eligible SVC zones, which sit outside cost."""
        svc = self.item(KIND_SVC_SCHEDULES)
        return MAX_SCHEDULE_FETCH_CALLS * len(svc.zone_ids) if svc else 0

    def has(self, kind: str) -> bool:
        """True when an item of this kind is planned."""
        return any(i.kind == kind for i in self.items)

    def item(self, kind: str) -> FetchItem | None:
        """The first planned item of this kind, or None."""
        return next((i for i in self.items if i.kind == kind), None)

    def kinds(self, phase: str | None = None) -> list[str]:
        """Kinds of the planned items, optionally of one phase."""
        return [i.kind for i in self.items if phase is None or i.phase == phase]

    def without(self, kinds: Iterable[str], *, reason: str) -> FetchPlan:
        """A copy with the matching items moved to shed under this reason."""
        drop = set(kinds)
        gone = tuple((i.kind, reason) for i in self.items if i.kind in drop)
        keep = tuple(i for i in self.items if i.kind not in drop)
        return replace(self, items=keep, shed=self.shed + gone)


FULL_SYNC_FIXED_CALLS = 2
NEW_ZONE_FULL_SYNC_CALLS = 2


def is_due(now: datetime, due_at: datetime, *, slack: bool = False) -> bool:
    """True when now has reached due_at, with one second of slack when asked."""
    if slack:
        due_at = due_at - timedelta(seconds=ZONE_SKIP_SLACK_SECONDS)
    return now >= due_at


@dataclass(frozen=True)
class PlanConfig:
    """The feature switches and per-type floors the builder reads."""

    weather: bool = False
    home_state: bool = False
    mobile: bool = False
    mobile_frequent: bool = False
    offsets: bool = False
    heating_circuits: bool = False
    weather_floor_min: int = 0
    presence_floor_min: int = 0
    mobile_floor_min: int = 0

    @classmethod
    def from_config_manager(cls, cm: ConfigurationManager) -> PlanConfig:
        """Read the switches and floors from the configuration manager."""
        return cls(
            weather=cm.get_weather_enabled(),
            home_state=cm.get_home_state_sync_enabled(),
            mobile=cm.get_mobile_devices_enabled(),
            mobile_frequent=cm.get_mobile_devices_frequent_sync(),
            offsets=cm.get_offset_enabled(),
            heating_circuits=cm.get_heating_circuit_enabled(),
            weather_floor_min=cm.get_weather_min_refresh_minutes(),
            presence_floor_min=cm.get_presence_min_refresh_minutes(),
            mobile_floor_min=cm.get_mobile_devices_min_refresh_minutes(),
        )


def has_custom_interval(cm: ConfigurationManager) -> bool:
    """True when the user set a day or a night polling interval."""
    return cm.get_custom_day_interval() is not None or cm.get_custom_night_interval() is not None


@dataclass(frozen=True)
class ZoneFact:
    """What the builder needs to know about one zone of the stored zone list."""

    id: str
    type: str
    has_offset_serial: bool = False


@dataclass(frozen=True)
class GapRecord:
    """A retry gap: how often a fetch failed and when it may run again."""

    fails: int
    next_retry_at: datetime


def gap_after_failure(prev: GapRecord | None, *, now: datetime, poll_interval_s: float, cap_s: float) -> GapRecord:
    """The record after one more failure: the retry time is absolute, doubling per failure up to the cap."""
    fails = (prev.fails if prev is not None else 0) + 1
    wait = min(cap_s, poll_interval_s * 2 ** min(fails - 1, _MAX_GAP_DOUBLINGS))
    return GapRecord(fails, now + timedelta(seconds=wait))


def gap_blocks(gap: GapRecord | None, now: datetime) -> bool:
    """True while the retry time has not come, with one second of slack at the expiry."""
    return gap is not None and not is_due(now, gap.next_retry_at, slack=True)


def gap_cap_seconds(
    kind: str, *, floor_s: float, poll_interval_s: float, full_sync_period_s: float, zone_list_known: bool = True,
) -> float:
    """The longest a failed fetch of this kind waits: its normal period, one poll while no zone list exists."""
    if kind == GAP_FULL_SYNC:
        return full_sync_period_s if zone_list_known else poll_interval_s
    if kind in (GAP_PARTIAL, KIND_CAPABILITIES_FORCED):
        return full_sync_period_s
    return max(floor_s, poll_interval_s)


@dataclass(frozen=True)
class PlanInputs:
    """Everything the builder reads, gathered once at the start of a poll."""

    now: datetime
    config: PlanConfig = PlanConfig()
    homekit_connected: bool = False
    user_has_custom_interval: bool = False
    dial_minutes: int = 30
    full_sync_period_s: int = 3600
    last_zone_fetch: datetime | None = None
    last_full_sync: datetime | None = None
    last_weather: datetime | None = None
    last_home_state: datetime | None = None
    last_mobile: datetime | None = None
    last_offset_resync: datetime | None = None
    request_full_sync: bool = False
    force_zone_fetch: bool = False
    forced_types: frozenset[str] = frozenset()
    zones_info: tuple[ZoneFact, ...] | None = None
    cached_capability_ids: frozenset[str] = frozenset()
    unlisted_zone_count: int = 0
    svc_zone_ids: frozenset[str] = frozenset()
    gaps: Mapping[str, GapRecord] = field(default_factory=dict)
    partial: tuple[str, ...] = ()
    caps_changed: frozenset[str] = frozenset()
    first_load_pending: int = 0
    poll_interval_s: float = 1800.0
    health_ok: bool = True


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_stamp(value: object) -> bool:
    return value is None or (isinstance(value, datetime) and value.tzinfo is not None)


def _is_gap(kind: object, gap: object) -> bool:
    if not (isinstance(kind, str) and isinstance(gap, GapRecord) and _is_number(gap.fails)):
        return False
    return isinstance(gap.next_retry_at, datetime) and gap.next_retry_at.tzinfo is not None


def _inputs_are_sound(i: PlanInputs) -> bool:
    """True when every value the builder compares or iterates has the expected type."""
    if not (isinstance(i.now, datetime) and i.now.tzinfo is not None and isinstance(i.config, PlanConfig)):
        return False
    stamps = (i.last_zone_fetch, i.last_full_sync, i.last_weather, i.last_home_state, i.last_mobile, i.last_offset_resync)
    if not all(_is_stamp(s) for s in stamps):
        return False
    pending = i.first_load_pending
    if not (isinstance(pending, int) and not isinstance(pending, bool) and pending >= 0):
        return False
    if not (isinstance(i.caps_changed, frozenset) and all(isinstance(z, str) for z in i.caps_changed)):
        return False
    cfg = i.config
    numbers = (
        i.dial_minutes, i.full_sync_period_s, i.poll_interval_s,
        cfg.weather_floor_min, cfg.presence_floor_min, cfg.mobile_floor_min,
    )
    if not all(_is_number(n) for n in numbers):
        return False
    if not (isinstance(i.partial, tuple) and all(isinstance(p, str) for p in i.partial)):
        return False
    if not (isinstance(i.gaps, Mapping) and all(_is_gap(k, g) for k, g in i.gaps.items())):
        return False
    zones = i.zones_info
    return zones is None or (isinstance(zones, tuple) and all(isinstance(z, ZoneFact) for z in zones))


def _floor_minutes(i: PlanInputs, base_min: int) -> int:
    """A per-type floor in minutes, which HomeKit widens to the dial."""
    return max(base_min, i.dial_minutes) if i.homekit_connected else base_min


def _floor_due(i: PlanInputs, last: datetime | None, base_min: int) -> bool:
    """True when a slow-data fetch is past its floor (strict, no slack)."""
    if last is None:
        return True
    return is_due(i.now, last + timedelta(minutes=_floor_minutes(i, base_min)))


def _zone_states_item(i: PlanInputs) -> tuple[FetchItem | None, tuple[str, str] | None]:
    """The zone-states item, or the reason it is skipped under HomeKit."""
    if i.force_zone_fetch:
        return FetchItem(KIND_ZONE_STATES, 1, GATE_MUST, "force_zone"), None
    last = i.last_zone_fetch
    if i.homekit_connected and last is not None and not i.user_has_custom_interval:
        if not is_due(i.now, last + timedelta(minutes=i.dial_minutes), slack=True):
            return None, (KIND_ZONE_STATES, "homekit_skip")
    return FetchItem(KIND_ZONE_STATES, 1, GATE_MUST, "due"), None


def _quick_extra(
    i: PlanInputs, kind: str, forced_name: str, enabled: bool, last: datetime | None, floor_min: int,
) -> tuple[FetchItem | None, tuple[str, str] | None]:
    """One quick-pass extra: absent when disabled, skipped inside its floor unless forced."""
    if not enabled:
        return None, (kind, "disabled")
    forced = forced_name in i.forced_types
    if _floor_due(i, last, floor_min):
        if not forced and gap_blocks(i.gaps.get(kind), i.now):
            return None, (kind, "retry_gap")
        return FetchItem(kind, 1, GATE_MUST, "due", forced=forced), None
    if forced:
        return FetchItem(kind, 1, GATE_MUST, "forced", forced=True), None
    return None, (kind, "floor")


def _full_sync_reason(i: PlanInputs) -> str | None:
    """Why a full sync is due now, or None when it is not."""
    if i.last_full_sync is None:
        return "first_poll"
    if i.request_full_sync:
        return "explicit_request"
    if (i.now - i.last_full_sync).total_seconds() >= i.full_sync_period_s:
        return "age"
    return None


def full_group_items(
    inputs: PlanInputs, *, mobile_in_full: bool, reason: str,
) -> tuple[tuple[FetchItem, ...], bool]:
    """The full-sync group in fetch order, and whether it is only a lower bound (no zone list yet)."""
    def grouped(kind: str, calls: int, gate: str = GATE_HARD_RESERVE, zones: frozenset[str] = frozenset()) -> FetchItem:
        return FetchItem(kind, calls, gate, reason, group=GROUP_FULL_SYNC, zone_ids=zones)

    items = [grouped(KIND_ZONES_INFO, 1)]
    if mobile_in_full:
        items.append(grouped(KIND_MOBILE_FULL, 1))
    items.append(grouped(KIND_DEVICES, 1))
    zones = inputs.zones_info
    cfg = inputs.config
    if zones is None:
        # Sub-items are listed at 0 calls and priced at run time, so the cost stays a lower bound.
        if cfg.offsets:
            items.append(grouped(KIND_OFFSETS_FULL, 0))
        items.append(grouped(KIND_CAPABILITIES, 0))
        if cfg.heating_circuits:
            items += [grouped(KIND_HEATING_CIRCUITS, 0), grouped(KIND_CIRCUIT_CONTROL, 0)]
        if inputs.svc_zone_ids:
            items.append(grouped(KIND_SVC_SCHEDULES, 0, GATE_LIVE_SVC, inputs.svc_zone_ids))
        return tuple(items), True
    # Counts come from the stored list and may be 0: the item says "run this step" against the list the sync fetches.
    if cfg.offsets:
        items.append(grouped(KIND_OFFSETS_FULL, sum(1 for z in zones if is_climate_zone(z.type) and z.has_offset_serial)))
    uncached = sum(1 for z in zones if z.type in CAPABILITY_ZONE_TYPES and z.id not in inputs.cached_capability_ids)
    items.append(grouped(KIND_CAPABILITIES, uncached + NEW_ZONE_FULL_SYNC_CALLS * inputs.unlisted_zone_count))
    if cfg.heating_circuits:
        items.append(grouped(KIND_HEATING_CIRCUITS, 1))
        items.append(grouped(KIND_CIRCUIT_CONTROL, sum(1 for z in zones if z.type == "HEATING")))
    if inputs.svc_zone_ids:
        # Upper bound: production also limits SVC to HEATING zones without a healthy cached schedule.
        items.append(grouped(KIND_SVC_SCHEDULES, 0, GATE_LIVE_SVC, inputs.svc_zone_ids))
    return tuple(items), False


@dataclass(frozen=True)
class SteadyStateCost:
    """Calls per poll and per full sync once the floors have settled, with no drift and nothing forced."""

    per_poll: int
    per_full_sync: int


_PRICING_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def steady_state_cost(
    config: PlanConfig,
    *,
    homekit_connected: bool,
    user_has_custom_interval: bool,
    zones_info: tuple[ZoneFact, ...] | None = None,
    cached_capability_ids: frozenset[str] = frozenset(),
    unlisted_zone_count: int = 0,
) -> SteadyStateCost:
    """The floor-free per-poll and per-full-sync cost, priced by the same group builder the plan uses."""
    zone_fetch = 0 if homekit_connected and not user_has_custom_interval else 1
    frequent_mobile = config.mobile and config.mobile_frequent
    per_poll = zone_fetch + config.weather + config.home_state + frequent_mobile
    mobile_in_full = config.mobile and not config.mobile_frequent
    zones = zones_info if isinstance(zones_info, tuple) and all(isinstance(z, ZoneFact) for z in zones_info) else None
    if zones is None:
        return SteadyStateCost(per_poll, FULL_SYNC_FIXED_CALLS + mobile_in_full)
    priced = PlanInputs(
        now=_PRICING_EPOCH, config=config, zones_info=zones,
        cached_capability_ids=cached_capability_ids, unlisted_zone_count=unlisted_zone_count,
    )
    group, _ = full_group_items(priced, mobile_in_full=mobile_in_full, reason="steady")
    return SteadyStateCost(per_poll, sum(i.calls for i in group))


def _drift_due(i: PlanInputs) -> bool:
    """True when the offset drift refresh is past its floor, which HomeKit widens to the dial (strict, no slack)."""
    last = i.last_offset_resync
    if last is None:
        return True
    floor_s = OFFSET_DRIFT_REFRESH_SECONDS
    if i.homekit_connected:
        floor_s = max(floor_s, i.dial_minutes * 60)
    return is_due(i.now, last + timedelta(seconds=floor_s))


def post_items(inputs: PlanInputs, *, offsets_in_sync: bool) -> tuple[FetchItem, ...]:
    """The spends that follow the sync: drift refresh, forced capabilities, first-load offset reads."""
    items: list[FetchItem] = []
    zones = inputs.zones_info
    has_climate = zones is not None and any(is_climate_zone(z.type) for z in zones)
    if inputs.config.offsets and has_climate and not offsets_in_sync and _drift_due(inputs):
        items.append(FetchItem(KIND_OFFSETS_DRIFT, 1, GATE_SOFT_LOW, "due", phase=PHASE_POST))
    if inputs.caps_changed and not gap_blocks(inputs.gaps.get(KIND_CAPABILITIES_FORCED), inputs.now):
        items.append(FetchItem(
            KIND_CAPABILITIES_FORCED, len(inputs.caps_changed), GATE_HARD_RESERVE, "changed",
            phase=PHASE_POST, forced=True,
        ))
    if inputs.first_load_pending:
        items.append(FetchItem(
            KIND_OFFSETS_FIRST_LOAD, inputs.first_load_pending, GATE_HARD_RESERVE, "first_load", phase=PHASE_POST,
        ))
    return tuple(items)


def build_post_plan(inputs: PlanInputs, sync_plan: FetchPlan) -> FetchPlan:
    """The post-sync spends, priced with the HomeKit read the sync plan already took."""
    if not _inputs_are_sound(inputs):
        return FetchPlan(homekit_connected=sync_plan.homekit_connected)
    pinned = replace(inputs, homekit_connected=sync_plan.homekit_connected)
    held = bool(inputs.caps_changed) and gap_blocks(inputs.gaps.get(KIND_CAPABILITIES_FORCED), inputs.now)
    return FetchPlan(
        items=post_items(pinned, offsets_in_sync=False),
        skipped=((KIND_CAPABILITIES_FORCED, "retry_gap"),) if held else (),
        homekit_connected=sync_plan.homekit_connected,
    )


def _build(i: PlanInputs) -> FetchPlan:
    items: list[FetchItem] = []
    skipped: list[tuple[str, str]] = []
    zone_item, zone_skip = _zone_states_item(i)
    cfg = i.config
    extras = (
        _quick_extra(i, KIND_WEATHER, FORCEABLE_FETCH_WEATHER, cfg.weather, i.last_weather, cfg.weather_floor_min),
        _quick_extra(i, KIND_HOME_STATE, FORCEABLE_FETCH_HOME_STATE, cfg.home_state, i.last_home_state, cfg.presence_floor_min),
        _quick_extra(
            i, KIND_MOBILE_QUICK, FORCEABLE_FETCH_MOBILE, cfg.mobile and cfg.mobile_frequent,
            i.last_mobile, cfg.mobile_floor_min,
        ),
    )
    for item, skip in ((zone_item, zone_skip), *extras):
        if item is not None:
            items.append(item)
        if skip is not None and not (skip[0] == KIND_MOBILE_QUICK and cfg.mobile and skip[1] != "retry_gap"):
            skipped.append(skip)
    unpriced = False
    reason = _full_sync_reason(i)
    mobile_in_full = cfg.mobile and not any(x.kind == KIND_MOBILE_QUICK for x in items)
    if reason is not None and not i.request_full_sync and gap_blocks(i.gaps.get(GAP_FULL_SYNC), i.now):
        skipped.append((GAP_FULL_SYNC, "retry_gap"))
    elif reason is not None:
        group, unpriced = full_group_items(i, mobile_in_full=mobile_in_full, reason=reason)
        items.extend(group)
    elif i.partial:
        if gap_blocks(i.gaps.get(GAP_PARTIAL), i.now):
            skipped.append((GAP_PARTIAL, "retry_gap"))
        else:
            group, _ = full_group_items(i, mobile_in_full=mobile_in_full, reason="partial_retry")
            items.extend(x for x in group if x.kind in i.partial)
    items.extend(post_items(i, offsets_in_sync=any(x.kind == KIND_OFFSETS_FULL for x in items)))
    return FetchPlan(
        items=tuple(items), skipped=tuple(skipped), unpriced=unpriced, homekit_connected=i.homekit_connected,
        force_zone=i.force_zone_fetch, forced_types=i.forced_types,
    )


SKIP_DEFERRED_FOR_PROBE = "deferred_for_probe"
SKIP_DEFERRED_FOR_BACKOFF = "deferred_for_backoff"

_PROBE_ORDER = (KIND_ZONE_STATES, KIND_WEATHER, KIND_HOME_STATE, KIND_MOBILE_QUICK, KIND_ZONES_INFO)


def probe_plan(plan: FetchPlan) -> FetchPlan:
    """Keep only the first sync item in probe order and defer every other item."""
    if not plan.items:
        return plan
    first = next((i for kind in _PROBE_ORDER for i in plan.items if i.kind == kind and i.phase == PHASE_SYNC), None)
    kept = (first,) if first is not None else ()
    deferred = tuple((i.kind, SKIP_DEFERRED_FOR_PROBE) for i in plan.items if i is not first)
    return replace(plan, items=kept, skipped=plan.skipped + deferred, probe_only=True)


def build_plan(inputs: PlanInputs) -> FetchPlan:
    """The fetches one poll will spend calls on; malformed inputs give the zone-states-only plan."""
    if not _inputs_are_sound(inputs):
        _LOGGER.debug("FetchPlan: bad input, conservative plan")
        return FetchPlan(
            items=(FetchItem(KIND_ZONE_STATES, 1, GATE_MUST, "conservative"),),
            force_zone=inputs.force_zone_fetch, forced_types=inputs.forced_types,
        )
    plan = _build(inputs)
    return plan if inputs.health_ok else probe_plan(plan)


SHED_DRIFT_GATE = "drift_gate"
SHED_OVER_RESERVE = "over_reserve"
SHED_FULL_SYNC_DEFERRED = "full_sync_deferred"

SHAPE_NONE = "none"
SHAPE_EMPTY = "empty"
SHAPE_NO_RESET = "no_reset"
SHAPE_NO_REMAINING = "no_remaining"


def snapshot_shape(snapshot: Mapping[str, Any] | None) -> str | None:
    """Name the way a rate-limit snapshot is absent or unusable, or None when it can drive a decision."""
    if not isinstance(snapshot, Mapping):
        return SHAPE_NONE
    if not snapshot:
        return SHAPE_EMPTY
    if not snapshot.get("last_reset_utc"):
        return SHAPE_NO_RESET
    remaining = snapshot.get("remaining")
    if not isinstance(remaining, int) or isinstance(remaining, bool):
        return SHAPE_NO_REMAINING
    return None


def shed_plan(
    plan: FetchPlan,
    *,
    snapshot: Mapping[str, Any] | None,
    would_pause: Callable[[int], bool],
    drift_allowed: bool,
) -> FetchPlan:
    """Drop optional spends, drift first and the full-sync group last and whole, until the plan fits the quota."""
    shape = snapshot_shape(snapshot)
    if shape is not None:
        return replace(plan, snapshot_shape=shape)
    if not drift_allowed:
        plan = plan.without((KIND_OFFSETS_DRIFT,), reason=SHED_DRIFT_GATE)
    if would_pause(plan.cost):
        plan = plan.without((KIND_OFFSETS_DRIFT,), reason=SHED_OVER_RESERVE)
    if would_pause(plan.cost):
        plan = plan.without((KIND_OFFSETS_FIRST_LOAD, KIND_CAPABILITIES_FORCED), reason=SHED_OVER_RESERVE)
    if would_pause(plan.cost) and not plan.unpriced:
        group = (i.kind for i in plan.items if i.group)
        plan = plan.without(group, reason=SHED_FULL_SYNC_DEFERRED)
    return plan


def gate_post_plan(
    post_plan: FetchPlan,
    *,
    snapshot: Mapping[str, Any] | None,
    would_pause: Callable[[int], bool],
    drift_allowed: bool,
) -> FetchPlan:
    """Re-gate the post-sync items against the snapshot as the sync left it."""
    shape = snapshot_shape(snapshot)
    plan = post_plan
    # WHY: the soft-low gate reads only remaining and limit, so it holds even when the reset time is unknown.
    if not drift_allowed:
        plan = plan.without((KIND_OFFSETS_DRIFT,), reason=SHED_DRIFT_GATE)
    if shape is not None:
        return replace(plan, snapshot_shape=shape)
    over = [i.kind for i in plan.items if i.kind != KIND_OFFSETS_DRIFT and would_pause(i.calls)]
    return plan.without(over, reason=SHED_OVER_RESERVE)


@dataclass(frozen=True)
class ItemResult:
    """What one executed item came back with: returned is False for a failure or a missing reply, True even for [] or {}."""

    kind: str
    returned: bool
    calls: int = 0


@dataclass(frozen=True)
class FetchTally:
    """Calls spent and items obtained by a repeated fetch such as the drift refresh."""

    calls: int
    obtained: int
    obtained_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StampUpdates:
    """The fetch-time stamps, forced types satisfied and retry-gap outcomes one poll's results decided."""

    weather: datetime | None = None
    home_state: datetime | None = None
    mobile: datetime | None = None
    full_sync: datetime | None = None
    offset_resync: datetime | None = None
    clear_request_full_sync: bool = False
    satisfied_forced: frozenset[str] = frozenset()
    partial: tuple[str, ...] = ()
    gap_ok: frozenset[str] = frozenset()
    gap_fail: Mapping[str, float] = field(default_factory=dict)


# kind -> (forced type name, StampUpdates field, PlanConfig floor attribute)
_QUICK_STAMPS = {
    KIND_WEATHER: (FORCEABLE_FETCH_WEATHER, "weather", "weather_floor_min"),
    KIND_HOME_STATE: (FORCEABLE_FETCH_HOME_STATE, "home_state", "presence_floor_min"),
    KIND_MOBILE_QUICK: (FORCEABLE_FETCH_MOBILE, "mobile", "mobile_floor_min"),
}


def _returned(results: Mapping[str, ItemResult], kind: str) -> bool:
    """True when the item has a result that says it returned data; a missing result is a failure."""
    result = results.get(kind)
    return isinstance(result, ItemResult) and result.returned is True


def apply_results(plan: FetchPlan, results: Mapping[str, ItemResult], inputs: PlanInputs) -> StampUpdates:
    """Decide stamps and retry gaps from what each planned fetch returned; bad input stamps nothing."""
    if not (isinstance(plan, FetchPlan) and isinstance(results, Mapping) and _inputs_are_sound(inputs)):
        return StampUpdates()
    now = inputs.now
    stamps: dict[str, Any] = {}
    satisfied: set[str] = set()
    gap_ok: set[str] = set()
    gap_fail: dict[str, float] = {}

    def cap(kind: str, floor_s: float = 0.0) -> float:
        return gap_cap_seconds(
            kind, floor_s=floor_s, poll_interval_s=inputs.poll_interval_s,
            full_sync_period_s=inputs.full_sync_period_s, zone_list_known=inputs.zones_info is not None,
        )

    for item in plan.items:
        quick = _QUICK_STAMPS.get(item.kind) if not item.group else None
        if quick is None:
            continue
        forced_name, stamp_field, floor_attr = quick
        if _returned(results, item.kind):
            stamps[stamp_field] = now
            satisfied.add(forced_name)
            gap_ok.add(item.kind)
        else:
            gap_fail[item.kind] = cap(item.kind, _floor_minutes(inputs, getattr(inputs.config, floor_attr)) * 60)

    group = [i for i in plan.items if i.group == GROUP_FULL_SYNC]
    partial: tuple[str, ...] = ()
    full: dict[str, Any] = {}
    if group:
        partial = tuple(i.kind for i in group if i.kind != KIND_ZONES_INFO and not _returned(results, i.kind))
        if partial:
            gap_fail[GAP_PARTIAL] = cap(GAP_PARTIAL)
        else:
            gap_ok.add(GAP_PARTIAL)
        if not all(i.reason == "partial_retry" for i in group):
            if _returned(results, KIND_ZONES_INFO):
                full = {"full_sync": now, "clear_request_full_sync": True}
                gap_ok.add(GAP_FULL_SYNC)
                if any(i.kind == KIND_OFFSETS_FULL for i in group) and _returned(results, KIND_OFFSETS_FULL):
                    full["offset_resync"] = now
            else:
                gap_fail[GAP_FULL_SYNC] = cap(GAP_FULL_SYNC)
    return StampUpdates(
        **stamps, **full, satisfied_forced=frozenset(satisfied), partial=partial,
        gap_ok=frozenset(gap_ok), gap_fail=gap_fail,
    )


class PollOutcome(StrEnum):
    """How a poll ended, as far as the pending fetch flags are concerned."""

    SUCCESS = "success"
    PAUSE = "pause"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    BLOCKED = "blocked"
    SYNC_ERROR = "sync_error"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class Requeue:
    """The pending flags to put back for the next poll."""

    force_zone: bool
    forced_types: frozenset[str]


def requeue_flags(
    plan: FetchPlan,
    outcome: PollOutcome,
    *,
    satisfied: frozenset[str] = frozenset(),
    enabled_types: frozenset[str] = frozenset(),
) -> Requeue:
    """The flags to requeue: all of them after a failed exit, only the unsatisfied enabled types after success."""
    if outcome == PollOutcome.SUCCESS:
        return Requeue(False, (plan.forced_types - satisfied) & enabled_types)
    return Requeue(plan.force_zone, plan.forced_types)
