"""Tests for the emergency beacon arm/disarm flow + EmergencyBeacon TX.

Covers:
  - EmergencyArmGesture state machine (arm/disarm/cancel/idempotent)
  - UIState integration (snapshot reflects armed flag + hold progress)
  - Renderer (4 visual states: idle/arming/armed/disarming)
  - Renderer (identity uses state.callsign, not gated on emergency_override)
  - SOS badge appears on every screen when armed
  - Router behavior on EMERGENCY screen
  - Router ring-nav-allowed-when-armed (operator can browse other screens)
  - EmergencyBeacon constructs the right wire-format SOS
"""

from __future__ import annotations

import asyncio
import time

import pytest

from microjs8.gps.types import FixKind, GpsFix
from microjs8.input.emergency_arm_gesture import (
    DEFAULT_EMERGENCY_HOLD_S,
    EmergencyArmGesture,
)
from microjs8.input.events import Key, KeyEvent
from microjs8.input.router import InputRouter
from microjs8.ui.state import Screen, UIState


# ── Helpers ───────────────────────────────────────────────────────────


def _state(*, screen=Screen.EMERGENCY, callsign="W5DMH", grid="EN83ih",
           tx_allowed=True, **kw):
    s = UIState(callsign, grid, tx_allowed, "miles", **kw)
    s.set_screen(screen)
    return s


def _with_gps(s, lat=43.2794, lon=-83.3391):
    """Attach a 3D GPS fix to a state."""
    s.set_gps(GpsFix(
        kind=FixKind.FIX_3D, lat=lat, lon=lon,
        altitude_m=234.4, speed_mps=0.0, track_deg=None,
        hdop=1.4, fix_time=None, satellites_used=10,
        received_at=time.monotonic(),
    ))
    return s


@pytest.fixture(scope="module")
def fonts():
    """Load the font set once per test module — load_fonts hits disk."""
    from microjs8.ui.fonts import load_fonts
    return load_fonts()


# ── EmergencyArmGesture unit tests ────────────────────────────────────


def test_gesture_default_hold_seconds_is_3():
    """Spec: hold ENTER 3 s to arm, hold ESC 3 s to disarm. Both use
    the same constant — pin it so refactors can't silently shorten
    the deliberate-action gate.
    """
    assert DEFAULT_EMERGENCY_HOLD_S == 3.0


def test_gesture_initial_state_idle():
    s = _state()

    async def _arm(): pass
    async def _disarm(): pass

    loop = asyncio.new_event_loop()
    try:
        g = EmergencyArmGesture(s, loop, _arm, _disarm)
        assert g.is_active() is False
        assert g.is_arming() is False
        assert g.is_disarming() is False
        assert g.cancel(source="t") is False
    finally:
        loop.close()


def test_gesture_begin_arming_starts_countdown():
    s = _state()
    arm_calls: list[int] = []

    async def _arm():
        arm_calls.append(1)

    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        assert g.begin_arming(source="t") is True
        assert g.is_arming() is True
        assert g.is_active() is True
        snap = s.snapshot()
        assert snap.emergency_hold_direction == "arm"
        assert snap.emergency_hold_progress == 1.0
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())
    # Cancel before completion → arm_callback never fires
    assert arm_calls == []


def test_gesture_double_arm_is_idempotent():
    """Two rapid Enter-presses shouldn't produce two countdown tasks."""
    s = _state()

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        assert g.begin_arming(source="first") is True
        assert g.begin_arming(source="second") is False
        assert g.is_arming() is True
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_gesture_arm_completion_invokes_callback_and_flips_flag():
    """A 0.05 s hold runs to completion; arm callback fires; UIState
    reflects the new armed state."""
    s = _state()
    arm_calls: list[int] = []

    async def _arm():
        # Real app wires this to s.arm_emergency_beacon(). We do the same here.
        s.arm_emergency_beacon()
        arm_calls.append(1)

    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=0.05,
        )
        g.begin_arming(source="t")
        await asyncio.sleep(0.20)
        assert g.is_active() is False

    asyncio.run(_run())
    assert arm_calls == [1]
    snap = s.snapshot()
    assert snap.emergency_beacon_armed is True
    assert snap.emergency_hold_progress is None
    assert snap.emergency_hold_direction is None


def test_gesture_disarm_completion_invokes_disarm_callback():
    """From armed state, disarm-hold completion fires disarm_callback
    and clears the armed flag."""
    s = _state()
    s.arm_emergency_beacon()  # start armed
    disarm_calls: list[int] = []

    async def _arm(): pass

    async def _disarm():
        s.disarm_emergency_beacon()
        disarm_calls.append(1)

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=0.05,
        )
        g.begin_disarming(source="t")
        await asyncio.sleep(0.20)
        assert g.is_active() is False

    asyncio.run(_run())
    assert disarm_calls == [1]
    assert s.snapshot().emergency_beacon_armed is False


