"""Tests for microjs8.input.router.

The router is pure functional logic — no I/O — so we drive it directly
with synthetic KeyEvents and assert on UIState afterward.
"""

from __future__ import annotations

import pytest

from microjs8.input.events import Key, KeyEvent
from microjs8.input.router import InputRouter
from microjs8.ui.state import RING, Screen, UIState


# ── Fixtures ─────────────────────────────────────────────────────────


def _state(*, configured: bool = True, screen: Screen = Screen.HOME) -> UIState:
    if configured:
        s = UIState("K1ABC", "FN42", True, "miles")
    else:
        s = UIState("N0CALL", "", False, "miles")
    s.set_screen(screen)
    return s


class _SaveCapture:
    """Capturing save callback for assertions."""

    def __init__(self, accept: bool = True) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.accept = accept

    def __call__(self, callsign: str, grid: str, units: str) -> bool:
        self.calls.append((callsign, grid, units))
        return self.accept


class _BypassCapture:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _router(state: UIState, *, save_accept: bool = True):
    save = _SaveCapture(accept=save_accept)
    bypass = _BypassCapture()
    return InputRouter(state, save_config=save, emergency_bypass=bypass), save, bypass


# ── Ring navigation ─────────────────────────────────────────────────


def test_arrow_right_advances_when_configured():
    s = _state()
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.RIGHT))
    assert s.snapshot().screen is Screen.HEARD


def test_arrow_left_retreats_when_configured():
    s = _state()
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.LEFT))
    assert s.snapshot().screen is RING[-1]


def test_ring_locked_when_unconfigured():
    """Per spec, the operator cannot navigate away from Setup until
    Call+Grid are valid (or emergency bypass is activated)."""
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.RIGHT))
    assert s.snapshot().screen is Screen.SETUP
    r.handle(KeyEvent(key=Key.LEFT))
    assert s.snapshot().screen is Screen.SETUP


def test_ring_unlocked_after_emergency_override():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    s.trigger_emergency_override()
    r.handle(KeyEvent(key=Key.RIGHT))
    # We were on EMERGENCY (set by trigger_emergency_override). Ring
    # nav should advance from there.
    assert s.snapshot().screen is not Screen.EMERGENCY


# ── Global hotkeys ──────────────────────────────────────────────────


def test_ctrl_s_jumps_to_setup_from_anywhere():
    s = _state(screen=Screen.HEARD)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.CTRL_S))
    assert s.snapshot().screen is Screen.SETUP


def test_ctrl_q_jumps_to_allcall_when_configured():
    s = _state(screen=Screen.HOME)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.CTRL_Q))
    assert s.snapshot().screen is Screen.ALLCALL


def test_ctrl_q_disabled_when_unconfigured():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.CTRL_Q))
    # Stays on Setup since the ring is locked.
    assert s.snapshot().screen is Screen.SETUP


def test_ctrl_s_works_when_unconfigured():
    """Ctrl-S must work even on unconfigured station — it goes to where
    the operator needs to be anyway."""
    s = _state(configured=False, screen=Screen.HOME)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.CTRL_S))
    assert s.snapshot().screen is Screen.SETUP


# ── Tab focus on Setup ──────────────────────────────────────────────


def test_tab_cycles_setup_focus():
    s = _state(screen=Screen.SETUP)
    r, _, _ = _router(s)
    # Initially focus is on first field (callsign).
    assert s.focused_field_name() == "callsign"
    r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "grid"
    r.handle(KeyEvent(key=Key.TAB))
    # Phase 10: 'groups' (JS8Call group memberships) sits between
    # grid and units in the focus cycle. Empty by default.
    assert s.focused_field_name() == "groups"
    r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "units"
    r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "freq_hz"
    r.handle(KeyEvent(key=Key.TAB))
    # Step 6+: Radio profile cycler sits between freq_hz and the
    # emergency-bypass button. Operator presses Enter to cycle through
    # the registry and the daemon restarts to apply.
    assert s.focused_field_name() == "radio"
    r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "emergency_bypass"
    r.handle(KeyEvent(key=Key.TAB))
    # Wraps back
    assert s.focused_field_name() == "callsign"


# ── Edit mode ───────────────────────────────────────────────────────


def test_enter_on_callsign_field_starts_edit():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.ENTER))
    snap = s.snapshot()
    assert snap.editing_field == "callsign"
    # Starting with N0CALL → buffer is empty.
    assert snap.edit_buffer == ""


