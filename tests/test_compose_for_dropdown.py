"""Tests for the COMPOSE TO/FOR heard-dropdown UX and the dynamic
field cycle that inserts FOR between CMD and TEXT for MSG_TO.

State-layer tests only. Router behaviour is exercised in
test_router.py; the local-mailbox write for STORE is exercised in
test_app_compose_store.py.
"""

from __future__ import annotations

import pytest

from microjs8.protocol.types import HeardStation
from microjs8.ui.state import (
    ComposeCmd,
    Screen,
    UIState,
    _compose_focus_cycle,
)


def _state(callsign: str = "W5DMH", grid: str = "EN83") -> UIState:
    return UIState(callsign=callsign, grid=grid, tx_allowed=True)


def _hs(call: str, last_heard: float = 1700000000.0) -> HeardStation:
    return HeardStation(
        callsign=call, snr_db=0, grid=None, frequency_hz=1500.0,
        distance_mi=None, bearing_deg=None, last_heard=last_heard,
    )


# ── Focus cycle ────────────────────────────────────────────────────


def test_compose_focus_cycle_default_excludes_for():
    """FREE / MSG / etc. don't show the FOR field."""
    cycle = _compose_focus_cycle(ComposeCmd.FREE)
    assert "compose_for" not in cycle
    assert cycle == (
        "compose_to", "compose_cmd", "compose_text", "compose_send",
    )


def test_compose_focus_cycle_msg_to_inserts_for():
    """MSG TO adds the FOR field between CMD and TEXT."""
    cycle = _compose_focus_cycle(ComposeCmd.MSG_TO)
    assert cycle == (
        "compose_to", "compose_cmd", "compose_for",
        "compose_text", "compose_send",
    )


def test_compose_focus_cycle_store_no_for():
    """STORE uses TO and TEXT only (single-callsign local store)."""
    cycle = _compose_focus_cycle(ComposeCmd.STORE)
    assert "compose_for" not in cycle


# ── Tab cycle navigates FOR when MSG_TO selected ──────────────────


def test_tab_cycle_includes_for_when_msg_to_active():
    s = _state()
    s.set_screen(Screen.COMPOSE)
    # Cycle CMD to MSG_TO so the focus cycle includes FOR.
    while s.compose_cmd is not ComposeCmd.MSG_TO:
        s.compose_cycle_cmd(forward=True)
    # Start parked on TO (index 0). Tab through and collect focus.
    seen = [s.compose_focused_field()]
    for _ in range(5):  # full loop + 1
        s.cycle_focus()
        seen.append(s.compose_focused_field())
    assert seen == [
        "compose_to",
        "compose_cmd",
        "compose_for",
        "compose_text",
        "compose_send",
        "compose_to",  # wrapped
    ]


def test_cmd_change_away_from_msg_to_keeps_focus_in_bounds():
    """If focus is on FOR (index 2 in the 5-field MSG_TO cycle) and
    the operator cycles CMD to a non-MSG_TO command (4-field cycle),
    focus index 2 maps naturally to TEXT in the new cycle. The
    clamp only fires when the old focus index would land outside the
    new cycle's range (e.g., SEND at index 4 → clamp to CMD at 1)."""
    s = _state()
    s.set_screen(Screen.COMPOSE)
    while s.compose_cmd is not ComposeCmd.MSG_TO:
        s.compose_cycle_cmd(forward=True)
    # Park on FOR.
    s.cycle_focus()  # TO → CMD
    s.cycle_focus()  # CMD → FOR
    assert s.compose_focused_field() == "compose_for"
    # Cycle CMD off MSG_TO. Index 2 stays valid → focus lands on
    # TEXT in the new 4-field cycle.
    s.compose_cycle_cmd(forward=True)
    assert s.compose_cmd is not ComposeCmd.MSG_TO
    assert s.compose_focused_field() == "compose_text"


def test_cmd_change_away_from_msg_to_clamps_when_out_of_range():
    """When focus is on SEND (index 4 in MSG_TO's 5-field cycle) and
    CMD changes away from MSG_TO (cycle shrinks to 4 fields), index 4
    is out of range → clamp to CMD."""
    s = _state()
    s.set_screen(Screen.COMPOSE)
    while s.compose_cmd is not ComposeCmd.MSG_TO:
        s.compose_cycle_cmd(forward=True)
    # Park on SEND (index 4).
    for _ in range(4):
        s.cycle_focus()
    assert s.compose_focused_field() == "compose_send"
    # Cycle off MSG_TO — clamp fires.
    s.compose_cycle_cmd(forward=True)
    assert s.compose_focused_field() == "compose_cmd"


# ── TO/FOR heard-dropdown cycling ─────────────────────────────────


def test_to_cycle_next_picks_most_recent_heard():
    """First ↓ on empty TO lands on heard[0] (most recent)."""
    s = _state()
    s.set_heard((_hs("K1ABC"), _hs("KD8GIJ")))
    assert s.compose_to == ""
    assert s.compose_to_heard_index is None
    s.compose_to_cycle_heard_next()
    assert s.compose_to == "K1ABC"
    assert s.compose_to_heard_index == 0


