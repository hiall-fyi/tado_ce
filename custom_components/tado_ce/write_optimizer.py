"""Tado CE API write optimisation: guard, debounce, queue.

Four primitives the climate / water_heater / switch entities use to
keep redundant or rapid-fire calls off the cloud API: `ActionGuard`
skips calls whose requested state already matches current state,
`ActionDebouncer` collapses bursts within a per-zone window,
`DeviceSyncQueue` serialises device-level writes with a configurable
gap, `ResumeGuard` skips a schedule-resume call for a zone with
no active overlay. All four are zone-/entity-scoped, none mutate
state on their own.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError
from .helpers import get_zone_state

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.components.climate import HVACMode

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task[object]) -> None:
    """Log uncaught exceptions from fire-and-forget tasks at warning level."""
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.warning("Write Optimiser: background task failed: %s", task.exception())


def held_data_unconfirmed(coordinator: TadoDataUpdateCoordinator, zone_id: str | None = None) -> bool:
    """True while polling is paused or the zone's last write is of unknown outcome: the data on hand cannot say what Tado holds, so no guard may skip on it."""
    if getattr(coordinator, "polling_paused", False) is True:
        return True
    ledger = getattr(coordinator, "write_ledger", None)
    return zone_id is not None and ledger is not None and ledger.unconfirmed(zone_id) is True


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
        requested_mode: str,
        current_mode: str | None,
        *,
        optimistic_active: bool = False,
    ) -> bool:
        """Return True when the requested HVAC mode matches current and no optimistic hold is active."""
        if optimistic_active:
            return False
        return requested_mode == current_mode

    @staticmethod
    def should_skip_fan_mode(
        requested_fan: str,
        current_fan: str | None,
        *,
        optimistic_active: bool = False,
    ) -> bool:
        """Return True when the requested fan mode matches current and no optimistic hold is active."""
        if optimistic_active:
            return False
        return requested_fan == current_fan

    @staticmethod
    def should_skip_swing_mode(
        requested_swing: str,
        current_swing: str | None,
        *,
        optimistic_active: bool = False,
    ) -> bool:
        """Return True when the requested swing mode matches current and no optimistic hold is active."""
        if optimistic_active:
            return False
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
        # Told of each zone whose waiting call is cancelled, so the write ledger can drop its queued mark.
        self.on_cancel: Callable[[str], None] | None = None

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

        # A replaced call is not a cancel: the newer call was announced before this one, and its mark stays.
        self._drop_pending(zone_id)

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

    def _drop_pending(self, zone_id: str) -> bool:
        """Drop the zone's waiting call; True when one was waiting."""
        handle = self._pending.pop(zone_id, None)
        if handle is not None:
            handle.cancel()
        self._pending_coros.pop(zone_id, None)
        return handle is not None

    def cancel(self, zone_id: str) -> None:
        """Cancel any pending debounced call for one zone."""
        if self._drop_pending(zone_id) and self.on_cancel is not None:
            self.on_cancel(zone_id)

    def cancel_all(self) -> None:
        """Cancel every pending debounce (called during integration unload)."""
        if self._pending:
            _LOGGER.warning(
                "Write Optimiser: shutdown dropped %d pending debounced "
                "action(s) for zone(s) %s",
                len(self._pending),
                ", ".join(sorted(self._pending.keys())),
            )
        waiting = tuple(self._pending)
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()
        if self.on_cancel is not None:
            for zone_id in waiting:
                self.on_cancel(zone_id)
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
    error: TadoAuthError | TadoRateLimitError | TadoBlockedError | None = None
    user_initiated: bool = False


class DeviceSyncQueue:
    """Runs device-level API operations one at a time, user-initiated first, FIFO inside each group."""

    def __init__(self, delay: float = 1.0, max_depth: int = 20, cloud_held: Callable[[], bool] | None = None) -> None:
        """Initialise the queue with the between-op delay, capacity and an optional cloud-hold reader."""
        self._cloud_held = cloud_held
        self._queue: asyncio.PriorityQueue[tuple[int, int, DeviceOperation]] = asyncio.PriorityQueue(maxsize=max_depth)
        self._arrivals: int = 0
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
            # seq keeps ties from ever comparing operation objects
            self._queue.put_nowait((0 if operation.user_initiated else 1, self._arrivals, operation))
            self._arrivals += 1
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
        """Drain the queue, user-initiated first, with `_delay` between operations.

        Fail-forward: a single operation raising or returning False
        does not stop the queue: the next operation still runs.
        """
        is_first = True
        refused = 0
        while not self._queue.empty() and not self._shutdown_event.is_set():
            _, _, operation = self._queue.get_nowait()

            if not is_first and self._delay > 0:
                await asyncio.sleep(self._delay)
            is_first = False

            try:
                if self._is_cloud_held():
                    # Not sent and not parked: the caller repeats the action once the hold clears.
                    refused += 1
                    is_first = True
                    if operation.done is not None and not operation.done.done():
                        operation.done.set_result(False)
                    continue
                result = await operation.callback()
                _LOGGER.debug(
                    "Write Optimiser: %s completed for %s (result=%s)",
                    operation.operation_name,
                    operation.entity_id,
                    result,
                )
                if operation.done is not None and not operation.done.done():
                    operation.done.set_result(bool(result))
            except Exception as err:
                # Broad on purpose: one failing device must not stop the queue; a typed cloud refusal is kept for the caller.
                if isinstance(err, (TadoAuthError, TadoRateLimitError, TadoBlockedError)):
                    operation.error = err
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

        if refused:
            _LOGGER.debug("Write Optimiser: device-sync queue: cloud holds, %d operation(s) not sent", refused)

    def _is_cloud_held(self) -> bool:
        """Return True when the cloud-hold reader says so; a reader that raises counts as not held."""
        if self._cloud_held is None:
            return False
        try:
            return bool(self._cloud_held())
        except Exception:
            # Broad on purpose: a broken health reader must not stop device writes, today's behaviour applies.
            _LOGGER.warning("Write Optimiser: cloud_held check failed, treating the cloud as not held", exc_info=True)
            return False

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
                _, _, op = self._queue.get_nowait()
                _LOGGER.warning(
                    "Write Optimiser: shutdown dropped queued operation "
                    "%s for %s",
                    op.operation_name,
                    op.entity_id,
                )
                if op.done is not None and not op.done.done():
                    op.done.set_result(False)
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


class ResumeGuard:
    """Skip schedule-resume calls for zones already running their schedule."""

    @staticmethod
    def should_skip_resume(
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        *,
        owed_before: bool = False,
    ) -> bool:
        """Return True when the zone has no active overlay (no API call needed) in data that already shows our last write."""
        # Held data can be hours old while polling is paused, and data from before a timed-out write cannot show whether it landed.
        if held_data_unconfirmed(coordinator, zone_id) or not coordinator.zone_data_covers_own_write(zone_id):
            return False
        # A write in flight or waiting for the zone's slot is not in the data yet, so the schedule it is about to replace is no reason to skip.
        ledger = getattr(coordinator, "write_ledger", None)
        if ledger is not None and ledger.busy(zone_id) is True:
            return False
        # A change still owed was sent over HomeKit and is not in data from before the outage; `owed_before` is the caller's read from before it superseded the entry.
        if owed_before or (ledger is not None and ledger.owes_change(zone_id) is True):
            return False
        coord_data = coordinator.data or {}
        zone_data = get_zone_state(coord_data, zone_id) or {}
        overlay_type = zone_data.get("overlayType")
        return overlay_type is None
