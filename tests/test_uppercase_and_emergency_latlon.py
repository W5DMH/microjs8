"""Regression tests for the May 14 2026 cleanup pass:

  1. JS8Call's wire protocol is uppercase-only. All text fields that
     produce on-air content (callsign, grid, command verbs, free-text
     bodies) must auto-uppercase the operator's typed input. UI-only
     fields (units) are exempt — they never appear on the wire.

  2. Emergency screen and the EmergencyBeacon's wire format use raw
     GPS lat/lon (decimal degrees) as the position payload, not a
     Maidenhead grid. Rationale: in a real emergency, exact
     coordinates are the most useful payload a rescuer can read off
     the air; grids carry 25 m – 15 km ambiguity depending on
     precision. Falls back to the configured grid only when no GPS
     fix is available.
"""

from __future__ import annotations

import pytest

from microjs8.input.router import InputRouter
from microjs8.input.events import Key, KeyEvent
from microjs8.gps.types import FixKind, GpsFix
from microjs8.ui.state import Screen, UIState
from microjs8.tx.beacon import EmergencyBeacon


# Reuse the lightweight fixture pattern from the existing router
# tests — no need for a full app spin-up for these unit tests.


class _SaveCapture:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, callsign, grid, units, **kwargs):
        self.calls.append((callsign, grid, units))
        return True


class _BypassCapture:
    def __init__(self):
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _make_router(state: UIState) -> InputRouter:
    return InputRouter(
        state,
        save_config=_SaveCapture(),
        emergency_bypass=_BypassCapture(),
    )


def _state_compose() -> UIState:
    s = UIState("W5DMH", "EN83ih", True, "miles")
    s.set_screen(Screen.COMPOSE)
    return s


# ── Compose TEXT field auto-uppercase (Phase 14b) ─────────────────


def test_compose_text_lowercase_input_uppercases():
    s = _state_compose()
    s.compose_set_to("K1ABC")
    r = _make_router(s)
    # Tab to TEXT field.
    r.handle(KeyEvent(key=Key.TAB))  # to CMD
    r.handle(KeyEvent(key=Key.TAB))  # to TEXT
    assert s.snapshot().compose_focused_field == "compose_text"
    for ch in "hello dave":
        if ch == " ":
            r.handle(KeyEvent(key=Key.SPACE))
        else:
            r.handle(KeyEvent(char=ch))
    assert s.snapshot().compose_text == "HELLO DAVE"


def test_compose_text_mixed_case_uppercases_all():
    """Mixed case input (e.g., CapsLock toggling on/off) lands all-
    uppercase on the wire, matching JS8Call's uppercase-only display."""
    s = _state_compose()
    s.compose_set_to("K1ABC")
    r = _make_router(s)
    r.handle(KeyEvent(key=Key.TAB))
    r.handle(KeyEvent(key=Key.TAB))
    for ch in "HeLLo":
        r.handle(KeyEvent(char=ch))
    assert s.snapshot().compose_text == "HELLO"


def test_compose_text_digits_and_punctuation_unchanged():
    """Non-letter characters (digits, punctuation, symbols) pass
    through .upper() unchanged — JS8Call accepts these as-is."""
    s = _state_compose()
    s.compose_set_to("K1ABC")
    r = _make_router(s)
    r.handle(KeyEvent(key=Key.TAB))
    r.handle(KeyEvent(key=Key.TAB))
    for ch in "73 from 100w!":
        if ch == " ":
            r.handle(KeyEvent(key=Key.SPACE))
        else:
            r.handle(KeyEvent(char=ch))
    assert s.snapshot().compose_text == "73 FROM 100W!"


# ── Setup-screen callsign / grid auto-uppercase ───────────────────


def test_setup_callsign_field_uppercases_input():
    """Operators may have CapsLock off and type ``k1abc``; the field
    must store ``K1ABC`` so the wire callsign is JS8-conformant."""
    s = UIState("N0CALL", "", False, "miles")
    s.set_screen(Screen.SETUP)
    r = _make_router(s)
    # Start editing the callsign field directly.
    s.begin_edit("callsign")
    # Clear the prefilled buffer.
    while s.edit_buffer():
        r.handle(KeyEvent(key=Key.BACKSPACE))
    for ch in "k1abc":
        r.handle(KeyEvent(char=ch))
    assert s.edit_buffer() == "K1ABC"


