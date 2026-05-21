"""Tests for the shared ShutdownGesture + keyboard Fn+Q path.

Covers:
  - ShutdownGesture.arm() / cancel() / is_armed() basics
  - Idempotent arm (two paths racing)
  - Cancel from a different "source" than the arm (button release
    can cancel a keyboard-armed countdown and vice versa)
  - Countdown completion invokes the callback
  - Router Fn+Q arms the shared gesture
  - Router Esc on SHUTTING_DOWN cancels
"""

from __future__ import annotations

import asyncio

import pytest

from microjs8.input.events import Key, KeyEvent
from microjs8.input.router import InputRouter
from microjs8.input.shutdown_gesture import (
    DEFAULT_SHUTDOWN_HOLD_S,
    ShutdownGesture,
)
from microjs8.ui.state import Screen, UIState


def _state(*, screen=Screen.HOME, **kw):
    s = UIState("W5DMH", "EN83", True, "miles", **kw)
    s.set_screen(screen)
    return s


# ── ShutdownGesture unit tests ────────────────────────────────────────


def test_gesture_initial_state_not_armed():
    """A brand-new gesture object is not armed and has no task."""
    s = _state()

    async def _cb():
        pass

    loop = asyncio.new_event_loop()
    try:
        g = ShutdownGesture(s, loop, _cb)
        assert g.is_armed() is False
        assert g.cancel(source="test") is False  # no-op cancel
    finally:
        loop.close()


def test_gesture_arm_returns_true_first_time_false_when_already_armed():
    """First arm() returns True, subsequent arm()s while running return False.

    Idempotent arming is essential: keyboard Fn+Q during a
    button-hold (or vice versa) must not produce two countdown
    tasks racing to call systemctl poweroff.
    """
    s = _state()

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        assert g.arm(source="first") is True
        assert g.is_armed() is True
        # Race: second arm shouldn't produce a second task.
        assert g.arm(source="second") is False
        assert g.is_armed() is True
        # Clean up the still-running countdown so the test loop closes.
        g.cancel(source="teardown")
        # Yield once so cancel propagates through the task.
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_gesture_cancel_rolls_ui_back():
    """Cancelling restores the screen the operator was on before arming."""
    s = _state(screen=Screen.HEARD)

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        assert s.snapshot().screen is Screen.HEARD
        g.arm(source="t")
        assert s.snapshot().screen is Screen.SHUTTING_DOWN
        g.cancel(source="t")
        await asyncio.sleep(0)  # let the cancelled task settle
        # UI should be back on HEARD — cancel_shutdown restores it.
        assert s.snapshot().screen is Screen.HEARD

    asyncio.run(_run())


def test_gesture_cancel_from_different_source_works():
    """Button release can cancel a keyboard-armed gesture and vice versa.

    The 'source' parameter is only for log diagnostics; the cancel
    contract is "if armed, cancel". No source-coupling.
    """
    s = _state()

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        g.arm(source="keyboard Fn+Q")
        assert g.is_armed() is True
        # Different source label cancels just fine.
        assert g.cancel(source="buttons") is True
        await asyncio.sleep(0)
        assert g.is_armed() is False

    asyncio.run(_run())


def test_gesture_completion_invokes_callback():
    """When the countdown runs to completion, the shutdown callback fires.

    We use a very short hold (0.05 s) so the test doesn't sleep
    for the full 5-second production duration.
    """
    s = _state()
    callback_calls: list[str] = []

    async def _cb():
        callback_calls.append("fired")

    async def _run():
        g = ShutdownGesture(
            s, asyncio.get_running_loop(), _cb,
            hold_seconds=0.05,
        )
        g.arm(source="test")
        # Wait for the countdown to complete plus a margin.
        await asyncio.sleep(0.20)
        assert g.is_armed() is False, "task should be done after completion"

    asyncio.run(_run())
    assert callback_calls == ["fired"], (
        f"callback should fire exactly once at completion, "
        f"got {callback_calls}"
    )


def test_gesture_cancel_before_completion_does_not_fire_callback():
    """Cancelling mid-countdown must NOT invoke the shutdown callback —
    that's the whole point of the cancel."""
    s = _state()
    callback_calls: list[str] = []

    async def _cb():
        callback_calls.append("fired")

    async def _run():
        g = ShutdownGesture(
            s, asyncio.get_running_loop(), _cb,
            hold_seconds=10.0,  # plenty of time to cancel
        )
        g.arm(source="t")
        await asyncio.sleep(0.05)
        g.cancel(source="t")
        # Wait longer than a tick to be sure the cancelled task settled
        # and isn't still about to fire.
        await asyncio.sleep(0.15)

    asyncio.run(_run())
    assert callback_calls == [], (
        f"cancelled gesture should NOT fire callback, got {callback_calls}"
    )


def test_gesture_progress_drains_during_countdown():
    """The shutdown_remaining frac should decrease from 1.0 toward 0.0
    as the countdown runs. Pin the contract that the renderer's
    progress-bar input is being updated."""
    s = _state()

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(
            s, asyncio.get_running_loop(), _cb,
            hold_seconds=0.30,
        )
        g.arm(source="t")
        # Immediately after arm, progress should be close to 1.0
        assert s.snapshot().shutdown_remaining > 0.9
        # Wait until partway through
        await asyncio.sleep(0.15)
        partial = s.snapshot().shutdown_remaining
        assert 0.2 < partial < 0.8, (
            f"midway shutdown_remaining should be ~0.5, got {partial}"
        )
        # Let it finish
        await asyncio.sleep(0.25)
        # After completion, gesture is not armed
        assert g.is_armed() is False

    asyncio.run(_run())


