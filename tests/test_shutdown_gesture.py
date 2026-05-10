"""Tests for microjs8.input.shutdown_gesture.

The Phase 3 replacement for the MiniJS8 both-buttons gesture. We don't
poke real keyboard input — the gesture's API is ``arm()`` / ``cancel()``,
which the router calls in response to ``Fn+Q`` press / release events.
We exercise the state machine directly, which keeps the tests fast and
deterministic regardless of the underlying asyncio scheduler.

These tests intentionally mirror the structure of the prior
``test_buttons.py`` so coverage parity is obvious to anyone diffing the
two phases.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

import pytest

from microjs8.input.shutdown_gesture import (
    SHUTDOWN_HOLD_S,
    ShutdownGesture,
)
from microjs8.ui.state import Screen, UIState


def _noop_shutdown(fired: asyncio.Event) -> Callable[[], Awaitable[None]]:
    """Build a fake shutdown callback that just sets a flag.

    Mirrors the prior test_buttons helper so behaviour parity is easy
    to audit.
    """
    async def cb() -> None:
        fired.set()
    return cb


@pytest.fixture
def loop_state():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    state = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    yield loop, state
    # Cancel anything still pending — tests that arm() the gesture
    # without awaiting completion would otherwise leak a running task
    # and trigger pytest's unawaited-coroutine warning.
    pending = asyncio.all_tasks(loop)
    for t in pending:
        t.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()


# ── Arm + UI state ────────────────────────────────────────────────────


def test_arm_switches_to_shutting_down_screen(loop_state):
    """Fn+Q press must immediately switch to the SHUTTING_DOWN screen."""
    loop, state = loop_state
    fired = asyncio.Event()
    gesture = ShutdownGesture(state, loop, shutdown_callback=_noop_shutdown(fired))

    gesture.arm()
    loop.run_until_complete(asyncio.sleep(0))   # let the task tick once

    assert state.snapshot().screen is Screen.SHUTTING_DOWN
    assert gesture.is_armed
    assert not fired.is_set()


def test_arm_is_idempotent(loop_state):
    """A second arm() while already armed must NOT restart the timer.

    This matters for keyboard auto-repeat: if the kernel emits a stream
    of key_hold events during a 3-second hold, the gesture's countdown
    must keep ticking from the original press, not reset.
    """
    loop, state = loop_state
    fired = asyncio.Event()
    gesture = ShutdownGesture(state, loop, shutdown_callback=_noop_shutdown(fired))

    gesture.arm()
    first_task = gesture._task
    gesture.arm()
    assert gesture._task is first_task    # same object — not replaced


# ── Cancel paths ──────────────────────────────────────────────────────


def test_cancel_before_hold_completes_rolls_back_ui(loop_state):
    """Fn+Q release before 3s elapses cancels the shutdown."""
    loop, state = loop_state
    fired = asyncio.Event()
    gesture = ShutdownGesture(state, loop, shutdown_callback=_noop_shutdown(fired))

    gesture.arm()
    loop.run_until_complete(asyncio.sleep(0.1))    # let the countdown tick a bit
    assert state.snapshot().screen is Screen.SHUTTING_DOWN

    gesture.cancel()
    loop.run_until_complete(asyncio.sleep(0))

    assert state.snapshot().screen is Screen.HOME
    assert not gesture.is_armed
    assert not fired.is_set()


def test_cancel_when_not_armed_is_a_noop(loop_state):
    """Calling cancel() with no active gesture must not raise or
    mutate state."""
    loop, state = loop_state
    fired = asyncio.Event()
    gesture = ShutdownGesture(state, loop, shutdown_callback=_noop_shutdown(fired))

    # Unsolicited cancel — operator releases Fn+Q without ever pressing,
    # which can happen if Fn+Q press is debounced/dropped by the kernel
    # but the release still fires. Must be a no-op.
    gesture.cancel()
    assert state.snapshot().screen is Screen.HOME
    assert not fired.is_set()


# ── Full-hold success path ────────────────────────────────────────────


def test_full_hold_invokes_shutdown_callback(loop_state):
    """Holding Fn+Q for the full SHUTDOWN_HOLD_S window fires the callback."""
    loop, state = loop_state
    fired = asyncio.Event()
    gesture = ShutdownGesture(state, loop, shutdown_callback=_noop_shutdown(fired))

    gesture.arm()
    # Let the countdown finish. Slack of 0.5s covers scheduler jitter
    # and the final await sleep tick that happens at completion.
    loop.run_until_complete(asyncio.sleep(SHUTDOWN_HOLD_S + 0.5))

    assert fired.is_set()
    assert not gesture.is_armed


# ── Re-arm after cancel ───────────────────────────────────────────────


def test_arm_again_after_cancel_works_cleanly(loop_state):
    """A cancelled gesture must not leave the state machine wedged.

    Equivalent to MiniJS8's
    test_short_press_after_cancelled_shutdown_still_navigates — verifies
    the gesture can fire correctly after a cancel.
    """
    loop, state = loop_state
    fired = asyncio.Event()
    gesture = ShutdownGesture(state, loop, shutdown_callback=_noop_shutdown(fired))

    gesture.arm()
    loop.run_until_complete(asyncio.sleep(0.05))
    gesture.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    assert state.snapshot().screen is Screen.HOME

    # Now re-arm and confirm the SHUTTING_DOWN screen comes up again.
    gesture.arm()
    loop.run_until_complete(asyncio.sleep(0))
    assert state.snapshot().screen is Screen.SHUTTING_DOWN
    assert gesture.is_armed


def test_stop_releases_pending_task(loop_state):
    """stop() must cancel any in-flight countdown for clean teardown."""
    loop, state = loop_state
    fired = asyncio.Event()
    gesture = ShutdownGesture(state, loop, shutdown_callback=_noop_shutdown(fired))

    gesture.arm()
    assert gesture.is_armed
    gesture.stop()
    loop.run_until_complete(asyncio.sleep(0))
    assert not gesture.is_armed


# ── Failing shutdown callback rolls UI back ───────────────────────────


def test_failing_shutdown_callback_rolls_ui_back(loop_state):
    """If the shutdown callback raises, the UI must NOT remain on the
    SHUTTING_DOWN screen indefinitely — the operator needs the device
    to be usable again.
    """
    loop, state = loop_state

    async def failing_cb() -> None:
        raise RuntimeError("simulated shutdown failure")

    gesture = ShutdownGesture(state, loop, shutdown_callback=failing_cb)

    gesture.arm()
    loop.run_until_complete(asyncio.sleep(SHUTDOWN_HOLD_S + 0.3))

    # After the callback raises, the gesture catches it and cancels
    # the SHUTTING_DOWN screen. We're back to HOME.
    assert state.snapshot().screen is Screen.HOME
