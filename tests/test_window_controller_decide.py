"""Tests for the pure decision rules in window_controller."""

from __future__ import annotations

from custom_components.tado_ce.const import (
    WINDOW_ACTION_HOUSE,
    WINDOW_ACTION_NONE,
    WINDOW_ACTION_ROOM,
    WINDOW_RESUME_ALL,
    WINDOW_RESUME_OWN,
)
from custom_components.tado_ce.window_controller import (
    WindowState,
    WindowView,
    decide,
    holders_of,
    targets_of,
)

HEATING = frozenset({"1", "2", "3"})


def _view(actions: dict[str, str], open_zones: set[str], triggered: set[str] | None = None) -> WindowView:
    return WindowView(
        heating_zones=HEATING,
        actions=actions,
        open_zones=frozenset(open_zones),
        triggered=frozenset(open_zones if triggered is None else triggered),
    )


def test_targets_of() -> None:
    assert targets_of("1", WINDOW_ACTION_ROOM, HEATING) == {"1"}
    assert targets_of("1", WINDOW_ACTION_HOUSE, HEATING) == HEATING
    assert targets_of("1", WINDOW_ACTION_NONE, HEATING) == frozenset()
    assert targets_of("9", WINDOW_ACTION_ROOM, HEATING) == frozenset()


def test_room_action_switches_only_its_room_off() -> None:
    to_off, to_restore = decide(
        _view({"1": WINDOW_ACTION_ROOM}, {"1"}), WindowState(), enabled=True, resume_mode=WINDOW_RESUME_OWN,
    )
    assert to_off == {"1"}
    assert to_restore == set()


def test_not_triggered_before_delay() -> None:
    to_off, _ = decide(
        _view({"1": WINDOW_ACTION_ROOM}, {"1"}, triggered=set()), WindowState(),
        enabled=True, resume_mode=WINDOW_RESUME_OWN,
    )
    assert to_off == set()


def test_house_action_switches_every_room_off() -> None:
    to_off, _ = decide(
        _view({"1": WINDOW_ACTION_HOUSE}, {"1"}), WindowState(), enabled=True, resume_mode=WINDOW_RESUME_OWN,
    )
    assert to_off == set(HEATING)


def test_held_and_overridden_rooms_are_not_switched_off_again() -> None:
    state = WindowState(held={"1"}, overridden={"2"})
    to_off, to_restore = decide(
        _view({"1": WINDOW_ACTION_HOUSE}, {"1"}), state, enabled=True, resume_mode=WINDOW_RESUME_OWN,
    )
    assert to_off == {"3"}
    assert to_restore == set()


def test_resume_own_restores_when_own_window_closes() -> None:
    state = WindowState(held={"1", "2"})
    view = _view({"1": WINDOW_ACTION_ROOM, "2": WINDOW_ACTION_ROOM}, {"2"})
    to_off, to_restore = decide(view, state, enabled=True, resume_mode=WINDOW_RESUME_OWN)
    assert to_off == set()
    assert to_restore == {"1"}


def test_resume_all_waits_for_every_window() -> None:
    state = WindowState(held={"1", "2"})
    view = _view({"1": WINDOW_ACTION_ROOM, "2": WINDOW_ACTION_ROOM}, {"2"})
    _, to_restore = decide(view, state, enabled=True, resume_mode=WINDOW_RESUME_ALL)
    assert to_restore == set()

    _, to_restore = decide(_view({"1": WINDOW_ACTION_ROOM}, set()), state, enabled=True, resume_mode=WINDOW_RESUME_ALL)
    assert to_restore == {"1", "2"}


def test_disabled_restores_everything_held() -> None:
    state = WindowState(held={"1", "3"})
    to_off, to_restore = decide(
        _view({"1": WINDOW_ACTION_HOUSE}, {"1"}), state, enabled=False, resume_mode=WINDOW_RESUME_ALL,
    )
    assert to_off == set()
    assert to_restore == {"1", "3"}


def test_holders_of() -> None:
    view = _view({"1": WINDOW_ACTION_HOUSE, "2": WINDOW_ACTION_ROOM}, {"1", "2"})
    assert holders_of("2", view) == ["1", "2"]
    assert holders_of("3", view) == ["1"]