def test_typing_chars_appends_to_buffer():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.ENTER))  # begin edit
    for ch in "K8XYZ":
        r.handle(KeyEvent(char=ch))
    assert s.snapshot().edit_buffer == "K8XYZ"


def test_typing_directly_auto_enters_edit_mode():
    """Pressing a printable key on a focused field starts edit mode
    and replaces the field. This matches the mental model 'Tab to
    field, type to overwrite' that operators expect."""
    s = _state(configured=True, screen=Screen.SETUP)  # K1ABC / FN42
    r, _, _ = _router(s)
    # Focus is initially on callsign; just type — no Enter first.
    r.handle(KeyEvent(char="W"))
    snap = s.snapshot()
    assert snap.editing_field == "callsign"
    # Buffer was cleared and replaced with the typed char (overwrites).
    assert snap.edit_buffer == "W"
    # Continue typing accumulates as normal.
    for ch in "1AW":
        r.handle(KeyEvent(char=ch))
    assert s.snapshot().edit_buffer == "W1AW"


def test_typing_directly_does_not_auto_edit_emergency_button():
    """Type-to-edit must not fire on the emergency_bypass button —
    that's a focused element, but it's not editable."""
    s = _state(configured=True, screen=Screen.SETUP)
    r, _, _ = _router(s)
    # Tab to emergency_bypass: callsign -> grid -> units -> freq_hz
    # callsign -> grid -> groups -> units -> freq_hz -> radio -> emergency_bypass (6 tabs).
    for _ in range(6):
        r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "emergency_bypass"
    r.handle(KeyEvent(char="x"))
    # No edit started.
    assert s.snapshot().editing_field is None


def test_backspace_deletes():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.ENTER))
    for ch in "K8XYZ":
        r.handle(KeyEvent(char=ch))
    r.handle(KeyEvent(key=Key.BACKSPACE))
    assert s.snapshot().edit_buffer == "K8XY"


def test_esc_cancels_edit():
    s = _state(configured=True, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.ENTER))
    # Buffer pre-fills with "K1ABC" since station IS configured.
    assert s.snapshot().edit_buffer == "K1ABC"
    r.handle(KeyEvent(char="W"))
    r.handle(KeyEvent(key=Key.ESC))
    snap = s.snapshot()
    assert snap.editing_field is None
    # Identity unchanged.
    assert snap.callsign == "K1ABC"


def test_enter_commits_valid_callsign():
    s = _state(configured=False, screen=Screen.SETUP)
    r, save, _ = _router(s)
    r.handle(KeyEvent(key=Key.ENTER))
    for ch in "K8XYZ":
        r.handle(KeyEvent(char=ch))
    r.handle(KeyEvent(key=Key.ENTER))
    # save was called with new callsign + existing grid + existing units
    assert save.calls == [("K8XYZ", "", "miles")]
    assert s.snapshot().editing_field is None


def test_enter_marks_invalid_when_save_rejects():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s, save_accept=False)
    r.handle(KeyEvent(key=Key.ENTER))
    for ch in "BAD!!":
        r.handle(KeyEvent(char=ch))
    r.handle(KeyEvent(key=Key.ENTER))
    snap = s.snapshot()
    # Still in edit mode, flagged invalid.
    assert snap.editing_field == "callsign"
    assert snap.edit_invalid is True


def test_global_hotkey_ignored_during_edit():
    """Ctrl-S during edit must not break out — operator might be
    typing 'K1S' as part of their callsign."""
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.ENTER))
    r.handle(KeyEvent(key=Key.CTRL_S))
    # Edit is still active; Ctrl-S didn't fire a screen jump.
    assert s.snapshot().editing_field == "callsign"
    assert s.snapshot().screen is Screen.SETUP


def test_max_length_caps_callsign():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, _ = _router(s)
    r.handle(KeyEvent(key=Key.ENTER))
    for ch in "K8XYZ12345EXTRA":  # 15 chars; 10 is max
        r.handle(KeyEvent(char=ch))
    assert len(s.snapshot().edit_buffer) == 10


# ── Emergency bypass ────────────────────────────────────────────────


def test_emergency_bypass_button_invokes_callback():
    s = _state(configured=False, screen=Screen.SETUP)
    r, _, bypass = _router(s)
    # Tab five times: callsign -> grid -> units -> freq_hz -> radio
    # callsign -> grid -> groups -> units -> freq_hz -> radio -> emergency_bypass.
    for _ in range(6):
        r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "emergency_bypass"
    r.handle(KeyEvent(key=Key.ENTER))
    assert bypass.calls == 1


# ── Exception safety ────────────────────────────────────────────────


