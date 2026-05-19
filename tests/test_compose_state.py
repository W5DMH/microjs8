"""Tests for COMPOSE screen state, wire-format builder, and pre-populate.

Covers the state-layer plumbing (UIState methods + ComposeCmd cycle +
build_compose_wire) and the cross-screen pre-populate hook from Heard.
Router-level tests live in test_router.py; app-level callback tests
live in test_app_compose_send.py.
"""
from __future__ import annotations

import pytest

from microjs8.protocol.types import HeardStation
from microjs8.ui.state import (
    COMPOSE_CMD_ORDER,
    ComposeCmd,
    Screen,
    UIState,
    build_compose_wire,
)


# ── build_compose_wire ────────────────────────────────────────────────


def test_build_wire_free_directed_simple():
    """FREE cmd produces 'TO TEXT' with no verb. This is the common
    chat case — operator types a callsign and a body, message goes
    out as a directed free-form."""
    assert build_compose_wire("k1abc", ComposeCmd.FREE, "hello", "EN83") == "K1ABC hello"


def test_build_wire_msg_inserts_verb():
    """MSG cmd produces 'TO MSG TEXT' so the recipient stores it as
    a buffered/checksummed mail item."""
    assert build_compose_wire("K1ABC", ComposeCmd.MSG, "hello dave", "EN83") == "K1ABC MSG hello dave"


def test_build_wire_store_returns_none_no_wire():
    """STORE is a LOCAL action — no wire is built. The caller routes
    this to the mailbox-write path instead of enqueueing."""
    assert build_compose_wire("K1ABC", ComposeCmd.STORE, "for K2DEF", "EN83") is None
    # Empty TEXT or TO: still None (no wire either way).
    assert build_compose_wire("K1ABC", ComposeCmd.STORE, "", "EN83") is None
    assert build_compose_wire("", ComposeCmd.STORE, "body", "EN83") is None


def test_build_wire_msg_to_with_for_call():
    """MSG TO requires the FOR callsign — produces 'TO MSG TO:FOR TEXT'."""
    wire = build_compose_wire(
        "K1ABC", ComposeCmd.MSG_TO, "hello forward me", "EN83",
        for_call="KD8GIJ",
    )
    assert wire == "K1ABC MSG TO:KD8GIJ hello forward me"


def test_build_wire_msg_to_requires_for_call():
    """MSG TO with empty FOR is not a sendable wire."""
    assert build_compose_wire(
        "K1ABC", ComposeCmd.MSG_TO, "body", "EN83", for_call=""
    ) is None
    assert build_compose_wire(
        "K1ABC", ComposeCmd.MSG_TO, "body", "EN83", for_call="   "
    ) is None


def test_build_wire_msg_to_rejects_for_equal_to():
    """A relay holding for itself isn't meaningful — operator typo."""
    assert build_compose_wire(
        "K1ABC", ComposeCmd.MSG_TO, "body", "EN83", for_call="K1ABC"
    ) is None


def test_build_wire_query_msgs_is_verb_only():
    """QUERY MSGS is verb-only — no TEXT body required. Replaces the
    old bare QUERY command that needed the operator to type 'MSGS'."""
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSGS, "", "EN83") == "K1ABC QUERY MSGS"
    # TEXT is ignored for QUERY MSGS — verb-only protocol exchange.
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSGS, "ignored", "EN83") == "K1ABC QUERY MSGS"


def test_build_wire_verb_only_commands():
    """AGN?/SNR?/GRID? are verb-only — TEXT is ignored, no body on wire."""
    assert build_compose_wire("K1ABC", ComposeCmd.AGN_Q, "", "EN83") == "K1ABC AGN?"
    assert build_compose_wire("K1ABC", ComposeCmd.SNR_Q, "", "EN83") == "K1ABC SNR?"
    # GRID? is the question form (JS8Call requires the ? — bare GRID
    # is ignored). The reply form is MYLOC, which emits 'GRID <grid>'.
    assert build_compose_wire("K1ABC", ComposeCmd.GRID_Q, "", "EN83") == "K1ABC GRID?"
    # Even if the operator typed something, verb-only commands ignore
    # TEXT — keeps the protocol semantics clean.
    assert build_compose_wire("K1ABC", ComposeCmd.AGN_Q, "ignored", "EN83") == "K1ABC AGN?"


