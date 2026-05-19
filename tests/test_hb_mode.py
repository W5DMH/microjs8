"""Tests for the HbMode state machine and ALLCALL/HB_MODE_SELECT
focus state. These exercise the pure-state side of the heartbeat
feature — set_hb_mode, the mode-change callback, focus cycling,
the open/close lifecycle of the HB_MODE_SELECT modal.

Beacon-thread lifecycle is tested separately in test_beacon.py;
router dispatch is tested in test_router.py; app-layer enqueue
paths in test_app_allcall.py.
"""

from __future__ import annotations

import pytest

from microjs8.ui.state import (
    HB_MODES_ORDERED,
    HbMode,
    Screen,
    UIState,
)


# ── Fixture ─────────────────────────────────────────────────────────


def _state(
    callsign: str = "K1ABC",
    grid: str = "FN42",
    tx_allowed: bool = True,
) -> UIState:
    return UIState(callsign=callsign, grid=grid, tx_allowed=tx_allowed)


# ── Default state ───────────────────────────────────────────────────


def test_hb_mode_defaults_to_off():
    """Spec §5.5: OFF is the default — operator opts in explicitly."""
    s = _state()
    assert s.hb_mode is HbMode.OFF
    assert s.snapshot().hb_mode is HbMode.OFF


def test_allcall_focus_defaults_to_zero():
    """First row (HEARTBEAT) focused on entry to ALLCALL screen."""
    s = _state()
    assert s.allcall_focus == 0
    assert s.snapshot().allcall_focus == 0


def test_hb_select_focus_defaults_to_zero():
    s = _state()
    assert s.hb_select_focus == 0


def test_hb_modes_ordered_has_four_modes():
    """Defensive — adding a new mode would need new tests + renderer work."""
    assert HB_MODES_ORDERED == (
        HbMode.OFF,
        HbMode.SINGLE,
        HbMode.TWENTY_MIN,
        HbMode.ONE_HR,
    )


# ── set_hb_mode + callback ──────────────────────────────────────────


def test_set_hb_mode_changes_mode_and_fires_callback():
    s = _state()
    calls: list[HbMode] = []
    s.set_hb_mode_change_callback(calls.append)
    s.set_hb_mode(HbMode.TWENTY_MIN)
    assert s.hb_mode is HbMode.TWENTY_MIN
    assert calls == [HbMode.TWENTY_MIN]


def test_set_hb_mode_no_op_when_already_at_mode():
    """Avoid spurious beacon-thread churn when committing the
    already-active mode on the modal."""
    s = _state()
    calls: list[HbMode] = []
    s.set_hb_mode_change_callback(calls.append)
    s.set_hb_mode(HbMode.SINGLE)
    assert calls == [HbMode.SINGLE]
    # Same mode again → no callback fire.
    s.set_hb_mode(HbMode.SINGLE)
    assert calls == [HbMode.SINGLE]


def test_set_hb_mode_callback_can_be_detached():
    s = _state()
    calls: list[HbMode] = []
    s.set_hb_mode_change_callback(calls.append)
    s.set_hb_mode_change_callback(None)
    s.set_hb_mode(HbMode.ONE_HR)
    assert s.hb_mode is HbMode.ONE_HR
    assert calls == []


def test_set_hb_mode_callback_exception_swallowed():
    """A misbehaving app-side callback must not corrupt UIState."""
    s = _state()

    def boom(_mode: HbMode) -> None:
        raise RuntimeError("test")

    s.set_hb_mode_change_callback(boom)
    s.set_hb_mode(HbMode.TWENTY_MIN)
    # The mode change itself still applies.
    assert s.hb_mode is HbMode.TWENTY_MIN


def test_set_hb_mode_sets_dirty_flag():
    s = _state()
    s.consume_dirty()  # drain any pending flag
    s.set_hb_mode(HbMode.TWENTY_MIN)
    assert s.consume_dirty() is True


# ── ALLCALL focus cycling ───────────────────────────────────────────