def test_router_swallows_handler_exceptions():
    """A buggy callback must not blow up the router."""
    s = _state(configured=False, screen=Screen.SETUP)

    def boom(*args, **kw):
        raise RuntimeError("synthetic")

    r = InputRouter(s, save_config=boom, emergency_bypass=lambda: None)
    r.handle(KeyEvent(key=Key.ENTER))
    for ch in "K1ABC":
        r.handle(KeyEvent(char=ch))
    # Despite the buggy save, this should not raise.
    r.handle(KeyEvent(key=Key.ENTER))


# ── Radio profile cycle ─────────────────────────────────────────────


class _CycleRadioCapture:
    """Recordable cycle_radio callback for tests."""

    def __init__(self, accept: bool = True) -> None:
        self.calls = 0
        self.accept = accept

    def __call__(self) -> bool:
        self.calls += 1
        return self.accept


def test_enter_on_radio_invokes_cycle_callback():
    """Enter on the Radio row should invoke cycle_radio, NOT enter
    text-edit mode. The callback is responsible for picking the next
    id, saving, and restarting."""
    s = _state(configured=True, screen=Screen.SETUP)
    cycle = _CycleRadioCapture()
    r = InputRouter(
        s,
        save_config=_SaveCapture(),
        emergency_bypass=_BypassCapture(),
        cycle_radio=cycle,
    )
    # Tab to radio: callsign -> grid -> groups -> units -> freq_hz -> radio (5 tabs).
    for _ in range(5):
        r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "radio"

    r.handle(KeyEvent(key=Key.ENTER))
    assert cycle.calls == 1
    # Crucially, no text edit was started — the operator is not
    # going to type a radio name.
    assert s.snapshot().editing_field is None


def test_enter_on_radio_without_callback_is_safe():
    """If cycle_radio is None (e.g., headless test setup that didn't
    wire it), Enter on the radio row should be a no-op rather than
    crashing."""
    s = _state(configured=True, screen=Screen.SETUP)
    r = InputRouter(
        s,
        save_config=_SaveCapture(),
        emergency_bypass=_BypassCapture(),
        cycle_radio=None,  # explicitly no cycle handler
    )
    # Phase 10: 5 tabs to reach radio (callsign → grid → groups →
    # units → freq_hz → radio).
    for _ in range(5):
        r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "radio"

    # Must not raise.
    r.handle(KeyEvent(key=Key.ENTER))
    assert s.snapshot().editing_field is None


def test_typing_directly_does_not_auto_edit_radio():
    """Like emergency_bypass, the radio row is focused but NOT a
    text-edit field. Typing while focused on radio must not start
    an edit (which would corrupt the buffer with a key the operator
    intended for a screen-change shortcut, etc.)."""
    s = _state(configured=True, screen=Screen.SETUP)
    cycle = _CycleRadioCapture()
    r = InputRouter(
        s,
        save_config=_SaveCapture(),
        emergency_bypass=_BypassCapture(),
        cycle_radio=cycle,
    )
    # Phase 10: 5 tabs to reach radio (callsign → grid → groups →
    # units → freq_hz → radio).
    for _ in range(5):
        r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "radio"

    r.handle(KeyEvent(char="x"))
    assert s.snapshot().editing_field is None
    # Typing did not advance the cycle either.
    assert cycle.calls == 0


# ── Inbox / mailbox key handling (Phase 1+2) ────────────────────────


from microjs8.store.inbox import InboxRecord


def _inbox_records(*specs):
    """Helper: build a tuple of InboxRecord from short tuples.

    Each spec is (id, type, from_call, text). Other fields default
    to "for us, fixed UTC, no SNR" — keeps tests focused on the
    behavior under test rather than fixture noise.
    """
    return tuple(
        InboxRecord(
            id=spec[0],
            type=spec[1],
            from_call=spec[2],
            to_call="K1ABC",
            text=spec[3],
            utc_iso="2026-05-06T00:00:00.000+00:00",
            offset_hz=1500,
            snr_db=-3,
        )
        for spec in specs
    )


def _router_with_inbox_callback(state, accept_mark_read=True):
    """Build a router with a capturing mark_inbox_read callback."""
    save = _SaveCapture()
    bypass = _BypassCapture()
    calls: list[int] = []

    def _mark(rid: int) -> bool:
        calls.append(rid)
        return accept_mark_read

    r = InputRouter(
        state,
        save_config=save,
        emergency_bypass=bypass,
        mark_inbox_read=_mark,
    )
    return r, calls