def test_build_wire_myloc_substitutes_grid_from_config():
    """MYLOC is UI-only — wire form is 'TO GRID <my_grid>'. The
    operator doesn't have to type their grid; it comes from station
    config so it's always current."""
    assert build_compose_wire("K1ABC", ComposeCmd.MYLOC, "", "EN83") == "K1ABC GRID EN83"
    assert build_compose_wire("K1ABC", ComposeCmd.MYLOC, "ignored", "FN42") == "K1ABC GRID FN42"


def test_build_wire_myloc_rejects_missing_grid():
    """If the station hasn't been configured with a grid, MYLOC can't
    fire — we shouldn't broadcast a half-formed 'GRID ' with no
    locator. Returns None so the caller knows to surface an error."""
    assert build_compose_wire("K1ABC", ComposeCmd.MYLOC, "", "") is None


def test_build_wire_rejects_empty_to():
    """TO is mandatory for every CMD — there's no broadcast/everyone
    pattern in this layer; the operator must explicitly type @ALLCALL
    or a callsign."""
    assert build_compose_wire("", ComposeCmd.FREE, "hi", "EN83") is None
    assert build_compose_wire("   ", ComposeCmd.MSG, "hi", "EN83") is None


def test_build_wire_uppercases_callsign():
    """Callsigns go on the air uppercased (JS8 protocol convention).
    The operator can type lowercase for convenience."""
    assert build_compose_wire("k1abc", ComposeCmd.FREE, "hi", "EN83") == "K1ABC hi"


def test_build_wire_strips_to_whitespace():
    """Leading/trailing whitespace on TO would corrupt the wire
    format — strip it. Internal whitespace in TEXT is preserved
    because the protocol layer handles it correctly."""
    assert build_compose_wire("  K1ABC  ", ComposeCmd.FREE, "hello world", "EN83") == "K1ABC hello world"


def test_build_wire_rejects_empty_body_for_body_cmds():
    """FREE/MSG/STORE/QUERY all require a body — empty TEXT means
    nothing meaningful would be transmitted, so we reject."""
    for cmd in (ComposeCmd.FREE, ComposeCmd.MSG, ComposeCmd.STORE):
        assert build_compose_wire("K1ABC", cmd, "", "EN83") is None, cmd
        assert build_compose_wire("K1ABC", cmd, "   ", "EN83") is None, cmd


# ── UIState compose methods ───────────────────────────────────────────


def _state(screen=Screen.COMPOSE) -> UIState:
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_screen(screen)
    return s


def test_compose_state_defaults():
    """Fresh UIState starts with empty TO/TEXT and FREE cmd."""
    s = _state()
    snap = s.snapshot()
    assert snap.compose_to == ""
    assert snap.compose_cmd is ComposeCmd.FREE
    assert snap.compose_text == ""
    # Default focused field on COMPOSE is the first focusable (TO).
    assert snap.compose_focused_field == "compose_to"


def test_compose_set_to_and_text():
    s = _state()
    s.compose_set_to("K1ABC")
    s.compose_set_text("hello there")
    snap = s.snapshot()
    assert snap.compose_to == "K1ABC"
    assert snap.compose_text == "hello there"


def test_compose_cycle_cmd_forward_wraps():
    """↓ cycles forward through COMPOSE_CMD_ORDER and wraps to first
    after the last."""
    s = _state()
    # Start at FREE (index 0). Cycle forward len(ORDER) times → back to FREE.
    for _ in range(len(COMPOSE_CMD_ORDER)):
        s.compose_cycle_cmd(forward=True)
    assert s.compose_cmd is ComposeCmd.FREE


def test_compose_cycle_cmd_backward_wraps():
    """↑ cycles backward and wraps from FREE to the last enum."""
    s = _state()
    s.compose_cycle_cmd(forward=False)
    # FREE (index 0) − 1 wraps to MYLOC (last in COMPOSE_CMD_ORDER).
    assert s.compose_cmd is COMPOSE_CMD_ORDER[-1]


def test_compose_cycle_cmd_one_step():
    s = _state()
    s.compose_cycle_cmd(forward=True)
    assert s.compose_cmd is ComposeCmd.MSG


def test_compose_focus_cycles_through_four_fields():
    """Tab cycles TO → CMD → TEXT → SEND → TO."""
    s = _state()
    expected = ("compose_to", "compose_cmd", "compose_text", "compose_send", "compose_to")
    assert s.compose_focused_field() == expected[0]
    for nxt in expected[1:]:
        s.cycle_focus()
        assert s.compose_focused_field() == nxt


