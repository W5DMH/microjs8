"""Tests for the Step 6 Setup-screen frequency edit and CAT status.

The frequency edit field uses the same edit pipeline as callsign/grid
but routes its commit through a separate CAT callback rather than
the persistent-config save path.
"""

from __future__ import annotations

import pytest

from microjs8.input.events import Key, KeyEvent
from microjs8.input.router import InputRouter
from microjs8.ui.state import Screen, UIState


def _state() -> UIState:
    """Configured state on the Setup screen, ready to edit fields."""
    s = UIState("K1ABC", "FN42", True, "miles")
    s.set_screen(Screen.SETUP)
    return s


class _Spy:
    def __init__(self, return_value=True):
        self.calls: list = []
        self.return_value = return_value

    def __call__(self, *args):
        self.calls.append(args)
        return self.return_value


def _tab_to_freq_and_start_edit(r: InputRouter, s: UIState) -> None:
    """Tab to the freq_hz field and press Enter to enter edit mode.

    On entering edit mode, the buffer is pre-filled with the current
    frequency formatted as MHz, e.g. "7.078".
    """
    # 4 tabs: callsign -> grid -> groups -> units -> freq_hz.
    for _ in range(4):
        r.handle(KeyEvent(key=Key.TAB))
    assert s.focused_field_name() == "freq_hz"
    r.handle(KeyEvent(key=Key.ENTER))
    assert s.is_editing()


def _type_freq_replacing_prefill(r: InputRouter, text: str) -> None:
    """Replace the prefilled buffer by backspacing then typing.

    Driving via public API only — backspace until empty, then type
    the new chars.
    """
    # Backspace at most 16 times to clear the prefill (MHz strings
    # are at most ~10 chars).
    for _ in range(16):
        r.handle(KeyEvent(key=Key.BACKSPACE))
    for ch in text:
        r.handle(KeyEvent(char=ch))


# ── Frequency edit happy path ────────────────────────────────────────


def test_freq_field_prefills_with_current_mhz():
    s = _state()
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=_Spy(),
    )
    _tab_to_freq_and_start_edit(r, s)
    # Default freq is 7_078_000 → "7.078" formatted.
    assert s.edit_buffer() == "7.078"


def test_freq_edit_accepts_mhz_form():
    """Operator types '14.078' on Setup; CAT receives 14_078_000 Hz."""
    s = _state()
    save_cb = _Spy()
    set_freq = _Spy(return_value=True)
    r = InputRouter(
        s, save_config=save_cb, emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "14.078")
    r.handle(KeyEvent(key=Key.ENTER))

    assert set_freq.calls == [(14_078_000,)]
    assert s.snapshot().freq_hz == 14_078_000
    assert not s.is_editing()
    # Persistent config never touched.
    assert save_cb.calls == []


def test_freq_edit_accepts_integer_hz():
    """Some operators may type 7078000 directly."""
    s = _state()
    set_freq = _Spy(return_value=True)
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "7078000")
    r.handle(KeyEvent(key=Key.ENTER))
    assert set_freq.calls == [(7_078_000,)]


def test_freq_edit_accepts_short_int_as_mhz():
    """Type '7' meaning 7 MHz — common shorthand."""
    s = _state()
    set_freq = _Spy(return_value=True)
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "7")
    r.handle(KeyEvent(key=Key.ENTER))
    # 7 < 1_000_000, treated as MHz.
    assert set_freq.calls == [(7_000_000,)]


def test_freq_edit_european_comma_decimal_works():
    """European keyboards may type ',' instead of '.'."""
    s = _state()
    set_freq = _Spy(return_value=True)
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "7,078")
    r.handle(KeyEvent(key=Key.ENTER))
    assert set_freq.calls == [(7_078_000,)]


# ── Frequency edit failure paths ─────────────────────────────────────


def test_freq_edit_no_cat_callback_fails():
    """When CAT is unavailable, freq edit always fails — no callback."""
    s = _state()
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=None,  # no CAT
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "14.078")
    r.handle(KeyEvent(key=Key.ENTER))

    assert s.snapshot().edit_invalid


def test_freq_edit_cat_rejects():
    """CAT callback returns False (e.g. radio said no)."""
    s = _state()
    set_freq = _Spy(return_value=False)
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "14.078")
    r.handle(KeyEvent(key=Key.ENTER))

    snap = s.snapshot()
    assert snap.edit_invalid
    # set_frequency was called, but the displayed freq_hz did NOT update.
    assert set_freq.calls == [(14_078_000,)]
    assert snap.freq_hz == 7_078_000  # unchanged


def test_freq_edit_unparseable_text():
    s = _state()
    set_freq = _Spy(return_value=True)
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "abc")
    r.handle(KeyEvent(key=Key.ENTER))
    assert s.snapshot().edit_invalid
    # CAT was never called for an unparseable string.
    assert set_freq.calls == []


def test_freq_edit_out_of_range_rejected_at_ui():
    """Frequencies outside HF range get caught at the UI layer."""
    s = _state()
    set_freq = _Spy(return_value=True)
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "999")
    r.handle(KeyEvent(key=Key.ENTER))
    # 999 MHz is out of HF range; UI flags as invalid before CAT.
    assert s.snapshot().edit_invalid
    assert set_freq.calls == []


def test_freq_edit_empty_buffer_rejected():
    s = _state()
    set_freq = _Spy(return_value=True)
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
        set_frequency=set_freq,
    )
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "")
    r.handle(KeyEvent(key=Key.ENTER))
    assert s.snapshot().edit_invalid
    assert set_freq.calls == []


# ── CAT status mutator ───────────────────────────────────────────────


def test_cat_connected_default_false():
    s = UIState("K1ABC", "FN42", True, "miles")
    assert s.snapshot().cat_connected is False


def test_set_cat_connected_dirties_state():
    s = UIState("K1ABC", "FN42", True, "miles")
    # Drain initial dirty.
    s.consume_dirty()
    s.set_cat_connected(True)
    assert s.consume_dirty()
    assert s.snapshot().cat_connected is True


def test_set_cat_connected_idempotent():
    """Setting the same value twice doesn't dirty the state."""
    s = UIState("K1ABC", "FN42", True, "miles")
    s.set_cat_connected(True)
    s.consume_dirty()
    s.set_cat_connected(True)
    assert not s.consume_dirty()


# ── Router constructor accepts new optional arg ──────────────────────


def test_router_works_without_set_frequency():
    """Existing call sites that don't pass set_frequency still work
    — the field becomes effectively read-only."""
    s = _state()
    r = InputRouter(
        s, save_config=_Spy(), emergency_bypass=lambda: None,
    )
    # Tabbing to and trying to edit freq_hz must not crash.
    _tab_to_freq_and_start_edit(r, s)
    _type_freq_replacing_prefill(r, "14.078")
    r.handle(KeyEvent(key=Key.ENTER))
    # Edit was rejected (no CAT), but no exception.
    assert s.snapshot().edit_invalid