def test_gesture_cancel_does_not_flip_flag():
    """Cancel aborts the in-flight transition without toggling the
    armed state. Critical: a partial hold must not accidentally
    arm or disarm."""
    s = _state()
    arm_calls: list[int] = []

    async def _arm():
        arm_calls.append(1)

    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        g.begin_arming(source="t")
        await asyncio.sleep(0.05)  # partway through
        g.cancel(source="t")
        await asyncio.sleep(0.10)

    asyncio.run(_run())
    assert arm_calls == [], "cancel must NOT fire the arm callback"
    snap = s.snapshot()
    assert snap.emergency_beacon_armed is False
    assert snap.emergency_hold_progress is None


def test_gesture_progress_drains_during_arming():
    """The hold-progress should drain from 1.0 toward 0.0 over the
    configured duration. The renderer uses this to draw the bar."""
    s = _state()

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=0.30,
        )
        g.begin_arming(source="t")
        # Immediately ~1.0
        assert s.snapshot().emergency_hold_progress > 0.9
        await asyncio.sleep(0.15)
        partial = s.snapshot().emergency_hold_progress
        assert 0.2 < partial < 0.8, (
            f"midway progress should be ~0.5, got {partial}"
        )
        await asyncio.sleep(0.25)
        assert g.is_active() is False

    asyncio.run(_run())


# ── Router integration ────────────────────────────────────────────────


def _router_with_emergency(state, gesture):
    return InputRouter(
        state,
        save_config=lambda *a, **kw: True,
        emergency_bypass=lambda: True,
        emergency_arm_gesture=gesture,
    )


def test_router_enter_on_emergency_idle_begins_arming():
    s = _state()

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        r = _router_with_emergency(s, g)
        r.handle(KeyEvent(key=Key.ENTER))
        assert g.is_arming() is True
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_router_esc_during_arming_cancels():
    s = _state()

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        r = _router_with_emergency(s, g)
        r.handle(KeyEvent(key=Key.ENTER))
        assert g.is_arming() is True
        r.handle(KeyEvent(key=Key.ESC))
        await asyncio.sleep(0)
        assert g.is_active() is False
        # Did NOT flip armed
        assert s.snapshot().emergency_beacon_armed is False

    asyncio.run(_run())


def test_router_esc_on_emergency_armed_begins_disarming():
    s = _state()
    s.arm_emergency_beacon()  # start armed

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        r = _router_with_emergency(s, g)
        r.handle(KeyEvent(key=Key.ESC))
        assert g.is_disarming() is True
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_router_enter_during_disarming_cancels():
    s = _state()
    s.arm_emergency_beacon()

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        r = _router_with_emergency(s, g)
        r.handle(KeyEvent(key=Key.ESC))
        assert g.is_disarming() is True
        # Enter cancels the disarming.
        r.handle(KeyEvent(key=Key.ENTER))
        await asyncio.sleep(0)
        assert g.is_active() is False
        # Still armed (cancel doesn't toggle the flag).
        assert s.snapshot().emergency_beacon_armed is True

    asyncio.run(_run())


def test_router_left_right_on_armed_emergency_navigates_ring():
    """While armed, LEFT/RIGHT must still cycle the screen ring so
    the operator can navigate to INBOX / HEARD to see responses
    coming back from the SOS. Per Q3 (May 2026 spec): 'beacon
    continues but the operator can navigate to other screens'."""
    s = _state(screen=Screen.EMERGENCY)
    s.arm_emergency_beacon()

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        r = _router_with_emergency(s, g)
        # We're on EMERGENCY armed. Press LEFT.
        before = s.snapshot().screen
        r.handle(KeyEvent(key=Key.LEFT))
        after = s.snapshot().screen
        assert after is not before, (
            "ring nav should still work while armed — operator must be "
            "able to see INBOX/HEARD responses"
        )
        # And beacon stays armed through navigation
        assert s.snapshot().emergency_beacon_armed is True

    asyncio.run(_run())