# Inbox list nav (Screen.INBOX) ──────────────────────────────────


def test_inbox_arrow_down_advances_focus():
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records(
            (1, "UNREAD", "A", "x"),
            (2, "UNREAD", "B", "y"),
        ),
        held_count=0,
        unread_count=2,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.DOWN))
    assert s.snapshot().inbox_focused_index == 1


def test_inbox_arrow_down_clamps_at_end():
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "x")),
        held_count=0, unread_count=1,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.DOWN))
    r.handle(KeyEvent(key=Key.DOWN))
    assert s.snapshot().inbox_focused_index == 0


def test_inbox_arrow_up_retreats_focus():
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records(
            (1, "UNREAD", "A", "x"),
            (2, "UNREAD", "B", "y"),
        ),
        held_count=0, unread_count=2,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.DOWN))
    r.handle(KeyEvent(key=Key.UP))
    assert s.snapshot().inbox_focused_index == 0


def test_inbox_arrow_up_at_zero_is_noop():
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "x")),
        held_count=0, unread_count=1,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.UP))
    assert s.snapshot().inbox_focused_index == 0


def test_inbox_arrows_on_empty_inbox_are_safe():
    """No items + arrow keys shouldn't crash or transition state."""
    s = _state(screen=Screen.INBOX)
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.UP))
    r.handle(KeyEvent(key=Key.DOWN))
    assert s.snapshot().inbox_focused_index == 0


def test_inbox_left_right_still_does_ring_nav():
    """Left/Right on the inbox should still cycle screens (not blocked)."""
    s = _state(screen=Screen.INBOX)
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.RIGHT))
    assert s.snapshot().screen is Screen.COMPOSE


# Inbox detail entry on Enter ───────────────────────────────────────


def test_inbox_enter_on_focused_row_opens_detail():
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records(
            (1, "UNREAD", "A", "hello"),
            (2, "READ", "B", "old"),
        ),
        held_count=0, unread_count=1,
    )
    r, mark_calls = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    snap = s.snapshot()
    assert snap.screen is Screen.INBOX_DETAIL
    assert snap.inbox_detail_id == 1


def test_inbox_enter_on_unread_calls_mark_read():
    """Opening detail on an UNREAD row triggers the daemon callback
    so the row is persisted as READ."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "hello")),
        held_count=0, unread_count=1,
    )
    r, mark_calls = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    assert mark_calls == [1]


def test_inbox_enter_on_read_skips_mark_read():
    """READ rows shouldn't trigger another mark_read DB write."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "READ", "A", "hello")),
        held_count=0, unread_count=0,
    )
    r, mark_calls = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    assert mark_calls == []


def test_inbox_enter_on_unread_locally_marks_row_read():
    """The router should also update the in-memory cache so the UI
    immediately reflects the new READ state."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "hello")),
        held_count=0, unread_count=1,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    # UIState's internal state was updated locally
    snap = s.snapshot()
    assert snap.inbox_messages[0].is_read is True
    assert snap.inbox_unread_count == 0


def test_inbox_enter_on_empty_inbox_is_noop():
    s = _state(screen=Screen.INBOX)
    r, mark_calls = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    assert s.snapshot().screen is Screen.INBOX
    assert mark_calls == []


def test_inbox_enter_without_callback_still_navigates():
    """If no mark_inbox_read callback is wired, Enter still opens
    detail and updates the local cache."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "hello")),
        held_count=0, unread_count=1,
    )
    save = _SaveCapture()
    bypass = _BypassCapture()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass)
    r.handle(KeyEvent(key=Key.ENTER))
    snap = s.snapshot()
    assert snap.screen is Screen.INBOX_DETAIL


# Inbox detail navigation (Screen.INBOX_DETAIL) ─────────────────────


def test_detail_esc_returns_to_inbox():
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "hello")),
        held_count=0, unread_count=1,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    assert s.snapshot().screen is Screen.INBOX_DETAIL
    r.handle(KeyEvent(key=Key.ESC))
    assert s.snapshot().screen is Screen.INBOX
    assert s.snapshot().inbox_detail_id is None