def test_compose_clear_resets_all():
    """compose_clear blanks fields, restores FREE, returns focus to TO."""
    s = _state()
    s.compose_set_to("K1ABC")
    s.compose_set_text("hi")
    s.compose_cycle_cmd(forward=True)
    s.cycle_focus()
    s.cycle_focus()  # focus is now on TEXT
    s.compose_clear()
    snap = s.snapshot()
    assert snap.compose_to == ""
    assert snap.compose_text == ""
    assert snap.compose_cmd is ComposeCmd.FREE
    assert snap.compose_focused_field == "compose_to"


def test_compose_focused_field_is_none_off_screen():
    """When not on COMPOSE, focused-field reads as None — the router
    uses this to decide whether to dispatch compose-specific keys."""
    s = _state(screen=Screen.HOME)
    assert s.compose_focused_field() is None
    snap = s.snapshot()
    assert snap.compose_focused_field is None


# ── compose_prepopulate_from_heard ────────────────────────────────────


def test_prepopulate_fills_empty_to():
    """Pre-populate fills TO when it's currently empty."""
    s = _state()
    s.compose_prepopulate_from_heard("K1ABC")
    assert s.compose_to == "K1ABC"


def test_prepopulate_uppercases():
    s = _state()
    s.compose_prepopulate_from_heard("k1abc")
    assert s.compose_to == "K1ABC"


def test_prepopulate_does_not_overwrite_typed_value():
    """If the operator has already typed a callsign in TO, pre-
    populate is a no-op — we don't clobber their work."""
    s = _state()
    s.compose_set_to("K2DEF")
    s.compose_prepopulate_from_heard("K1ABC")
    assert s.compose_to == "K2DEF"  # unchanged


def test_prepopulate_with_none_is_noop():
    s = _state()
    s.compose_prepopulate_from_heard(None)
    assert s.compose_to == ""


def test_prepopulate_with_empty_string_is_noop():
    s = _state()
    s.compose_prepopulate_from_heard("")
    assert s.compose_to == ""


# ── Cross-screen hook on advance_ring / retreat_ring ──────────────────


def _heard(*calls: str) -> tuple[HeardStation, ...]:
    import time
    now = time.time()
    return tuple(
        HeardStation(
            callsign=c, snr_db=-10, frequency_hz=7078000.0, last_heard=now,
            grid=None, distance_mi=None, bearing_deg=None,
        )
        for c in calls
    )


def test_entering_compose_via_advance_ring_prepopulates_to():
    """Operator on INBOX (the screen before COMPOSE in the ring) hits
    →. Pre-populate hook fires, TO gets the most-recently-heard
    callsign."""
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_heard(_heard("K1ABC", "K2DEF"))
    s.set_screen(Screen.INBOX)
    s.advance_ring()
    assert s.snapshot().screen is Screen.COMPOSE
    assert s.compose_to == "K1ABC"


def test_entering_compose_via_retreat_ring_prepopulates_to():
    """Operator on ALLCALL (the screen after COMPOSE in the ring)
    hits ←. Same pre-populate."""
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_heard(_heard("K1ABC"))
    s.set_screen(Screen.ALLCALL)
    s.retreat_ring()
    assert s.snapshot().screen is Screen.COMPOSE
    assert s.compose_to == "K1ABC"


def test_entering_compose_with_empty_heard_leaves_to_blank():
    """No heard rows → no pre-populate. Operator types from scratch."""
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_screen(Screen.INBOX)
    s.advance_ring()
    assert s.snapshot().screen is Screen.COMPOSE
    assert s.compose_to == ""


def test_returning_to_compose_does_not_clobber_partial_typing():
    """Operator typed half a callsign in TO, navigated away, came
    back. Pre-populate is non-destructive — doesn't overwrite."""
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_heard(_heard("K1ABC"))
    s.set_screen(Screen.COMPOSE)
    s.compose_set_to("K9XY")  # partial type
    s.advance_ring()  # → ALLCALL
    s.retreat_ring()  # ← COMPOSE
    assert s.compose_to == "K9XY"  # preserved


# ── Self-decode filtering on Heard pre-populate ───────────────────────


def test_prepopulate_skips_self_in_heard():
    """If our own callsign is in the Heard list (e.g. radio loopback
    decoded our own TX), pre-populate skips us and uses the next
    non-self entry. Otherwise the operator opening Compose right
    after a TX would see their own call in TO and accidentally try
    to send to themselves."""
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_heard(_heard("W5DMH", "K1ABC"))  # self at top, K1ABC second
    s.set_screen(Screen.INBOX)
    s.advance_ring()
    assert s.snapshot().screen is Screen.COMPOSE
    assert s.compose_to == "K1ABC"  # self skipped