def test_allcall_focus_next_cycles_through_three_rows():
    s = _state()
    s.allcall_focus_next()
    assert s.allcall_focus == 1
    s.allcall_focus_next()
    assert s.allcall_focus == 2
    s.allcall_focus_next()
    assert s.allcall_focus == 0  # wraps


def test_allcall_focus_prev_wraps_negative():
    s = _state()
    s.allcall_focus_prev()
    assert s.allcall_focus == 2


def test_allcall_focus_sets_dirty():
    s = _state()
    s.consume_dirty()
    s.allcall_focus_next()
    assert s.consume_dirty() is True


# ── HB_MODE_SELECT open / close ─────────────────────────────────────


def test_open_hb_mode_select_transitions_screen():
    s = _state()
    s.set_screen(Screen.ALLCALL)
    s.open_hb_mode_select()
    assert s.snapshot().screen is Screen.HB_MODE_SELECT


def test_open_hb_mode_select_inits_focus_to_current_mode():
    s = _state()
    s.set_hb_mode(HbMode.ONE_HR)
    s.open_hb_mode_select()
    # ONE_HR is index 3 in HB_MODES_ORDERED.
    assert s.hb_select_focus == 3
    assert HB_MODES_ORDERED[3] is HbMode.ONE_HR


def test_open_hb_mode_select_focus_is_zero_when_off():
    s = _state()
    # Default OFF → index 0
    s.open_hb_mode_select()
    assert s.hb_select_focus == 0


def test_close_hb_mode_select_commit_applies_focused_mode():
    s = _state()
    s.set_screen(Screen.ALLCALL)
    s.open_hb_mode_select()
    # Focus moves to TWENTY_MIN (index 2).
    s.hb_select_focus_next()
    s.hb_select_focus_next()
    s.close_hb_mode_select(commit=True)
    assert s.snapshot().screen is Screen.ALLCALL
    assert s.hb_mode is HbMode.TWENTY_MIN


def test_close_hb_mode_select_cancel_preserves_mode():
    s = _state()
    s.set_hb_mode(HbMode.TWENTY_MIN)
    s.set_screen(Screen.ALLCALL)
    s.open_hb_mode_select()
    # Modal opens with focus at the current mode (TWENTY_MIN = index 2).
    assert s.hb_select_focus == 2
    # Focus moves to ONE_HR (index 3).
    s.hb_select_focus_next()
    assert s.hb_select_focus == 3
    # Cancel — mode unchanged from before the modal opened.
    s.close_hb_mode_select(commit=False)
    assert s.snapshot().screen is Screen.ALLCALL
    assert s.hb_mode is HbMode.TWENTY_MIN


def test_close_hb_mode_select_is_noop_outside_modal():
    """Defensive: caller shouldn't be calling close from elsewhere,
    but if they do, don't mangle state."""
    s = _state()
    s.set_screen(Screen.HOME)
    s.close_hb_mode_select(commit=True)
    assert s.snapshot().screen is Screen.HOME


# ── HB_MODE_SELECT focus cycling ────────────────────────────────────


def test_hb_select_focus_next_cycles_four_modes():
    s = _state()
    s.open_hb_mode_select()
    # Starts at OFF (index 0).
    s.hb_select_focus_next()
    assert s.hb_select_focus == 1
    s.hb_select_focus_next()
    assert s.hb_select_focus == 2
    s.hb_select_focus_next()
    assert s.hb_select_focus == 3
    s.hb_select_focus_next()
    assert s.hb_select_focus == 0  # wraps


def test_hb_select_focus_prev_wraps():
    s = _state()
    s.open_hb_mode_select()
    s.hb_select_focus_prev()
    assert s.hb_select_focus == 3  # wraps to last (ONE_HR)


# ── Snapshot exposure ───────────────────────────────────────────────


def test_snapshot_includes_hb_state():
    s = _state()
    s.set_hb_mode(HbMode.TWENTY_MIN)
    s.allcall_focus_next()
    snap = s.snapshot()
    assert snap.hb_mode is HbMode.TWENTY_MIN
    assert snap.allcall_focus == 1
    assert snap.hb_select_focus == 0