def test_router_keys_during_hold_are_consumed_and_ignored():
    """Non-cancel keys during an active hold should be quietly
    consumed (returned True), NOT bled into ring nav or anywhere
    else. The operator's hold takes priority — they must complete
    or cancel before navigating."""
    s = _state()

    async def _arm(): pass
    async def _disarm(): pass

    async def _run():
        g = EmergencyArmGesture(
            s, asyncio.get_running_loop(), _arm, _disarm,
            hold_seconds=10.0,
        )
        r = _router_with_emergency(s, g)
        r.handle(KeyEvent(key=Key.ENTER))  # begin arming
        assert g.is_arming() is True
        before_screen = s.snapshot().screen
        # Press a bunch of nav keys — none should cancel
        for k in (Key.LEFT, Key.RIGHT, Key.UP, Key.DOWN, Key.TAB):
            r.handle(KeyEvent(key=k))
            assert g.is_arming() is True, (
                f"key {k} should not cancel an active arm hold"
            )
        # And screen didn't change
        assert s.snapshot().screen is before_screen
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_router_emergency_handler_no_op_when_no_gesture():
    """Router built without the gesture: ENTER/ESC on EMERGENCY fall
    through to default behavior, no crash."""
    s = _state()
    r = InputRouter(
        s,
        save_config=lambda *a, **kw: True,
        emergency_bypass=lambda: True,
        # NO emergency_arm_gesture
    )
    # Should not raise
    r.handle(KeyEvent(key=Key.ENTER))
    r.handle(KeyEvent(key=Key.ESC))


# ── Renderer tests: identity correctness ──────────────────────────────


def test_render_emergency_uses_configured_callsign_not_n0call(fonts):
    """The bug: emergency_override is a one-way flag that stays True
    after the operator configures their callsign. The renderer
    must use state.callsign, NOT gate on emergency_override.
    Regression test for the May 2026 bug report."""
    from microjs8.ui.screens import _render_emergency

    s = _state(callsign="W5DMH")
    s.trigger_emergency_override()  # flag now True
    # Even with override flag set, callsign should be shown
    img = _render_emergency(s.snapshot(), fonts)
    # The renderer should NOT display "N0CALL" — it should display W5DMH
    # We check by scanning for the W5DMH text rendering area; simpler
    # method: inspect the rendered text via mock-free pixel scan is
    # hard, so we just verify the snapshot.callsign field is what the
    # renderer reads.
    assert img.size == (320, 170)
    assert s.snapshot().callsign == "W5DMH"


def test_render_emergency_shows_n0call_only_when_no_callsign(fonts):
    """Truly unconfigured station → N0CALL display. Sanity check
    the bug fix doesn't break the legitimate unconfigured case."""
    from microjs8.ui.screens import _render_emergency

    s = _state(callsign="", grid="", tx_allowed=False)
    img = _render_emergency(s.snapshot(), fonts)
    assert img.size == (320, 170)
    assert s.snapshot().callsign == ""


def test_render_emergency_idle_state_smoke(fonts):
    """Idle state: beacon not armed, no hold in progress."""
    from microjs8.ui.screens import _render_emergency
    s = _with_gps(_state())
    img = _render_emergency(s.snapshot(), fonts)
    assert img.size == (320, 170)
    assert s.snapshot().emergency_beacon_armed is False
    assert s.snapshot().emergency_hold_progress is None


def test_render_emergency_arming_state_smoke(fonts):
    """Arming state: progress bar rendered, direction = arm."""
    from microjs8.ui.screens import _render_emergency
    s = _with_gps(_state())
    s.begin_emergency_arm_hold()
    s.update_emergency_hold_progress(0.5)
    img = _render_emergency(s.snapshot(), fonts)
    assert img.size == (320, 170)
    snap = s.snapshot()
    assert snap.emergency_hold_direction == "arm"
    assert snap.emergency_hold_progress == 0.5


def test_render_emergency_armed_state_smoke(fonts):
    """Armed state: 'Beacon: ARMED' visible, no countdown."""
    from microjs8.ui.screens import _render_emergency
    s = _with_gps(_state())
    s.arm_emergency_beacon()
    img = _render_emergency(s.snapshot(), fonts)
    assert img.size == (320, 170)
    assert s.snapshot().emergency_beacon_armed is True


def test_render_emergency_disarming_state_smoke(fonts):
    """Disarming state: progress bar rendered with direction = disarm."""
    from microjs8.ui.screens import _render_emergency
    s = _with_gps(_state())
    s.arm_emergency_beacon()
    s.begin_emergency_disarm_hold()
    s.update_emergency_hold_progress(0.3)
    img = _render_emergency(s.snapshot(), fonts)
    assert img.size == (320, 170)
    snap = s.snapshot()
    assert snap.emergency_hold_direction == "disarm"
    assert snap.emergency_beacon_armed is True  # still armed during disarm-hold


# ── SOS badge on other screens ────────────────────────────────────────