def test_detail_arrow_keys_are_noop_for_now():
    """Future enhancement is body scroll; for now ↑/↓ in detail-view
    do nothing. Verify they at least don't blow up or change state."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "hello")),
        held_count=0, unread_count=1,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    snap_before = s.snapshot()
    r.handle(KeyEvent(key=Key.UP))
    r.handle(KeyEvent(key=Key.DOWN))
    snap_after = s.snapshot()
    # Still in detail view, same row
    assert snap_after.screen is Screen.INBOX_DETAIL
    assert snap_after.inbox_detail_id == snap_before.inbox_detail_id


def test_detail_does_not_trigger_ring_nav_on_left_right():
    """←/→ in detail-view should be inert — operator can't cycle out
    of the detail view. They have to Esc first."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "A", "hello")),
        held_count=0, unread_count=1,
    )
    r, _ = _router_with_inbox_callback(s)
    r.handle(KeyEvent(key=Key.ENTER))
    r.handle(KeyEvent(key=Key.RIGHT))
    r.handle(KeyEvent(key=Key.LEFT))
    assert s.snapshot().screen is Screen.INBOX_DETAIL


# ── Inbox delete (Delete key) ────────────────────────────────────────


def _router_with_inbox_callbacks(state, accept_mark_read=True, accept_delete=True):
    """Build a router with both mark_inbox_read AND delete_inbox_row capturing callbacks."""
    save = _SaveCapture()
    bypass = _BypassCapture()
    read_calls: list[int] = []
    delete_calls: list[int] = []

    def _mark(rid: int) -> bool:
        read_calls.append(rid)
        return accept_mark_read

    def _delete(rid: int) -> bool:
        delete_calls.append(rid)
        return accept_delete

    r = InputRouter(
        state,
        save_config=save,
        emergency_bypass=bypass,
        mark_inbox_read=_mark,
        delete_inbox_row=_delete,
    )
    return r, read_calls, delete_calls


def test_inbox_delete_removes_focused_row_and_calls_callback():
    """Delete on the INBOX list view: row drops from in-memory list,
    delete_inbox_row callback fires with the row's id, focus clamps."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records(
            (1, "UNREAD", "K1ABC", "msg1"),
            (2, "UNREAD", "K2DEF", "msg2"),
            (3, "READ",   "K3GHI", "msg3"),
        ),
        held_count=0,
        unread_count=2,
    )
    r, _read_calls, delete_calls = _router_with_inbox_callbacks(s)

    # Focus at index 0 by default; Delete should remove row id=1
    r.handle(KeyEvent(key=Key.DELETE))

    # Callback fired with the row's id
    assert delete_calls == [1], (
        f"delete callback should have been called with row id 1; got {delete_calls}"
    )
    # In-memory list shrank by one
    snap = s.snapshot()
    assert len(snap.inbox_messages) == 2
    assert snap.inbox_messages[0].id == 2
    assert snap.inbox_messages[1].id == 3
    # Focus clamped to (still) index 0 — operator's cursor now sits
    # on the row that took the deleted row's position.
    assert snap.inbox_focused_index == 0


def test_inbox_delete_at_end_of_list_moves_focus_up():
    """If the operator deletes the last visible row, focus moves up
    so they're not pointing at empty space."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records(
            (1, "UNREAD", "K1ABC", "x"),
            (2, "UNREAD", "K2DEF", "y"),
        ),
        held_count=0,
        unread_count=2,
    )
    r, _, delete_calls = _router_with_inbox_callbacks(s)
    # Move focus to the last row first
    r.handle(KeyEvent(key=Key.DOWN))
    assert s.snapshot().inbox_focused_index == 1
    # Delete
    r.handle(KeyEvent(key=Key.DELETE))

    assert delete_calls == [2]
    snap = s.snapshot()
    assert len(snap.inbox_messages) == 1
    assert snap.inbox_messages[0].id == 1
    # Focus moved up to the new last row (index 0)
    assert snap.inbox_focused_index == 0


def test_inbox_delete_on_empty_inbox_is_no_op():
    """Delete on an empty inbox: no callback, no error, no state change."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(records=(), held_count=0, unread_count=0)
    r, _, delete_calls = _router_with_inbox_callbacks(s)
    r.handle(KeyEvent(key=Key.DELETE))
    assert delete_calls == [], (
        "delete callback should NOT have been called on empty inbox"
    )
    snap = s.snapshot()
    assert len(snap.inbox_messages) == 0
    assert snap.screen is Screen.INBOX  # no screen change


def test_inbox_delete_only_row_clears_list():
    """Deleting the only row leaves the inbox empty, focus at 0."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "K1ABC", "lonely")),
        held_count=0,
        unread_count=1,
    )
    r, _, delete_calls = _router_with_inbox_callbacks(s)
    r.handle(KeyEvent(key=Key.DELETE))

    assert delete_calls == [1]
    snap = s.snapshot()
    assert len(snap.inbox_messages) == 0
    assert snap.inbox_focused_index == 0


