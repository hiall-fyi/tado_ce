"""CloudHealth: one pure value for "is the cloud down", written through a single mutator."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import logging
import math
from typing import Any, Final

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

BACKOFF_FLOOR_SECONDS: Final = 60
BACKOFF_CAP_SECONDS: Final = 86400
RESET_SLACK_SECONDS: Final = 120
BLOCKED_ISSUE_ATTEMPTS: Final = 3
BLOCKED_ISSUE_MINUTES: Final = 60


class CloudHealthState(StrEnum):
    """Displayed cloud state, highest precedence first after OK."""

    OK = "ok"
    BACKOFF = "backoff"
    UNREACHABLE = "unreachable"
    BLOCKED = "blocked"
    AUTH_FAILED = "auth_failed"


class CloudEvent(StrEnum):
    """One classified cloud outcome fed to the single mutator."""

    REST_SUCCESS = "rest_success"
    REST_429 = "rest_429"
    TOKEN_429 = "token_429"
    REST_5XX_EXHAUSTED = "rest_5xx_exhausted"
    REST_NETWORK_EXHAUSTED = "rest_network_exhausted"
    TOKEN_5XX_EXHAUSTED = "token_5xx_exhausted"
    TOKEN_TIMEOUT = "token_timeout"
    TOKEN_NETWORK_EXHAUSTED = "token_network_exhausted"
    REST_403_PERSISTENT = "rest_403_persistent"
    TOKEN_403_PERSISTENT = "token_403_persistent"
    TOKEN_INVALID_GRANT = "token_invalid_grant"
    TOKEN_OTHER_4XX = "token_other_4xx"
    REST_401_SECOND = "rest_401_second"
    HOME_ID_MISSING = "home_id_missing"


EVENT_STATE: Final[Mapping[CloudEvent, CloudHealthState | None]] = {
    CloudEvent.REST_SUCCESS: None,
    CloudEvent.REST_429: CloudHealthState.BACKOFF,
    CloudEvent.TOKEN_429: CloudHealthState.BACKOFF,
    CloudEvent.REST_5XX_EXHAUSTED: CloudHealthState.UNREACHABLE,
    CloudEvent.REST_NETWORK_EXHAUSTED: CloudHealthState.UNREACHABLE,
    CloudEvent.TOKEN_5XX_EXHAUSTED: CloudHealthState.UNREACHABLE,
    CloudEvent.TOKEN_TIMEOUT: CloudHealthState.UNREACHABLE,
    CloudEvent.TOKEN_NETWORK_EXHAUSTED: CloudHealthState.UNREACHABLE,
    CloudEvent.REST_403_PERSISTENT: CloudHealthState.BLOCKED,
    CloudEvent.TOKEN_403_PERSISTENT: CloudHealthState.BLOCKED,
    CloudEvent.TOKEN_INVALID_GRANT: CloudHealthState.AUTH_FAILED,
    CloudEvent.TOKEN_OTHER_4XX: CloudHealthState.AUTH_FAILED,
    CloudEvent.REST_401_SECOND: CloudHealthState.AUTH_FAILED,
    CloudEvent.HOME_ID_MISSING: CloudHealthState.AUTH_FAILED,
}


_BLOCKED_EVENTS: Final = frozenset({CloudEvent.REST_403_PERSISTENT, CloudEvent.TOKEN_403_PERSISTENT})


@dataclass
class _BlockedRecord:
    attempts: int = 0
    first_at: datetime | None = None


# Module scope so setup retries, which rebuild the coordinator and its CloudHealth, keep counting
_BLOCKED_RECORDS: dict[str, _BlockedRecord] = {}


def _blocked_key(home_id: str | None) -> str:
    return home_id or "default"


def reset_blocked_record(home_id: str | None) -> None:
    """Forget the BLOCKED attempt record for a home."""
    _BLOCKED_RECORDS.pop(_blocked_key(home_id), None)


def _backoff_seconds(retry_after: object) -> float:
    """Return a finite window length in [floor, cap]; garbage input counts as 0."""
    if isinstance(retry_after, bool) or not isinstance(retry_after, int | float):
        value = 0.0
    else:
        try:
            value = float(retry_after)
        except OverflowError:
            value = float(BACKOFF_CAP_SECONDS)
    if not math.isfinite(value):
        value = float(BACKOFF_CAP_SECONDS) if value == math.inf else 0.0
    return min(max(value, BACKOFF_FLOOR_SECONDS), BACKOFF_CAP_SECONDS)


@dataclass(frozen=True)
class CloudHealthChange:
    """What one `record` call changed, handed to every listener."""

    previous: CloudHealthState
    current: CloudHealthState
    event: CloudEvent
    first_success: bool
    backoff_changed: bool
    blocked_issue_due: bool


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class CloudHealth:
    """Independent cloud condition fields behind one `record` mutator."""

    def __init__(self, home_id: str | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self._home_id = home_id
        self._clock = clock or dt_util.utcnow
        self._backoff_until: datetime | None = None
        self._unreachable_since: datetime | None = None
        self._blocked_since: datetime | None = None
        self._auth_failed_since: datetime | None = None
        self._seen_success = False
        self._consecutive_429 = 0
        self._last_window_seconds = 0.0
        self._listeners: list[Callable[[CloudHealthChange], None]] = []

    @property
    def backoff_until(self) -> datetime | None:
        """End of the current backoff window, kept after it passes until a REST success."""
        return self._backoff_until

    @property
    def unreachable_since(self) -> datetime | None:
        """When UNREACHABLE was first recorded, or None."""
        return self._unreachable_since

    @property
    def blocked_since(self) -> datetime | None:
        """When BLOCKED was first recorded, or None."""
        return self._blocked_since

    @property
    def auth_failed_since(self) -> datetime | None:
        """When AUTH_FAILED was first recorded, or None."""
        return self._auth_failed_since

    @property
    def auth_failed(self) -> bool:
        """Return True while an auth failure is recorded."""
        return self._auth_failed_since is not None

    @property
    def state(self) -> CloudHealthState:
        """Return the highest-precedence stored condition."""
        if self._auth_failed_since is not None:
            return CloudHealthState.AUTH_FAILED
        if self._blocked_since is not None:
            return CloudHealthState.BLOCKED
        # BACKOFF stays displayed after `until` passes until a REST success
        if self._backoff_until is not None:
            return CloudHealthState.BACKOFF
        if self._unreachable_since is not None:
            return CloudHealthState.UNREACHABLE
        return CloudHealthState.OK

    def is_backoff_active(self) -> bool:
        """Return True while now is before the backoff window end."""
        return self._backoff_until is not None and self._clock() < self._backoff_until

    def backoff_remaining_seconds(self) -> float:
        """Return seconds left in the backoff window, 0 when none."""
        if self._backoff_until is None:
            return 0.0
        return max((self._backoff_until - self._clock()).total_seconds(), 0.0)

    def holds(self) -> bool:
        """Return True while callers should hold cloud calls."""
        return self.is_backoff_active() or self._unreachable_since is not None or self._blocked_since is not None

    def add_listener(self, listener: Callable[[CloudHealthChange], None]) -> Callable[[], None]:
        """Subscribe to transitions and return the unsubscribe callable."""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe

    def snapshot(self, quota_level: str | None = None) -> dict[str, Any]:
        """Return the diagnostics view with ISO timestamps."""
        return {
            "state": self.state.value,
            "backoff_until": _iso(self._backoff_until),
            "backoff_active": self.is_backoff_active(),
            "unreachable_since": _iso(self._unreachable_since),
            "blocked_since": _iso(self._blocked_since),
            "auth_failed_since": _iso(self._auth_failed_since),
            "quota_level": quota_level,
        }

    def record(
        self,
        event: CloudEvent,
        *,
        retry_after: float = 0.0,
        exhausted: bool = False,
        reset_cap_seconds: float | None = None,
    ) -> CloudHealthChange:
        """Apply one cloud outcome, notify listeners on a transition, and return the change."""
        previous = self.state
        previous_window = self._backoff_until
        self._apply(event, retry_after, exhausted, reset_cap_seconds)
        blocked_issue_due = self._count_blocked(event)
        first_success = event is CloudEvent.REST_SUCCESS and not self._seen_success
        if event is CloudEvent.REST_SUCCESS:
            self._seen_success = True
        change = CloudHealthChange(
            previous=previous,
            current=self.state,
            event=event,
            first_success=first_success,
            backoff_changed=self._backoff_until != previous_window,
            blocked_issue_due=blocked_issue_due,
        )
        if (
            change.previous is not change.current
            or change.backoff_changed
            or change.first_success
            or change.blocked_issue_due
        ):
            self._dispatch(change)
        return change

    def _count_blocked(self, event: CloudEvent) -> bool:
        """Count a BLOCKED attempt (reset on REST success) and return whether the issue is due."""
        if event is CloudEvent.REST_SUCCESS:
            reset_blocked_record(self._home_id)
            return False
        if event not in _BLOCKED_EVENTS:
            return False
        now = self._clock()
        record = _BLOCKED_RECORDS.setdefault(_blocked_key(self._home_id), _BlockedRecord())
        record.attempts += 1
        record.first_at = record.first_at or now
        elapsed = now - record.first_at
        return record.attempts >= BLOCKED_ISSUE_ATTEMPTS or elapsed >= timedelta(minutes=BLOCKED_ISSUE_MINUTES)

    def _dispatch(self, change: CloudHealthChange) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(change)
            except Exception:  # a repair or log listener must never break a cloud call
                _LOGGER.warning("CloudHealth: listener failed", exc_info=True)

    def _apply(self, event: CloudEvent, retry_after: float, exhausted: bool, reset_cap_seconds: float | None) -> None:
        now = self._clock()
        target = EVENT_STATE[event]
        if target is None:
            self._backoff_until = None
            self._unreachable_since = None
            self._blocked_since = None
            self._auth_failed_since = None
            self._consecutive_429 = 0
            self._last_window_seconds = 0.0
        elif target is CloudHealthState.BACKOFF:
            self._consecutive_429 += 1
            seconds = _backoff_seconds(retry_after)
            # WHY: with the quota used up the header value repeats for ever, so from the second 429 the window doubles up to the reset
            clipped = False
            if exhausted and self._consecutive_429 >= 2:
                seconds, clipped = self._escalated_seconds(seconds, reset_cap_seconds, now)
            self._last_window_seconds = seconds
            if clipped:
                # WHY: a 429 at the clipped window's end means the reset came late, so the wait starts again from the header value instead of doubling past the reset
                self._consecutive_429 = 0
                self._last_window_seconds = 0.0
            window = now + timedelta(seconds=seconds)
            if self._backoff_until is None or window > self._backoff_until:
                self._backoff_until = window
        elif target is CloudHealthState.UNREACHABLE:
            self._unreachable_since = self._unreachable_since or now
        elif target is CloudHealthState.BLOCKED:
            self._blocked_since = self._blocked_since or now
        else:
            self._auth_failed_since = self._auth_failed_since or now

    def _escalated_seconds(self, header_seconds: float, reset_cap_seconds: float | None, now: datetime) -> tuple[float, bool]:
        """Double the longer of the header window and the last window, capped at the reset plus slack; True when the window reaches the predicted reset."""
        seconds = min(max(header_seconds, self._last_window_seconds) * 2, BACKOFF_CAP_SECONDS)
        clipped = False
        # WHY: the slack covers the reset itself answering one more 429; a bad cap means no reset cap
        if (
            not isinstance(reset_cap_seconds, bool)
            and isinstance(reset_cap_seconds, int | float)
            and math.isfinite(reset_cap_seconds)
            and reset_cap_seconds > 0
        ):
            seconds = min(seconds, reset_cap_seconds + RESET_SLACK_SECONDS)
            clipped = seconds >= reset_cap_seconds
        _LOGGER.info(
            "CloudHealth: 429 number %d with no calls left, waiting %ds (until %s) so the retries stop wasting calls; the window ends at the quota reset at the latest",
            self._consecutive_429, seconds, (now + timedelta(seconds=seconds)).strftime("%H:%M:%S UTC"),
        )
        return seconds, clipped