def test_gesture_default_hold_seconds_matches_build_spec():
    """The keyboard Fn+Q path holds for 3 seconds per microjs8
    Build Spec §6.3.2 (vs MiniJS8's 5 s for the both-buttons
    gesture). The shorter hold is deliberate: the CardputerZero
    has no tactile buttons; Fn+Q on the keyboard is more
    discoverable and less ambiguous than holding two physical
    buttons, so a shorter hold is comfortable. Pin the constant
    so a refactor doesn't silently change keyboard UX."""
    assert DEFAULT_SHUTDOWN_HOLD_S == 3.0


# ── Router Fn+Q integration ─────────────────────────────────────────


def _router_with_gesture(state, gesture):
    """Build an InputRouter wired to the gesture, no other callbacks needed."""
    return InputRouter(
        state,
        save_config=lambda *a, **kw: True,
        emergency_bypass=lambda: True,
        shutdown_gesture=gesture,
    )


def test_router_ctrl_x_arms_shutdown_gesture():
    """Pressing Fn+Q on any screen arms the gesture and switches the
    UI to the SHUTTING_DOWN screen. This is the keyboard parallel
    of the both-buttons-held hardware gesture."""
    s = _state(screen=Screen.HOME)

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        r = _router_with_gesture(s, g)
        assert s.snapshot().screen is Screen.HOME
        r.handle(KeyEvent(key=Key.FN_Q))
        assert s.snapshot().screen is Screen.SHUTTING_DOWN, (
            "Fn+Q should switch UI to SHUTTING_DOWN"
        )
        assert g.is_armed() is True
        # Teardown
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_router_ctrl_x_works_on_unconfigured_station():
    """Operators must be able to shut down regardless of station
    configuration state. Power-off is a life-cycle gesture, not a
    radio-operation gesture; it should never be gated by tx_allowed."""
    # Station with empty callsign → tx_allowed is False
    s = UIState("", "", False, "miles")
    s.set_screen(Screen.SETUP)
    assert s.snapshot().tx_allowed is False

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        r = _router_with_gesture(s, g)
        r.handle(KeyEvent(key=Key.FN_Q))
        assert g.is_armed() is True, (
            "Fn+Q should work even when tx_allowed=False — "
            "shutdown is a life-cycle action, not a TX action"
        )
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_router_esc_on_shutting_down_cancels_gesture():
    """The README and on-device behaviour both say: Esc cancels.

    From any screen state where the gesture is armed (which always
    means screen == SHUTTING_DOWN), Esc rolls back to the previous
    screen and stops the countdown.
    """
    s = _state(screen=Screen.HEARD)

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        r = _router_with_gesture(s, g)
        r.handle(KeyEvent(key=Key.FN_Q))
        assert s.snapshot().screen is Screen.SHUTTING_DOWN
        # Now Esc cancels
        r.handle(KeyEvent(key=Key.ESC))
        await asyncio.sleep(0)  # let the cancelled task settle
        assert g.is_armed() is False
        # UI rolled back to HEARD
        assert s.snapshot().screen is Screen.HEARD

    asyncio.run(_run())


def test_router_other_keys_on_shutting_down_are_ignored():
    """While the countdown is running, miscellaneous keystrokes must
    NOT cancel — only Esc/Ctrl-C do. This prevents an operator who
    happens to be typing when they fire Fn+Q from accidentally
    cancelling the shutdown they just requested via a stray keypress."""
    s = _state(screen=Screen.HOME)

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        r = _router_with_gesture(s, g)
        r.handle(KeyEvent(key=Key.FN_Q))
        assert g.is_armed() is True
        # A bunch of non-cancel keys
        for k in (Key.UP, Key.DOWN, Key.LEFT, Key.RIGHT, Key.TAB,
                  Key.SPACE, Key.ENTER):
            r.handle(KeyEvent(key=k))
            assert g.is_armed() is True, (
                f"key {k} should NOT cancel — only Esc/Ctrl-C do"
            )
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_router_ctrl_x_when_no_gesture_is_quiet_no_op():
    """If the router is constructed without a shutdown gesture (e.g.
    in a test fixture that doesn't exercise shutdown), Fn+Q is a
    quiet no-op rather than a crash. Defensive."""
    s = _state()
    r = InputRouter(
        s,
        save_config=lambda *a, **kw: True,
        emergency_bypass=lambda: True,
        # NO shutdown_gesture
    )
    # Should not raise
    r.handle(KeyEvent(key=Key.FN_Q))
    # And should not have switched screens
    assert s.snapshot().screen is Screen.HOME


def test_router_ctrl_x_idempotent_with_existing_button_arm():
    """If buttons already armed the shutdown, pressing keyboard Fn+Q
    is a no-op (returns False from the gesture). The shared gesture
    handles the deduplication, the router just calls arm() blindly."""
    s = _state(screen=Screen.HOME)

    async def _cb():
        pass

    async def _run():
        g = ShutdownGesture(s, asyncio.get_running_loop(), _cb,
                            hold_seconds=10.0)
        r = _router_with_gesture(s, g)
        # Simulate buttons arming first
        g.arm(source="buttons")
        assert g.is_armed() is True
        # Keyboard Fn+Q — should be quietly absorbed (no second task)
        r.handle(KeyEvent(key=Key.FN_Q))
        # Still armed (didn't crash, didn't duplicate)
        assert g.is_armed() is True
        g.cancel(source="teardown")
        await asyncio.sleep(0)

    asyncio.run(_run())