def test_inbox_delete_callback_failure_does_not_crash_router():
    """If the delete callback raises, the router catches it and the
    in-memory drop still happens — UI feels responsive even if the
    persistence layer hiccupped. (The next periodic refresh from
    disk would resurrect the row but that's acceptable degraded
    behavior.)"""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "K1ABC", "x")),
        held_count=0,
        unread_count=1,
    )
    save = _SaveCapture()
    bypass = _BypassCapture()

    def _delete_raises(rid: int) -> bool:
        raise RuntimeError("simulated db error")

    r = InputRouter(
        s, save_config=save, emergency_bypass=bypass,
        delete_inbox_row=_delete_raises,
    )
    # Should not raise
    r.handle(KeyEvent(key=Key.DELETE))
    # In-memory drop still happened
    assert len(s.snapshot().inbox_messages) == 0


def test_inbox_delete_with_no_callback_still_drops_in_memory():
    """If no delete_inbox_row callback is wired (test harness, future
    tools), the router still updates the UI cache. Disk persistence
    won't happen but the screen reflects the operator's action."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "K1ABC", "x")),
        held_count=0,
        unread_count=1,
    )
    save = _SaveCapture()
    bypass = _BypassCapture()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass)
    r.handle(KeyEvent(key=Key.DELETE))
    assert len(s.snapshot().inbox_messages) == 0


def test_inbox_delete_on_detail_view_is_no_op():
    """Delete on INBOX_DETAIL should not trigger a deletion (the
    detail-view is for reading, not list manipulation). Esc closes
    it; Delete is ignored."""
    s = _state(screen=Screen.INBOX)
    s.set_inbox(
        records=_inbox_records((1, "UNREAD", "K1ABC", "x")),
        held_count=0,
        unread_count=1,
    )
    r, _, delete_calls = _router_with_inbox_callbacks(s)
    # Open detail view first (Enter)
    r.handle(KeyEvent(key=Key.ENTER))
    assert s.snapshot().screen is Screen.INBOX_DETAIL
    # Delete inside detail view should be ignored
    r.handle(KeyEvent(key=Key.DELETE))
    assert delete_calls == [], (
        "delete callback fired inside detail-view; should be no-op"
    )
    assert s.snapshot().screen is Screen.INBOX_DETAIL


# ── Compose router branch ─────────────────────────────────────────────


def _router_with_compose_callback(state, accept=True):
    """Build a router with a capturing compose_send callback."""
    save = _SaveCapture()
    bypass = _BypassCapture()
    calls: list[tuple] = []

    def _send(to: str, cmd, text: str) -> bool:
        calls.append((to, cmd, text))
        return accept

    r = InputRouter(
        state, save_config=save, emergency_bypass=bypass,
        compose_send=_send,
    )
    return r, calls


def test_compose_tab_cycles_through_four_fields():
    """Tab on COMPOSE moves focus TO → CMD → TEXT → SEND → TO."""
    from microjs8.ui.state import ComposeCmd  # noqa: F401

    s = _state(screen=Screen.COMPOSE)
    r, _ = _router_with_compose_callback(s)
    expected = ("compose_to", "compose_cmd", "compose_text", "compose_send", "compose_to")
    assert s.snapshot().compose_focused_field == expected[0]
    for nxt in expected[1:]:
        r.handle(KeyEvent(key=Key.TAB))
        assert s.snapshot().compose_focused_field == nxt


def test_compose_typing_in_to_appends_uppercased_char():
    """Type-to-edit on TO field appends and uppercases the character."""
    s = _state(screen=Screen.COMPOSE)
    r, _ = _router_with_compose_callback(s)
    # Focus is on TO by default. Type "k1abc"
    for ch in "k1abc":
        r.handle(KeyEvent(char=ch))
    assert s.snapshot().compose_to == "K1ABC"


def test_compose_backspace_in_to_drops_last_char():
    s = _state(screen=Screen.COMPOSE)
    s.compose_set_to("K1ABC")
    r, _ = _router_with_compose_callback(s)
    r.handle(KeyEvent(key=Key.BACKSPACE))
    assert s.snapshot().compose_to == "K1AB"


def test_compose_typing_in_text_preserves_case_and_spaces():
    """TEXT field is case-sensitive (operator's literal body) and
    accepts spaces via the SPACE key."""
    s = _state(screen=Screen.COMPOSE)
    s.compose_set_to("K1ABC")
    r, _ = _router_with_compose_callback(s)
    # Tab to CMD, Tab to TEXT
    r.handle(KeyEvent(key=Key.TAB))
    r.handle(KeyEvent(key=Key.TAB))
    assert s.snapshot().compose_focused_field == "compose_text"
    for ch in "Hello":
        r.handle(KeyEvent(char=ch))
    r.handle(KeyEvent(key=Key.SPACE))
    for ch in "Dave":
        r.handle(KeyEvent(char=ch))
    assert s.snapshot().compose_text == "Hello Dave"


def test_compose_up_down_on_cmd_cycles_dropdown():
    """↑/↓ when CMD focused cycles the enum; on other fields it
    falls through (returns False) so global keys can still work."""
    from microjs8.ui.state import ComposeCmd, COMPOSE_CMD_ORDER

    s = _state(screen=Screen.COMPOSE)
    r, _ = _router_with_compose_callback(s)
    # Tab to CMD field
    r.handle(KeyEvent(key=Key.TAB))
    assert s.snapshot().compose_focused_field == "compose_cmd"
    # ↓ should advance to MSG
    r.handle(KeyEvent(key=Key.DOWN))
    assert s.snapshot().compose_cmd is ComposeCmd.MSG
    # ↑ should go back to FREE
    r.handle(KeyEvent(key=Key.UP))
    assert s.snapshot().compose_cmd is ComposeCmd.FREE
    # ↑ from FREE wraps to last (MYLOC)
    r.handle(KeyEvent(key=Key.UP))
    assert s.snapshot().compose_cmd is COMPOSE_CMD_ORDER[-1]


def test_compose_esc_clears_fields_stays_on_compose():
    """Esc on COMPOSE clears all fields but keeps the screen on
    COMPOSE — the operator can navigate away with ←/→ themselves."""
    s = _state(screen=Screen.COMPOSE)
    s.compose_set_to("K1ABC")
    s.compose_set_text("hello")
    r, _ = _router_with_compose_callback(s)
    r.handle(KeyEvent(key=Key.ESC))
    snap = s.snapshot()
    assert snap.compose_to == ""
    assert snap.compose_text == ""
    assert snap.screen is Screen.COMPOSE


def test_compose_enter_on_send_fires_callback_and_jumps_to_directed():
    """Enter on the SEND field invokes compose_send with (to, cmd,
    text), clears the compose state, and jumps to DIRECTED so the
    operator sees their TX'd message land in the activity log."""
    from microjs8.ui.state import ComposeCmd

    s = _state(screen=Screen.COMPOSE)
    s.compose_set_to("K1ABC")
    s.compose_set_text("hello dave")
    r, calls = _router_with_compose_callback(s)
    # Tab to SEND (3 tabs from TO)
    for _ in range(3):
        r.handle(KeyEvent(key=Key.TAB))
    assert s.snapshot().compose_focused_field == "compose_send"
    r.handle(KeyEvent(key=Key.ENTER))
    # Callback fired with the right args
    assert calls == [("K1ABC", ComposeCmd.FREE, "hello dave")]
    # State cleared and screen jumped to DIRECTED for the activity log.
    snap = s.snapshot()
    assert snap.compose_to == ""
    assert snap.compose_text == ""
    assert snap.screen is Screen.DIRECTED


def test_compose_enter_on_send_clears_even_if_callback_fails():
    """If the compose_send callback raises, the router still clears
    the compose state — operator isn't stuck looking at a half-sent
    message."""
    s = _state(screen=Screen.COMPOSE)
    s.compose_set_to("K1ABC")
    s.compose_set_text("hi")
    save = _SaveCapture()
    bypass = _BypassCapture()

    def _send_raises(to, cmd, text):
        raise RuntimeError("simulated queue error")
    r = InputRouter(
        s, save_config=save, emergency_bypass=bypass,
        compose_send=_send_raises,
    )
    # Tab to SEND
    for _ in range(3):
        r.handle(KeyEvent(key=Key.TAB))
    r.handle(KeyEvent(key=Key.ENTER))
    snap = s.snapshot()
    assert snap.compose_to == ""
    assert snap.compose_text == ""


def test_compose_left_right_still_navigate_ring():
    """←/→ from COMPOSE still cycle the ring — type-to-edit on TO/TEXT
    consumes printable chars but arrow keys fall through to ring nav.
    Operator can leave Compose mid-edit; in-progress fields persist."""
    s = _state(screen=Screen.COMPOSE)
    s.compose_set_to("K1ABC")
    r, _ = _router_with_compose_callback(s)
    r.handle(KeyEvent(key=Key.LEFT))
    # Should have moved off COMPOSE
    assert s.snapshot().screen is not Screen.COMPOSE
    # TO was preserved
    assert s.compose_to == "K1ABC"


def test_compose_send_with_no_callback_still_clears():
    """If no compose_send callback is wired (test harness, no daemon),
    the router still clears the compose state on Enter — UI stays
    responsive."""
    s = _state(screen=Screen.COMPOSE)
    s.compose_set_to("K1ABC")
    s.compose_set_text("hi")
    save = _SaveCapture()
    bypass = _BypassCapture()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass)
    for _ in range(3):
        r.handle(KeyEvent(key=Key.TAB))
    r.handle(KeyEvent(key=Key.ENTER))
    snap = s.snapshot()
    assert snap.compose_to == ""
    assert snap.compose_text == ""


