"""Tado CE API write optimisation: guard, debounce, queue, coalesce.

Five primitives the climate / water_heater / switch entities use to
keep redundant or rapid-fire calls off the cloud API: `ActionGuard`
skips calls whose requested state already matches current state,
`ActionDebouncer` collapses bursts within a per-zone window,
`DeviceSyncQueue` serialises device-level writes with a configurable
gap, `RefreshCoalescer` collapses post-write coordinator refreshes
into one, `ResumeGuard` skips a schedule-resume call for a zone with
no active overlay. All five are zone-/entity-scoped, none mutate
state on their own.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from .helpers import get_zone_state

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.components.climate import HVACMode  # type: ignore[attr-defined]

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task[object]) -> None:
    """Log uncaught exceptions from fire-and-forget tasks at warning level."""
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.warning("Write Optimiser: background task failed: %s", task.exception())


class ActionGuard:
    """Skip API calls whose requested state already matches current state."""

    @staticmethod
    def should_skip_temperature(
        requested_temp: float | None,
        current_temp: float | None,
        requested_mode: HVACMode | None,
        current_mode: HVACMode | None,
        *,
        optimistic_active: bool = False,
    ) -> bool:
        """Return True when the requested temperature + mode would be a no-op write."""
        if optimistic_active:
            return False
        if requested_temp is None:
            return False
        if current_temp is None:
            return False
        temp_matches = requested_temp == current_temp
        mode_matches = requested_mode == current_mode
        return temp_matches and mode_matches

    @staticmethod
    def should_skip_hvac_mode(
        requested_mode: HVACMode,
        current_mode: HVACMode | None,
        *,
        optimistic_active: bool = False,
    ) -> bool:
        """Return True when the requested HVAC mode already matches current."""
        if optimistic_active:
            return False
        return requested_mode == current_mode

    @staticmethod
    def should_skip_fan_mode(
        requested_fan: str,
        current_fan: str | None,
    ) -> bool:
        """Return True when the requested fan mode already matches current."""
        return requested_fan == current_fan

    @staticmethod
    def should_skip_swing_mode(
        requested_swing: str,
        current_swing: str | None,
    ) -> bool:
        """Return True when the requested swing mode already matches current."""
        return requested_swing == current_swing

    @staticmethod
    def should_skip_preset_mode(
        requested_preset: str,
        current_preset: str | None,
    ) -> bool:
        """Return True when the requested preset mode already matches current."""
        return requested_preset == current_preset


class ActionDebouncer:
    """Per-zone debouncer for write callbacks (collapses bursts within a window)."""

    def __init__(self, default_window: float = 3.0) -> None:
        """Initialise the debouncer with a default window in seconds."""
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._pending_coros: dict[str, Callable[[], Awaitable[None]]] = {}
        self._default_window: float = default_window
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running_tasks: set[asyncio.Task[None]] = set()

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Return the cached event loop, acquiring it on first call."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    async def debounce(
        self,
        zone_id: str,
        callback: Callable[[], Awaitable[None]],
        window: float | None = None,
    ) -> None:
        """Run `callback` after the debounce window, cancelling any pending call.

        A non-positive window short-circuits the debounce and runs the
        callback immediately, useful for tests and for callers that
        want a synchronous "no debounce" path.
        """
        effective_window = window if window is not None else self._default_window

        if effective_window <= 0:
            await callback()
            return

        self.cancel(zone_id)

        loop = self._get_loop()
        self._pending_coros[zone_id] = callback

        def _fire() -> None:
            coro = self._pending_coros.pop(zone_id, None)
            self._pending.pop(zone_id, None)
            if coro is not None:
                task = asyncio.ensure_future(coro())
                self._running_tasks.add(task)
                task.add_done_callback(self._running_tasks.discard)
                task.add_done_callback(_log_task_exception)

        handle = loop.call_later(effective_window, _fire)
        self._pending[zone_id] = handle

    def cancel(self, zone_id: str) -> None:
        """Cancel any pending debounced call for one zone."""
        handle = self._pending.pop(zone_id, None)
        if handle is not None:
            handle.cancel()
        self._pending_coros.pop(zone_id, None)

    def cancel_all(self) -> None:
        """Cancel every pending debounce (called during integration unload)."""
        if self._pending:
            _LOGGER.warning(
                "Write Optimiser: shutdown dropped %d pending debounced "
                "action(s) for zone(s) %s",
                len(self._pending),
                ", ".join(sorted(self._pending.keys())),
            )
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()
        self._pending_coros.clear()
        for task in self._running_tasks:
            task.cancel()
        self._running_tasks.clear()

    @property
    def pending_zones(self) -> set[str]:
        """Return the set of zone IDs with a pending debounced call."""
        return set(self._pending.keys())


@dataclass
class DeviceOperation:
    """One queued device-level API operation with a completion future.

    `done` is resolved by `DeviceSyncQueue._process_queue` when the
    callback finishes. Callers that care about the outcome, beyond
    "was the operation accepted into the queue", should await this
    future; the queue itself no longer swallows False-return or
    raised exceptions silently.
    """

    device_serial: str
    operation_name: str
    callback: Callable[[], Awaitable[bool]]
    entity_id: str
    done: asyncio.Future[bool] | None = None


class DeviceSyncQueue:
    """FIFO queue that runs device-level API operations one at a time."""

    def __init__(self, delay: float = 1.0, max_depth: int = 20) -> None:
        """Initialise the queue with the desired between-op delay and capacity."""
        self._queue: asyncio.Queue[DeviceOperation] = asyncio.Queue(maxsize=max_depth)
        self._delay: float = delay
        self._max_depth: int = max_depth
        self._processor_task: asyncio.Task[None] | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()

    async def enqueue(
        self, operation: DeviceOperation,
    ) -> tuple[bool, asyncio.Future[bool]]:
        """Add an operation to the queue, returning (accepted, completion_future).

        `accepted` is False when the queue is full. In that case the
        completion future is already resolved to False so callers
        awaiting it never block.
        """
        loop = asyncio.get_running_loop()
        done: asyncio.Future[bool] = loop.create_future()
        operation.done = done

        try:
            self._queue.put_nowait(operation)
        except asyncio.QueueFull:
            _LOGGER.warning(
                "Write Optimiser: device-sync queue full (%s/%s), "
                "rejecting %s for %s, will retry on next user action",
                self._queue.qsize(),
                self._max_depth,
                operation.operation_name,
                operation.entity_id,
            )
            done.set_result(False)
            return False, done

        _LOGGER.debug(
            "Write Optimiser: enqueued %s for %s (queue depth %s)",
            operation.operation_name,
            operation.entity_id,
            self._queue.qsize(),
        )

        if self._processor_task is None or self._processor_task.done():
            self._shutdown_event.clear()
            self._processor_task = asyncio.create_task(self._process_queue())

        return True, done

    async def _process_queue(self) -> None:
        """Drain the queue FIFO with `_delay` between operations.

        Fail-forward: a single operation raising or returning False
        does not stop the queue: the next operation still runs.
        """
        is_first = True
        while not self._queue.empty() and not self._shutdown_event.is_set():
            operation = self._queue.get_nowait()

            if not is_first and self._delay > 0:
                await asyncio.sleep(self._delay)
            is_first = False

            try:
                result = await operation.callback()
                _LOGGER.debug(
                    "Write Optimiser: %s completed for %s (result=%s)",
                    operation.operation_name,
                    operation.entity_id,
                    result,
                )
                if operation.done is not None and not operation.done.done():
                    operation.done.set_result(bool(result))
            except Exception:
                _LOGGER.warning(
                    "Write Optimiser: %s failed for %s, operation "
                    "will not be retried automatically",
                    operation.operation_name,
                    operation.entity_id,
                    exc_info=True,
                )
                if operation.done is not None and not operation.done.done():
                    operation.done.set_result(False)
            finally:
                self._queue.task_done()

    @property
    def queue_depth(self) -> int:
        """Return the current queue depth."""
        return self._queue.qsize()

    async def shutdown(self) -> None:
        """Cancel the processor task and drop any remaining queued operations."""
        self._shutdown_event.set()

        if self._processor_task is not None and not self._processor_task.done():
            self._processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor_task
            self._processor_task = None

        dropped_count = 0
        while not self._queue.empty():
            try:
                op = self._queue.get_nowait()
                _LOGGER.warning(
                    "Write Optimiser: shutdown dropped queued operation "
                    "%s for %s",
                    op.operation_name,
                    op.entity_id,
                )
                self._queue.task_done()
                dropped_count += 1
            except asyncio.QueueEmpty:
                break

        if dropped_count:
            _LOGGER.warning(
                "Write Optimiser: shutdown dropped %d queued device "
                "operation(s) in total",
                dropped_count,
            )


class RefreshCoalescer:
    """Collapse multiple post-write coordinator refreshes into one debounced call."""

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        window: float = 2.0,
        *,
        skip_when_fresh: bool = False,
    ) -> None:
        """Initialise the coalescer with a window and freshness-skip toggle."""
        self._coordinator = coordinator
        self._window: float = window
        self._pending_timer: asyncio.TimerHandle | None = None
        self._pending_count: int = 0
        self._skip_when_fresh: bool = skip_when_fresh
        self._loop: asyncio.AbstractEventLoop | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Return the cached event loop, acquiring it on first call."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def schedule_refresh(self, entity_id: str | None = None) -> None:
        """Schedule a coalesced refresh after the window, skipping when entity is fresh.

        When `skip_when_fresh` is enabled and the entity has had a
        recent API call, the refresh is dropped entirely. The next
        scheduled poll picks up the state without an extra request.
        """
        if (
            entity_id
            and self._skip_when_fresh
            and self._coordinator.is_entity_fresh(entity_id)
        ):
            _LOGGER.debug(
                "Write Optimiser: %s is still fresh, skipping coalesced "
                "refresh, next poll will pick it up",
                entity_id,
            )
            return

        self._pending_count += 1

        if self._pending_timer is not None:
            self._pending_timer.cancel()

        loop = self._get_loop()
        self._pending_timer = loop.call_later(self._window, self._fire_refresh)

    def _fire_refresh(self) -> None:
        """Run the coalesced coordinator refresh."""
        self._pending_count = 0
        self._pending_timer = None
        task = asyncio.ensure_future(
            self._coordinator.async_request_refresh(),
        )
        self._refresh_task = task
        task.add_done_callback(_log_task_exception)

    def cancel(self) -> None:
        """Cancel any pending coalesced refresh (called during integration unload)."""
        if self._pending_count > 0:
            _LOGGER.debug(
                "Write Optimiser: shutdown cancelled coalesced refresh "
                "(%d pending entity update(s))",
                self._pending_count,
            )
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            self._refresh_task = None
        self._pending_count = 0

    @property
    def pending_count(self) -> int:
        """Return the number of writes queued behind the current coalesce window."""
        return self._pending_count


class ResumeGuard:
    """Skip schedule-resume calls for zones already running their schedule."""

    @staticmethod
    def should_skip_resume(
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
    ) -> bool:
        """Return True when the zone has no active overlay (no API call needed)."""
        coord_data = coordinator.data or {}
        zone_data = get_zone_state(coord_data, zone_id) or {}
        overlay_type = zone_data.get("overlayType")
        return overlay_type is None