def test_prepopulate_skips_self_case_insensitive():
    """Callsign comparison is case-insensitive — heard list might
    contain mixed-case decodes."""
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_heard(_heard("w5dmh", "K1ABC"))
    s.set_screen(Screen.INBOX)
    s.advance_ring()
    assert s.compose_to == "K1ABC"


def test_prepopulate_with_only_self_in_heard_leaves_to_blank():
    """If the heard list contains ONLY us, pre-populate finds no
    valid entry — TO stays blank, operator types from scratch."""
    s = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    s.set_heard(_heard("W5DMH"))
    s.set_screen(Screen.INBOX)
    s.advance_ring()
    assert s.compose_to == ""


# ── Reject TO == self (gfsk8 AUTO_REMOVE_MYCALL silently strips) ──────


def test_build_wire_rejects_to_equal_my_call():
    """If TO == our own callsign, gfsk8 silently strips the leading
    callsign (Varicode.cpp::AUTO_REMOVE_MYCALL), producing a frame
    with no directed-message envelope. Build returns None to prevent
    this malformed transmission."""
    assert build_compose_wire(
        to="W5DMH", cmd=ComposeCmd.MSG, text="hi",
        my_grid="EN83", my_call="W5DMH",
    ) is None


def test_build_wire_rejects_to_self_case_insensitive():
    """The case-insensitive comparison covers operator typos like
    typing their own call in lowercase."""
    assert build_compose_wire(
        to="w5dmh", cmd=ComposeCmd.FREE, text="hi",
        my_grid="EN83", my_call="W5DMH",
    ) is None


def test_build_wire_allows_other_call_when_my_call_set():
    """Sanity: rejection is specifically for the self case, not a
    blanket disable."""
    assert build_compose_wire(
        to="K1ABC", cmd=ComposeCmd.MSG, text="hi",
        my_grid="EN83", my_call="W5DMH",
    ) == "K1ABC MSG hi"


def test_build_wire_my_call_unset_does_not_block():
    """When my_call is empty (default), the self-check is skipped —
    callers that don't supply identity (e.g. legacy tests) keep the
    old behavior. The router's runtime path always passes my_call so
    this lenient default only affects test code."""
    # No my_call → wire builds even though TO matches what would-be self
    assert build_compose_wire(
        to="W5DMH", cmd=ComposeCmd.MSG, text="hi", my_grid="EN83",
    ) == "W5DMH MSG hi"


# ── QUERY MSG <id> (fetch buffered message by mailbox id) ────────────


def test_build_wire_query_msg_with_numeric_id():
    """QUERY MSG <id> emits 'TO QUERY MSG <id>' on the wire so the
    operator can fetch a specific buffered message from the TO
    station's mailbox."""
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSG, "1", "EN83") == "K1ABC QUERY MSG 1"
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSG, "42", "EN83") == "K1ABC QUERY MSG 42"


def test_build_wire_query_msg_strips_text_whitespace():
    """Operator-typed leading/trailing spaces in the ID field don't
    block the wire build — they're stripped by the boundary normaliser
    above ``isdigit()`` check."""
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSG, " 7 ", "EN83") == "K1ABC QUERY MSG 7"


def test_build_wire_query_msg_rejects_empty_id():
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSG, "", "EN83") is None
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSG, "   ", "EN83") is None


@pytest.mark.parametrize("bad", ["abc", "1.5", "-1", "one", "0", "1a", "+7"])
def test_build_wire_query_msg_rejects_non_positive_integer(bad):
    """Mailbox IDs are positive integers (SQLite AUTOINCREMENT starts
    at 1). Reject zero, negatives, decimals, and any non-digit
    content — JS8Call's wire contract requires a clean integer
    after the verb."""
    assert build_compose_wire("K1ABC", ComposeCmd.QUERY_MSG, bad, "EN83") is None


def test_build_wire_query_msg_in_dropdown_order():
    """QUERY MSG sits between QUERY MSGS and MYLOC in the cycle —
    related QUERY commands grouped together for easy navigation."""
    from microjs8.ui.state import COMPOSE_CMD_ORDER
    assert ComposeCmd.QUERY_MSG in COMPOSE_CMD_ORDER
    i_msgs = COMPOSE_CMD_ORDER.index(ComposeCmd.QUERY_MSGS)
    i_msg = COMPOSE_CMD_ORDER.index(ComposeCmd.QUERY_MSG)
    i_myloc = COMPOSE_CMD_ORDER.index(ComposeCmd.MYLOC)
    assert i_msgs < i_msg < i_myloc