# ── Phase 3: CardputerZero system keys (Fn+B, Fn+Q) ──────────────────


class _FakeBacklight:
    """Records toggle() calls for assertion."""

    def __init__(self) -> None:
        self.toggles = 0

    def toggle(self) -> None:
        self.toggles += 1


class _FakeShutdownGesture:
    """Records arm()/cancel() calls for assertion."""

    def __init__(self) -> None:
        self.armed = 0
        self.cancelled = 0

    def arm(self) -> None:
        self.armed += 1

    def cancel(self) -> None:
        self.cancelled += 1


def test_fn_b_press_toggles_backlight():
    s = _state()
    save = _SaveCapture()
    bypass = _BypassCapture()
    bl = _FakeBacklight()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass, backlight=bl)
    r.handle(KeyEvent(key=Key.FN_B))
    assert bl.toggles == 1


def test_fn_b_release_does_not_toggle():
    """Fn+B is press-only; the release event (pressed=False) must not
    fire a second toggle and double-flip the state.
    """
    s = _state()
    save = _SaveCapture()
    bypass = _BypassCapture()
    bl = _FakeBacklight()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass, backlight=bl)
    r.handle(KeyEvent(key=Key.FN_B, pressed=False))
    assert bl.toggles == 0


def test_fn_b_with_no_backlight_service_is_silent():
    """If no Backlight is wired, Fn+B must be silently dropped — no
    AttributeError, no log spam.
    """
    s = _state()
    save = _SaveCapture()
    bypass = _BypassCapture()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass)
    r.handle(KeyEvent(key=Key.FN_B))    # must not raise


