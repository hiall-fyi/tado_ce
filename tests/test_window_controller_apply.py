"""Tests for WindowController's apply pass and restore retries (no Home Assistant instance needed)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.tado_ce import window_controller as wc
from custom_components.tado_ce.const import WINDOW_ACTION_ROOM, WINDOW_RESUME_OWN
from custom_components.tado_ce.window_controller import WindowController, WindowView

ZONE = "1"
HEATING = frozenset({ZONE, "2"})


def _controller() -> WindowController:
    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("Tado unreachable"))
    coordinator = MagicMock()
    coordinator.config_manager.get_window_control_enabled.return_value = True
    coordinator.config_manager.get_window_resume_mode.return_value = WINDOW_RESUME_OWN
    ctrl = WindowController(hass, coordinator)
    ctrl._climate_entity_id = MagicMock(return_value="climate.living_room")  # type: ignore[method-assign]
    ctrl._zone_name = MagicMock(return_value="Living room")  # type: ignore[method-assign]
    ctrl._refresh_entity = MagicMock()  # type: ignore[method-assign]
    ctrl._async_drop_capture = AsyncMock()  # type: ignore[method-assign]
    return ctrl


def _view(open_zones: set[str]) -> WindowView:
    return WindowView(
        heating_zones=HEATING,
        actions={ZONE: WINDOW_ACTION_ROOM},
        open_zones=frozenset(open_zones),
        triggered=frozenset(open_zones),
    )


def _held_and_reasserting(ctrl: WindowController) -> None:
    ctrl._state.held.add(ZONE)
    ctrl._reassert.add(ZONE)


def test_failed_restore_is_not_switched_off_again() -> None:
    ctrl = _controller()
    _held_and_reasserting(ctrl)
    ctrl._view = MagicMock(return_value=_view(set()))  # type: ignore[method-assign]
    ctrl._async_restore = AsyncMock()  # type: ignore[method-assign]  # fails: the room stays held
    ctrl._async_reassert_off = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(ctrl._async_apply("test"))

    ctrl._async_restore.assert_awaited_once_with(ZONE)
    ctrl._async_reassert_off.assert_not_awaited()


def test_held_room_is_switched_off_again_while_its_window_is_open() -> None:
    ctrl = _controller()
    _held_and_reasserting(ctrl)
    ctrl._view = MagicMock(return_value=_view({ZONE}))  # type: ignore[method-assign]
    ctrl._async_restore = AsyncMock()  # type: ignore[method-assign]
    ctrl._async_reassert_off = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(ctrl._async_apply("test"))

    ctrl._async_restore.assert_not_awaited()
    ctrl._async_reassert_off.assert_awaited_once_with(ZONE)


def test_restore_failures_count_once_per_retry_window() -> None:
    ctrl = _controller()
    ctrl._state.held.add(ZONE)
    retries: list[Any] = []

    def _call_later(_hass: Any, _delay: float, action: Any) -> MagicMock:
        retries.append(action)
        return MagicMock()

    async def _run() -> None:
        with patch.object(wc, "async_call_later", side_effect=_call_later), \
                patch.object(ctrl, "_schedule_apply"):
            for _ in range(3):  # passes started by sensor or settings changes
                await ctrl._async_restore(ZONE)
            assert ctrl._restore_failures[ZONE] == 1
            assert len(retries) == 1

            retries[0](None)  # the 5-minute retry fires
            await ctrl._async_restore(ZONE)
            assert ctrl._restore_failures[ZONE] == 2

    asyncio.run(_run())
    assert ZONE in ctrl._state.held


def test_restore_is_given_up_after_max_attempts() -> None:
    ctrl = _controller()
    ctrl._state.held.add(ZONE)
    ctrl._restore_failures[ZONE] = wc._RESTORE_MAX_ATTEMPTS - 1

    with patch.object(wc, "async_call_later", return_value=MagicMock()):
        asyncio.run(ctrl._async_restore(ZONE))

    assert ZONE not in ctrl._state.held
    assert ZONE not in ctrl._restore_failures
    assert ZONE not in ctrl._failure_counted
    ctrl._async_drop_capture.assert_awaited_once_with(ZONE)


def test_drop_zone_clears_retry_state() -> None:
    ctrl = _controller()
    _held_and_reasserting(ctrl)
    ctrl._restore_failures[ZONE] = 3
    ctrl._failure_counted.add(ZONE)

    assert ctrl.drop_zone(ZONE) is True

    assert ZONE not in ctrl._state.held
    assert ZONE not in ctrl._reassert
    assert ZONE not in ctrl._restore_failures
    assert ZONE not in ctrl._failure_counted