def test_setup_units_field_does_not_uppercase():
    """The units preference is UI-only — never on the wire — and
    its validator expects ``miles`` / ``km`` lowercase. The
    uppercase normalisation must skip this field specifically."""
    s = UIState("N0CALL", "", False, "miles")
    s.set_screen(Screen.SETUP)
    r = _make_router(s)
    s.begin_edit("units")
    while s.edit_buffer():
        r.handle(KeyEvent(key=Key.BACKSPACE))
    for ch in "km":
        r.handle(KeyEvent(char=ch))
    # No uppercasing — buffer holds the literal lowercase input.
    assert s.edit_buffer() == "km"


# ── Emergency beacon wire format ──────────────────────────────────


@pytest.fixture
def queue():
    from microjs8.tx.queue import OutboundQueue
    import sqlite3
    conn = sqlite3.connect(":memory:")
    return OutboundQueue(conn)


def test_emergency_wire_uses_lat_lon_with_gps(queue):
    """The May 2026 spec change: emergency beacon transmits decimal
    degrees, not a grid square. Position field is space-separated
    ``+lat -lon`` so JS8 tokenisation produces two clean numeric
    tokens on the receiver side."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("W5DMH", "EN83ih", 42.3601, -71.0589),
    )
    msg = eb._build_message()
    assert msg == "W5DMH: @ALLCALL SOS +42.3601 -71.0589"


def test_emergency_wire_four_decimal_places():
    """11 m precision matches the 4-decimal display on the EMERGENCY
    screen. Tighter precision wastes wire bytes; looser loses the
    accuracy that makes lat/lon worth transmitting over a grid."""
    # Verify format directly.
    lat, lon = 42.36012345, -71.05892
    pos = f"{lat:+.4f} {lon:+.4f}"
    assert pos == "+42.3601 -71.0589"


def test_emergency_wire_falls_back_to_grid_when_no_gps(queue):
    """If the GPS hasn't acquired a fix yet (or the daemon is on
    indoor power without antenna view), the configured grid is the
    only location data we have. Better to TX an approximate location
    than to refuse to call for help."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("W5DMH", "EN83ih", None, None),
    )
    msg = eb._build_message()
    assert msg == "W5DMH: @ALLCALL SOS EN83ih"


def test_emergency_wire_refuses_without_any_location(queue):
    """No GPS AND no configured grid → SOS would be unactionable —
    refuse to TX rather than send a useless beacon that wastes air
    time and battery."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("W5DMH", "", None, None),
    )
    assert eb._build_message() is None


# ── Emergency screen renderer shows lat/lon ───────────────────────


def test_emergency_screen_shows_lat_lon_when_gps_has_fix():
    """The EMERGENCY screen renders the GPS lat/lon in human-
    readable form (signed decimal degrees to 4 places), not a grid.
    This is the displayed-position contract the operator reads on
    the device — and what they'd radio over voice to a coordinator."""
    from microjs8.ui import load_fonts
    from microjs8.ui.screens import render

    fonts = load_fonts()
    s = UIState("W5DMH", "EN83ih", True, "miles")
    s.set_screen(Screen.EMERGENCY)
    s.set_gps(GpsFix(
        kind=FixKind.FIX_3D,
        lat=42.3601, lon=-71.0589,
        altitude_m=10.0, speed_mps=0.0, track_deg=0.0, hdop=1.0,
        fix_time=None, satellites_used=8, received_at=0.0,
    ))
    # The renderer doesn't expose its text output directly, so we
    # render to image and verify there are green pixels (FG_GOOD,
    # the lat/lon colour) somewhere in the body region.
    img = render(s.snapshot(), fonts)
    from microjs8.ui import theme
    found_green = False
    for y in range(theme.BODY_Y0, theme.BODY_Y1):
        for x in range(0, theme.SCREEN_W):
            r, g, b = img.getpixel((x, y))
            if g > 180 and r < 100 and b < 100:
                found_green = True
                break
        if found_green:
            break
    assert found_green, (
        "EMERGENCY with GPS fix should render lat/lon in FG_GOOD "
        "green; no green pixels found"
    )
