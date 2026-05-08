"""Tests for minijs8.ui.state — ring navigation and shutdown bookkeeping."""

from __future__ import annotations

import pytest

from minijs8.ui.state import RING, Screen, UIState


def _state(callsign: str = "K1ABC", grid: str = "FN42", tx_allowed: bool = True) -> UIState:
    return UIState(callsign=callsign, grid=grid, tx_allowed=tx_allowed)


# ── Ring navigation ─────────────────────────────────────────────────


def test_initial_screen_is_home():
    s = _state()
    assert s.snapshot().screen is Screen.HOME


def test_advance_visits_each_ring_screen_then_wraps():
    s = _state()
    seen = [s.snapshot().screen]
    for _ in range(len(RING) - 1):
        s.advance_ring()
        seen.append(s.snapshot().screen)
    assert seen == list(RING)
    # One more advance wraps back to HOME.
    s.advance_ring()
    assert s.snapshot().screen is Screen.HOME


def test_retreat_from_home_wraps_to_last():
    s = _state()
    s.retreat_ring()
    assert s.snapshot().screen is RING[-1]


def test_retreat_then_advance_round_trips():
    s = _state()
    original = s.snapshot().screen
    s.retreat_ring()
    s.advance_ring()
    assert s.snapshot().screen is original


def test_shutting_down_screen_is_not_in_ring():
    """The transient shutdown screen must not be reachable via ← / →."""
    assert Screen.SHUTTING_DOWN not in RING


# ── Shutdown gesture state ──────────────────────────────────────────


def test_begin_shutdown_remembers_previous_screen():
    s = _state()
    s.advance_ring()
    s.advance_ring()
    prev = s.snapshot().screen
    s.begin_shutdown()
    snap = s.snapshot()
    assert snap.screen is Screen.SHUTTING_DOWN
    assert snap.previous_screen is prev


def test_cancel_shutdown_returns_to_previous_screen():
    s = _state()
    s.advance_ring()  # now HEARD
    s.begin_shutdown()
    s.cancel_shutdown()
    assert s.snapshot().screen is Screen.HEARD


def test_shutdown_progress_clamped_to_unit_interval():
    s = _state()
    s.begin_shutdown()
    s.update_shutdown_progress(2.0)
    assert s.snapshot().shutdown_remaining == 1.0
    s.update_shutdown_progress(-0.5)
    assert s.snapshot().shutdown_remaining == 0.0


def test_begin_shutdown_resets_progress_to_full():
    s = _state()
    s.begin_shutdown()
    s.update_shutdown_progress(0.2)
    s.cancel_shutdown()
    s.begin_shutdown()
    assert s.snapshot().shutdown_remaining == 1.0


# ── Dirty-flag plumbing ─────────────────────────────────────────────


def test_initial_state_is_dirty():
    """A fresh UIState must request an initial render."""
    s = _state()
    assert s.dirty.is_set()


def test_consume_dirty_clears_flag():
    s = _state()
    assert s.consume_dirty() is True
    assert s.consume_dirty() is False
    assert not s.dirty.is_set()


def test_advance_marks_dirty():
    s = _state()
    s.consume_dirty()
    s.advance_ring()
    assert s.consume_dirty() is True


def test_set_identity_no_change_does_not_mark_dirty():
    """Idempotent set must not trigger spurious redraws."""
    s = _state()
    s.consume_dirty()
    s.set_identity("K1ABC", "FN42", "miles", True)  # same as constructor
    assert s.consume_dirty() is False


def test_set_identity_change_marks_dirty():
    s = _state()
    s.consume_dirty()
    s.set_identity("VE3XYZ", "FN03", "miles", True)
    assert s.consume_dirty() is True