def test_to_cycle_wraps_at_end():
    """↓ past the last heard wraps back to index 0."""
    s = _state()
    s.set_heard((_hs("K1ABC"), _hs("KD8GIJ")))
    s.compose_to_cycle_heard_next()
    s.compose_to_cycle_heard_next()
    assert s.compose_to == "KD8GIJ" and s.compose_to_heard_index == 1
    s.compose_to_cycle_heard_next()
    assert s.compose_to == "K1ABC" and s.compose_to_heard_index == 0


def test_to_cycle_prev_first_press_picks_last_heard():
    """First ↑ on empty TO lands on the LAST heard (wraps from None)."""
    s = _state()
    s.set_heard((_hs("K1ABC"), _hs("KD8GIJ")))
    s.compose_to_cycle_heard_prev()
    assert s.compose_to == "KD8GIJ"
    assert s.compose_to_heard_index == 1


def test_to_cycle_filters_self_from_dropdown():
    """Our own callsign never appears in the dropdown — operator
    can't compose a message to themselves via the dropdown."""
    s = _state(callsign="W5DMH")
    s.set_heard((_hs("W5DMH"), _hs("K1ABC"), _hs("KD8GIJ")))
    s.compose_to_cycle_heard_next()
    assert s.compose_to == "K1ABC"  # W5DMH skipped
    s.compose_to_cycle_heard_next()
    assert s.compose_to == "KD8GIJ"
    s.compose_to_cycle_heard_next()
    assert s.compose_to == "K1ABC"  # wrap, still skipping self


def test_to_cycle_no_op_when_heard_empty():
    s = _state()
    s.set_heard(())
    s.compose_to_cycle_heard_next()
    assert s.compose_to == ""
    assert s.compose_to_heard_index is None


def test_to_cycle_no_op_when_heard_is_only_self():
    """If the only heard station is us (radio loopback), dropdown
    is empty after filtering — ↓ is a no-op."""
    s = _state(callsign="W5DMH")
    s.set_heard((_hs("W5DMH"),))
    s.compose_to_cycle_heard_next()
    assert s.compose_to == ""


def test_typing_to_clears_heard_index():
    """Typing in TO after dropdown navigation switches to free-form
    mode (heard-index becomes None so renderer drops the age color)."""
    s = _state()
    s.set_heard((_hs("K1ABC"),))
    s.compose_to_cycle_heard_next()
    assert s.compose_to_heard_index == 0
    s.compose_set_to("K1ABCX")  # typed extension
    assert s.compose_to_heard_index is None
    assert s.compose_to == "K1ABCX"


def test_for_cycle_independent_from_to():
    """FOR has its own heard-index, independent of TO."""
    s = _state()
    s.set_heard((_hs("K1ABC"), _hs("KD8GIJ")))
    s.compose_to_cycle_heard_next()      # TO=K1ABC (idx 0)
    s.compose_for_cycle_heard_next()     # FOR=K1ABC (idx 0)
    assert s.compose_to == "K1ABC" and s.compose_to_heard_index == 0
    assert s.compose_for == "K1ABC" and s.compose_for_heard_index == 0
    s.compose_for_cycle_heard_next()     # FOR → KD8GIJ
    assert s.compose_for == "KD8GIJ" and s.compose_for_heard_index == 1
    # TO unchanged.
    assert s.compose_to == "K1ABC" and s.compose_to_heard_index == 0


def test_typing_for_clears_heard_index():
    s = _state()
    s.set_heard((_hs("K1ABC"),))
    s.compose_for_cycle_heard_next()
    assert s.compose_for_heard_index == 0
    s.compose_set_for("K1ABCX")
    assert s.compose_for_heard_index is None


# ── Prepopulate marks heard-index 0 ───────────────────────────────


def test_prepopulate_sets_heard_index_to_zero():
    """When entering COMPOSE, TO is prepopulated with most-recent
    heard. The heard-index is set so the renderer colours it by age."""
    s = _state()
    s.set_heard((_hs("K1ABC"),))
    s.compose_prepopulate_from_heard("K1ABC")
    assert s.compose_to == "K1ABC"
    assert s.compose_to_heard_index == 0


def test_prepopulate_skips_when_to_nonempty():
    """Operator's in-progress TO must not be clobbered."""
    s = _state()
    s.compose_set_to("KD8GIJ")
    s.set_heard((_hs("K1ABC"),))
    s.compose_prepopulate_from_heard("K1ABC")
    assert s.compose_to == "KD8GIJ"


# ── compose_clear resets all new fields ────────────────────────────


def test_compose_clear_resets_for_and_heard_indices():
    s = _state()
    s.set_heard((_hs("K1ABC"),))
    s.compose_to_cycle_heard_next()
    s.compose_for_cycle_heard_next()
    s.compose_set_text("hi")
    s.compose_clear()
    assert s.compose_to == ""
    assert s.compose_for == ""
    assert s.compose_text == ""
    assert s.compose_to_heard_index is None
    assert s.compose_for_heard_index is None


# ── Snapshot exposes new fields ────────────────────────────────────


def test_snapshot_includes_compose_for_and_heard_indices():
    s = _state()
    s.set_heard((_hs("K1ABC"),))
    s.compose_to_cycle_heard_next()
    s.compose_set_for("KD8GIJ")
    snap = s.snapshot()
    assert snap.compose_to == "K1ABC"
    assert snap.compose_to_heard_index == 0
    assert snap.compose_for == "KD8GIJ"
    assert snap.compose_for_heard_index is None  # typed, not picked
