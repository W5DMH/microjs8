"""Tests for microjs8.ui.state — ring navigation and shutdown bookkeeping."""

from __future__ import annotations

import pytest

from microjs8.ui.state import RING, Screen, UIState


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


# ── Phase 19 v0.0.10: Directed / Heard scroll offsets ────────────────


def test_directed_scroll_starts_at_zero():
    from microjs8.ui.state import UIState
    s = _state()
    assert s.snapshot().directed_scroll_offset == 0


def test_directed_scroll_down_advances_offset():
    """v0.0.10: ↓ on DIRECTED advances offset toward older entries."""
    from microjs8.ui.state import UIState
    from microjs8.activity import DirectedActivityEntry, Direction
    s = _state()
    # 5 entries in the log
    entries = tuple(
        DirectedActivityEntry(
            at_unix=1700000000.0 + i,
            direction=Direction.IN,
            other_call=f"N{i}AAA",
            verb="HEARTBEAT",
            body=f"EN8{i}",
            snr_db=-10,
            freq_hz=1500.0,
        ) for i in range(5)
    )
    s.set_directed_log(entries)
    # set_directed_log RESETS to 0 (new entries arrived)
    assert s.snapshot().directed_scroll_offset == 0

    s.directed_scroll_down()
    assert s.snapshot().directed_scroll_offset == 1
    s.directed_scroll_down()
    assert s.snapshot().directed_scroll_offset == 2


def test_directed_scroll_down_caps_at_last_entry():
    """v0.0.10: can't scroll past the oldest entry."""
    from microjs8.ui.state import UIState
    from microjs8.activity import DirectedActivityEntry, Direction
    s = _state()
    entries = tuple(
        DirectedActivityEntry(
            at_unix=1700000000.0 + i,
            direction=Direction.IN,
            other_call=f"N{i}AAA",
            verb="X", body="", snr_db=-10, freq_hz=1500.0,
        ) for i in range(3)
    )
    s.set_directed_log(entries)
    for _ in range(10):
        s.directed_scroll_down()
    # Capped at len-1 = 2
    assert s.snapshot().directed_scroll_offset == 2


def test_directed_scroll_up_at_top_is_noop():
    """v0.0.10: ↑ at offset 0 doesn't go negative."""
    from microjs8.ui.state import UIState
    s = _state()
    s.directed_scroll_up()
    assert s.snapshot().directed_scroll_offset == 0


def test_new_directed_entry_resets_scroll_to_top():
    """v0.0.10: when a new entry arrives, the operator's scroll
    position resets to 0 so they see the newest entry. This is the
    chat-style semantic — new traffic interrupts review."""
    from microjs8.ui.state import UIState
    from microjs8.activity import DirectedActivityEntry, Direction
    s = _state()
    entries = tuple(
        DirectedActivityEntry(
            at_unix=1700000000.0 + i, direction=Direction.IN,
            other_call=f"X{i}", verb="V", body="", snr_db=-5, freq_hz=0.0,
        ) for i in range(3)
    )
    s.set_directed_log(entries)
    s.directed_scroll_down()
    s.directed_scroll_down()
    assert s.snapshot().directed_scroll_offset == 2

    # Now a new entry arrives → expand to 4 entries
    new_entries = entries + (
        DirectedActivityEntry(
            at_unix=1700000003.0, direction=Direction.IN,
            other_call="NEW", verb="V", body="", snr_db=0, freq_hz=0.0,
        ),
    )
    s.set_directed_log(new_entries)
    # Reset to 0 — operator now sees the new entry at top.
    assert s.snapshot().directed_scroll_offset == 0


def test_directed_scroll_offset_clamped_when_log_shrinks():
    """v0.0.10: if entries vanish (clear/rollover), offset clamps to
    the new max rather than dangling past the end."""
    from microjs8.ui.state import UIState
    from microjs8.activity import DirectedActivityEntry, Direction
    s = _state()
    big = tuple(
        DirectedActivityEntry(
            at_unix=1700000000.0 + i, direction=Direction.IN,
            other_call=f"X{i}", verb="V", body="", snr_db=0, freq_hz=0.0,
        ) for i in range(10)
    )
    s.set_directed_log(big)
    for _ in range(8):
        s.directed_scroll_down()
    assert s.snapshot().directed_scroll_offset == 8

    # Now drop to 3 entries
    small = big[:3]
    s.set_directed_log(small)
    # Clamped to 2 (len-1)
    assert s.snapshot().directed_scroll_offset == 2


def test_heard_scroll_starts_at_zero_and_clamps():
    """v0.0.10: heard scroll offset behavior — starts at 0, advances
    with ↓, clamps when list shrinks, does NOT reset on new heard
    (contrast with directed which DOES reset)."""
    from microjs8.ui.state import UIState
    from microjs8.protocol.types import HeardStation
    s = _state()
    heard = tuple(
        HeardStation(
            callsign=f"N{i}A",
            snr_db=-10,
            grid="EN83",
            frequency_hz=1500.0,
            distance_mi=None, bearing_deg=None,
            last_heard=1700000000.0 + i,
        ) for i in range(8)
    )
    s.set_heard(heard)
    assert s.snapshot().heard_scroll_offset == 0
    s.heard_scroll_down()
    s.heard_scroll_down()
    s.heard_scroll_down()
    assert s.snapshot().heard_scroll_offset == 3

    # Add a new heard station — offset should NOT reset (operator
    # may be reviewing older stations; don't yank them back to top)
    heard_plus = (
        HeardStation(
            callsign="NEW", snr_db=-5, grid="EN84",
            frequency_hz=1500.0, distance_mi=None, bearing_deg=None,
            last_heard=1700000099.0,
        ),
    ) + heard
    s.set_heard(heard_plus)
    assert s.snapshot().heard_scroll_offset == 3, (
        "heard scroll must NOT reset on new arrivals"
    )

    # Heard list shrinks (callsign-change clear or HEARD_CAP rollover)
    s.set_heard(heard_plus[:2])
    # Clamped to 1 (len-1)
    assert s.snapshot().heard_scroll_offset == 1


def test_heard_scroll_up_at_top_is_noop():
    from microjs8.ui.state import UIState
    s = _state()
    s.heard_scroll_up()
    assert s.snapshot().heard_scroll_offset == 0


def test_directed_scroll_reset_jumps_to_top():
    """v0.0.10: directed_scroll_reset() forces offset back to 0
    regardless of current position (used for explicit navigation)."""
    from microjs8.ui.state import UIState
    from microjs8.activity import DirectedActivityEntry, Direction
    s = _state()
    entries = tuple(
        DirectedActivityEntry(
            at_unix=1700000000.0 + i, direction=Direction.IN,
            other_call=f"X{i}", verb="V", body="", snr_db=0, freq_hz=0.0,
        ) for i in range(5)
    )
    s.set_directed_log(entries)
    s.directed_scroll_down()
    s.directed_scroll_down()
    s.directed_scroll_reset()
    assert s.snapshot().directed_scroll_offset == 0