def test_fn_q_press_arms_shutdown_gesture():
    s = _state()
    save = _SaveCapture()
    bypass = _BypassCapture()
    sg = _FakeShutdownGesture()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass,
                    shutdown_gesture=sg)
    r.handle(KeyEvent(key=Key.FN_Q, pressed=True))
    assert sg.armed == 1
    assert sg.cancelled == 0


def test_fn_q_release_cancels_shutdown_gesture():
    s = _state()
    save = _SaveCapture()
    bypass = _BypassCapture()
    sg = _FakeShutdownGesture()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass,
                    shutdown_gesture=sg)
    r.handle(KeyEvent(key=Key.FN_Q, pressed=False))
    assert sg.armed == 0
    assert sg.cancelled == 1


def test_fn_q_dispatches_through_edit_mode():
    """The shutdown gesture must work even when the operator is mid-edit.

    The operator must always be able to power down regardless of any
    in-progress text-entry — that's the whole point of having a clean
    shutdown gesture vs the hardware power button.
    """
    s = _state(screen=Screen.SETUP)
    s.begin_edit("callsign")        # enters edit mode
    assert s.is_editing()

    save = _SaveCapture()
    bypass = _BypassCapture()
    sg = _FakeShutdownGesture()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass,
                    shutdown_gesture=sg)
    r.handle(KeyEvent(key=Key.FN_Q, pressed=True))
    # Gesture armed even though we were editing.
    assert sg.armed == 1
    # Edit mode is still in progress (the gesture didn't clobber it).
    assert s.is_editing()


def test_fn_b_dispatches_through_edit_mode():
    """Backlight toggle also works mid-edit — operator may want to dim
    the screen while typing a long message in low light.
    """
    s = _state(screen=Screen.SETUP)
    s.begin_edit("callsign")
    assert s.is_editing()

    save = _SaveCapture()
    bypass = _BypassCapture()
    bl = _FakeBacklight()
    r = InputRouter(s, save_config=save, emergency_bypass=bypass, backlight=bl)
    r.handle(KeyEvent(key=Key.FN_B))
    assert bl.toggles == 1
    assert s.is_editing()