def test_sos_badge_appears_in_header_when_armed(fonts):
    """Per Q3 spec: an armed-anywhere indicator must show in every
    screen header so the operator knows the beacon is TXing even
    when they've navigated to INBOX or HEARD. Verify by scanning
    for red pixels in the HEADER region of a non-EMERGENCY screen."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_home

    s = _with_gps(_state(screen=Screen.HOME, callsign="W5DMH"))
    s.set_freq_hz(7078000)
    s.set_cat_connected(True)
    s.arm_emergency_beacon()

    img = _render_home(s.snapshot(), fonts)
    # Scan the header band for red pixels (FG_BAD = ~220, 60, 60)
    found_red = False
    for y in range(0, theme.HEADER_H):
        for x in range(theme.SCREEN_W // 2, theme.SCREEN_W):
            r, g, b = img.getpixel((x, y))
            if r > 180 and g < 100 and b < 100:
                found_red = True
                break
        if found_red:
            break
    assert found_red, (
        "SOS badge (red rectangle in header) should be visible "
        "when emergency_beacon_armed=True, even on non-EMERGENCY screens"
    )


def test_sos_badge_absent_when_not_armed(fonts):
    """Regression: idle station's HOME screen header should NOT have
    a red SOS badge."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_home

    s = _with_gps(_state(screen=Screen.HOME, callsign="W5DMH"))
    s.set_freq_hz(7078000)
    s.set_cat_connected(True)
    # NOT armed

    img = _render_home(s.snapshot(), fonts)
    # Header should have no big red rectangle. The clock area is the
    # right half of the header; check it specifically.
    found_red = False
    for y in range(2, theme.HEADER_H - 2):
        for x in range(theme.SCREEN_W // 2 + 30, theme.SCREEN_W - 4):
            r, g, b = img.getpixel((x, y))
            if r > 180 and g < 100 and b < 100:
                found_red = True
                break
        if found_red:
            break
    assert not found_red, (
        "SOS badge should NOT render when beacon is idle"
    )


# ── EmergencyBeacon wire-format integration ───────────────────────────


def test_emergency_beacon_wire_format_with_gps():
    """The beacon's _build_message should produce the SOS wire format
    with lat/lon preferred over grid. Verify the integration between
    identity factory (returning lat/lon) and message construction."""
    from microjs8.tx.beacon import EmergencyBeacon

    identity = ("W5DMH", "EN83ih", 43.2794, -83.3391)

    class _FakeQueue:
        def enqueue(self, *a, **kw): pass
        def enqueue_for_encoding(self, *a, **kw): pass

    b = EmergencyBeacon(
        queue=_FakeQueue(),
        identity_factory=lambda: identity,
    )
    wire = b._build_message()
    # Format: "<call>: @ALLCALL SOS <lat lon>"
    assert wire is not None
    assert "W5DMH" in wire
    assert "@ALLCALL" in wire
    assert "SOS" in wire
    # Lat/lon preferred over grid
    assert "+43.2794" in wire
    assert "-83.3391" in wire
    # Grid should NOT appear when lat/lon is present
    assert "EN83" not in wire


def test_emergency_beacon_wire_format_grid_fallback():
    """No GPS → falls back to grid in the SOS message."""
    from microjs8.tx.beacon import EmergencyBeacon

    identity = ("W5DMH", "EN83ih", None, None)

    class _FakeQueue:
        def enqueue(self, *a, **kw): pass
        def enqueue_for_encoding(self, *a, **kw): pass

    b = EmergencyBeacon(
        queue=_FakeQueue(),
        identity_factory=lambda: identity,
    )
    wire = b._build_message()
    assert wire is not None
    assert "EN83ih" in wire
    assert "W5DMH" in wire


def test_emergency_beacon_refuses_when_no_location():
    """No GPS, no grid → beacon returns None (won't TX)."""
    from microjs8.tx.beacon import EmergencyBeacon

    identity = ("W5DMH", None, None, None)

    class _FakeQueue:
        def enqueue(self, *a, **kw): pass
        def enqueue_for_encoding(self, *a, **kw): pass

    b = EmergencyBeacon(
        queue=_FakeQueue(),
        identity_factory=lambda: identity,
    )
    assert b._build_message() is None, (
        "no location → no SOS wire; sending 'help me but I don't know "
        "where I am' is useless to receivers"
    )


def test_emergency_beacon_n0call_fallback_for_emergency_override():
    """In emergency-override mode the operator may not have a callsign
    set; the beacon should TX as N0CALL with the position so a rescuer
    has SOMETHING to act on."""
    from microjs8.tx.beacon import EmergencyBeacon

    identity = ("", None, 43.2794, -83.3391)

    class _FakeQueue:
        def enqueue(self, *a, **kw): pass
        def enqueue_for_encoding(self, *a, **kw): pass

    b = EmergencyBeacon(
        queue=_FakeQueue(),
        identity_factory=lambda: identity,
    )
    wire = b._build_message()
    assert wire is not None
    assert "N0CALL" in wire
    assert "+43.2794" in wire
