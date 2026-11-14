"""QuotaLevels: the daily limit, its tier numbers and the quota hold predicates in one pure value."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from .const import (
    ASSUMED_DAILY_LIMIT_UNKNOWN_TIER,
    LOW_QUOTA_RESERVE_FLOOR,
    LOW_QUOTA_RESERVE_PERCENT,
    MAX_SCHEDULE_FETCH_CALLS,
    QUOTA_BOOTSTRAP_CALLS,
    QUOTA_RESERVE_CALLS,
    QUOTA_RESERVE_PERCENT,
    SCHEDULE_FILL_MAX_QUOTA_FRACTION,
    ZONES_INFO_FREE_TIER_THRESHOLD,
    ZONES_INFO_REFRESH_SECONDS_FREE,
    ZONES_INFO_REFRESH_SECONDS_PAID,
)
from .helpers import predicted_next_reset

if TYPE_CHECKING:
    from .cloud_health import CloudHealth
    from .config_manager import ConfigurationManager

INTERVAL_RESERVE_CALLS = 5


def cloud_health_view(
    health: CloudHealth, snapshot: Mapping[str, Any] | None, config_manager: ConfigurationManager,
) -> dict[str, Any]:
    """The CloudHealth diagnostics view with the quota level of `snapshot`."""
    return health.snapshot(quota_level=QuotaLevels.build(snapshot, config_manager).quota_level(snapshot))


def _remaining_of(snapshot: Mapping[str, Any] | None) -> int | None:
    """Return the snapshot's `remaining` when it is a real int, else None."""
    if not isinstance(snapshot, Mapping):
        return None
    value = snapshot.get("remaining")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


@dataclass(frozen=True)
class QuotaLevels:
    """Tier numbers and hold predicates for one daily limit (None means unknown)."""

    limit: int | None
    reserve_enabled: bool = True

    @staticmethod
    def normalise_limit(value: object) -> int | None:
        """Return `value` when it is a positive int (not a bool), else None."""
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    @classmethod
    def build(
        cls, snapshot: Mapping[str, Any] | None, config_manager: ConfigurationManager,
    ) -> QuotaLevels:
        """Build from a rate-limit snapshot and the live master switch."""
        raw = snapshot.get("limit") if isinstance(snapshot, Mapping) else None
        return cls(cls.normalise_limit(raw), bool(config_manager.get_quota_reserve_enabled()))

    @property
    def manual_floor(self) -> int:
        """Remaining calls at which manual actions are blocked."""
        return QUOTA_BOOTSTRAP_CALLS

    @property
    def hard_reserve(self) -> int:
        """Remaining calls at which polling pauses, before the poll cost."""
        if self.limit is None:
            return QUOTA_RESERVE_CALLS
        return max(QUOTA_RESERVE_CALLS, int(self.limit * QUOTA_RESERVE_PERCENT))

    @property
    def soft_low(self) -> int:
        """Remaining calls at which the integration starts conserving."""
        if self.limit is None:
            return LOW_QUOTA_RESERVE_FLOOR
        return max(LOW_QUOTA_RESERVE_FLOOR, int(self.limit * LOW_QUOTA_RESERVE_PERCENT))

    @property
    def interval_reserve(self) -> int:
        """Flat reserve used by the interval maths, not scaled by tier."""
        return INTERVAL_RESERVE_CALLS

    @property
    def svc_defer_at(self) -> int:
        """Remaining calls at which a schedule fetch defers."""
        return self.hard_reserve + MAX_SCHEDULE_FETCH_CALLS

    @property
    def full_sync_period(self) -> int:
        """Seconds between full syncs."""
        if self.has_slow_full_sync:
            return ZONES_INFO_REFRESH_SECONDS_FREE
        return ZONES_INFO_REFRESH_SECONDS_PAID

    @property
    def is_free_tier(self) -> bool:
        """True for a known limit at or below the soft-low floor."""
        return self.limit is not None and self.limit <= self.soft_low

    @property
    def has_slow_full_sync(self) -> bool:
        """True for a known limit inside the slow full-sync band."""
        return self.limit is not None and self.limit <= ZONES_INFO_FREE_TIER_THRESHOLD

    def low_quota_mode(self, remaining: int) -> bool:
        """True when the interval maths should use the low-quota mode."""
        return remaining <= self.soft_low

    def drift_allowed(self, remaining: int | None) -> bool:
        """True when the offset drift refresh may run: never on the free tier, below soft-low only with the switch off."""
        if self.is_free_tier:
            return False
        if self.reserve_enabled and isinstance(remaining, int) and not isinstance(remaining, bool):
            return remaining >= self.soft_low
        return True

    def svc_zone_deferred(self, remaining: int | None) -> bool:
        """True when a schedule fetch must wait: no reading yet, or remaining at the defer line."""
        return remaining is None or remaining <= self.svc_defer_at

    def schedule_budget(self, remaining: int | None) -> int:
        """Return the calls one setup run may spend filling the schedule cache; an unknown limit is sized as the smallest tier."""
        sizing_limit = self.limit if self.limit is not None else ASSUMED_DAILY_LIMIT_UNKNOWN_TIER
        spendable = (sizing_limit if remaining is None else remaining) - self.hard_reserve
        return max(0, min(spendable, int(sizing_limit * SCHEDULE_FILL_MAX_QUOTA_FRACTION)))

    def released(self, snapshot: Mapping[str, Any] | None, now: datetime) -> bool:
        """True when holds do not apply: switch off, no reset time known, or the reset has passed."""
        if not self.reserve_enabled:
            return True
        last_reset = snapshot.get("last_reset_utc") if isinstance(snapshot, Mapping) else None
        if not last_reset:
            return True
        next_reset = predicted_next_reset(last_reset)
        return next_reset is not None and now >= next_reset

    def poll_holds(self, snapshot: Mapping[str, Any] | None, cost: int, now: datetime) -> bool:
        """True when a poll of `cost` calls would end inside the hard reserve."""
        remaining = _remaining_of(snapshot)
        if remaining is None or self.released(snapshot, now):
            return False
        return remaining <= self.hard_reserve + cost

    def manual_holds(self, snapshot: Mapping[str, Any] | None, cost: int, now: datetime) -> bool:
        """True when a manual action of `cost` calls would end inside the manual floor."""
        remaining = _remaining_of(snapshot)
        if remaining is None or self.released(snapshot, now):
            return False
        return remaining <= self.manual_floor + max(cost, 1) - 1

    def quota_level(
        self, snapshot: Mapping[str, Any] | None,
    ) -> Literal["empty", "reserve", "low", "ok"] | None:
        """Classify `remaining`, or None when the snapshot or `remaining` is absent."""
        remaining = _remaining_of(snapshot)
        if remaining is None:
            return None
        if remaining <= 0:
            return "empty"
        if remaining <= self.hard_reserve:
            return "reserve"
        if remaining <= self.soft_low:
            return "low"
        return "ok"
