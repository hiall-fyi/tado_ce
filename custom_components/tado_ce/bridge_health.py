"""Bridge API health tracking for monitoring connectivity and response times."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.util import dt as dt_util

from .helpers import parse_iso_datetime


@dataclass
class BridgeHealthState:
    """Track Bridge API health metrics."""

    consecutive_failures: int = 0
    last_successful_poll: datetime | None = None
    last_error: str | None = None
    last_response_time_ms: float | None = None
    is_connected: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialize health state to a JSON-compatible dict."""
        return {
            "consecutive_failures": self.consecutive_failures,
            "last_successful_poll": (
                self.last_successful_poll.isoformat()
                if self.last_successful_poll
                else None
            ),
            "last_error": self.last_error,
            "last_response_time_ms": self.last_response_time_ms,
            "is_connected": self.is_connected,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> BridgeHealthState:
        """Restore health state from a persisted dict.

        Validates each field individually so corrupt data never causes a crash.
        """
        state = cls()
        if isinstance(data.get("consecutive_failures"), int):
            state.consecutive_failures = data["consecutive_failures"]  # type: ignore[assignment]
        poll_str = data.get("last_successful_poll")
        if isinstance(poll_str, str):
            try:
                state.last_successful_poll = parse_iso_datetime(poll_str)
            except (ValueError, TypeError):
                pass  # Corrupt timestamp: keep default None
        if isinstance(data.get("last_error"), str):
            state.last_error = data["last_error"]  # type: ignore[assignment]
        if isinstance(data.get("last_response_time_ms"), (int, float)):
            state.last_response_time_ms = float(data["last_response_time_ms"])  # type: ignore[arg-type]
        if isinstance(data.get("is_connected"), bool):
            state.is_connected = data["is_connected"]  # type: ignore[assignment]
        return state


class BridgeHealthTracker:
    """Track Bridge API health across coordinator polls."""

    def __init__(self, failure_threshold: int = 3) -> None:
        self._state = BridgeHealthState()
        self._failure_threshold = failure_threshold

    def record_success(self, response_time_ms: float) -> None:
        """Record a successful Bridge API poll."""
        self._state.consecutive_failures = 0
        self._state.last_successful_poll = dt_util.utcnow()
        self._state.last_error = None
        self._state.last_response_time_ms = response_time_ms
        self._state.is_connected = True

    def record_failure(self, error: str) -> None:
        """Record a failed Bridge API poll."""
        self._state.consecutive_failures += 1
        self._state.last_error = error
        if self._state.consecutive_failures >= self._failure_threshold:
            self._state.is_connected = False

    @property
    def state(self) -> BridgeHealthState:
        """Return current health state."""
        return self._state

    @property
    def failure_threshold(self) -> int:
        """Return the configured failure threshold."""
        return self._failure_threshold

    def to_dict(self) -> dict[str, object]:
        """Serialize tracker state for persistence."""
        return self._state.to_dict()

    @classmethod
    def from_dict(
        cls, data: dict[str, object], failure_threshold: int = 3,
    ) -> BridgeHealthTracker:
        """Restore tracker from a persisted dict."""
        tracker = cls(failure_threshold=failure_threshold)
        tracker._state = BridgeHealthState.from_dict(data)
        return tracker
