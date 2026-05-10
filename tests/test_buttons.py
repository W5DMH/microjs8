"""Tests for microjs8.input.buttons.

We don't poke real GPIO. Instead we drive the watcher via fake
button objects that we control directly, so we can exercise the
shutdown gesture state machine deterministically.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from microjs8.input.buttons import (
    SHUTDOWN_HOLD_S,
    ButtonWatcher,
)
from microjs8.ui.state import Screen, UIState


class FakeButton:
    """Minimal stand-in for gpiozero.Button used by ButtonWatcher."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.is_pressed = False
        self.when_pressed = None
        self.when_released = None
        self.closed = False

    def press(self) -> None:
        self.is_pressed = True
        if self.when_pressed is not None:
            self.when_pressed()

    def release(self) -> None:
        self.is_pressed = False
        if self.when_released is not None:
            self.when_released()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def loop_state_buttons():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    state = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    top = FakeButton("top")
    bot = FakeButton("bottom")
    yield loop, state, top, bot
    loop.close()


# ── Single-press navigation ──────────────────────────────────────────


def test_short_press_top_advances_ring(loop_state_buttons):
    loop, state, top, bot = loop_state_buttons
    fired = asyncio.Event()
    watcher = ButtonWatcher(
        state, loop, shutdown_callback=_noop_shutdown(fired),
        button_top=top, button_bottom=bot,
    )
    watcher.start()

    top.press()
    top.release()
    loop.run_until_complete(asyncio.sleep(0))

    assert state.snapshot().screen is Screen.HEARD


def test_short_press_bottom_retreats_ring(loop_state_buttons):
    loop, state, top, bot = loop_state_buttons
    fired = asyncio.Event()
    watcher = ButtonWatcher(
        state, loop, shutdown_callback=_noop_shutdown(fired),
        button_top=top, button_bottom=bot,
    )
    watcher.start()

    bot.press()
    bot.release()
    loop.run_until_complete(asyncio.sleep(0))

    # Wraps from HOME back to the last ring screen.
    from microjs8.ui.state import RING
    assert state.snapshot().screen is RING[-1]


# ── Shutdown gesture ─────────────────────────────────────────────────


def test_both_held_arms_shutdown_screen(loop_state_buttons):
    loop, state, top, bot = loop_state_buttons
    fired = asyncio.Event()
    watcher = ButtonWatcher(
        state, loop, shutdown_callback=_noop_shutdown(fired),
        button_top=top, button_bottom=bot,
    )
    watcher.start()

    top.press()
    bot.press()
    loop.run_until_complete(asyncio.sleep(0))  # let scheduled callbacks run

    assert state.snapshot().screen is Screen.SHUTTING_DOWN


def test_release_before_hold_cancels_shutdown(loop_state_buttons):
    loop, state, top, bot = loop_state_buttons
    fired = asyncio.Event()
    watcher = ButtonWatcher(
        state, loop, shutdown_callback=_noop_shutdown(fired),
        button_top=top, button_bottom=bot,
    )
    watcher.start()

    top.press()
    bot.press()
    loop.run_until_complete(asyncio.sleep(0.1))   # countdown ticking
    assert state.snapshot().screen is Screen.SHUTTING_DOWN

    # Release one button — must cancel.
    top.release()
    loop.run_until_complete(asyncio.sleep(0))
    assert state.snapshot().screen is Screen.HOME
    assert not fired.is_set()


def test_full_hold_fires_shutdown_callback(loop_state_buttons):
    loop, state, top, bot = loop_state_buttons
    fired = asyncio.Event()
    watcher = ButtonWatcher(
        state, loop, shutdown_callback=_noop_shutdown(fired),
        button_top=top, button_bottom=bot,
    )
    watcher.start()

    top.press()
    bot.press()
    # Let the countdown finish. SHUTDOWN_HOLD_S + slack.
    loop.run_until_complete(asyncio.sleep(SHUTDOWN_HOLD_S + 0.5))

    assert fired.is_set()


def test_short_press_after_cancelled_shutdown_still_navigates(loop_state_buttons):
    """After a both-held cancel, a subsequent single-button press
    must still produce normal ring navigation."""
    loop, state, top, bot = loop_state_buttons
    fired = asyncio.Event()
    watcher = ButtonWatcher(
        state, loop, shutdown_callback=_noop_shutdown(fired),
        button_top=top, button_bottom=bot,
    )
    watcher.start()

    top.press()
    bot.press()
    loop.run_until_complete(asyncio.sleep(0.05))
    top.release()
    loop.run_until_complete(asyncio.sleep(0))
    bot.release()
    loop.run_until_complete(asyncio.sleep(0))
    # We're back at HOME. Now do a clean single press.
    top.press()
    top.release()
    loop.run_until_complete(asyncio.sleep(0))
    assert state.snapshot().screen is Screen.HEARD


def test_stop_releases_buttons(loop_state_buttons):
    loop, state, top, bot = loop_state_buttons
    fired = asyncio.Event()
    watcher = ButtonWatcher(
        state, loop, shutdown_callback=_noop_shutdown(fired),
        button_top=top, button_bottom=bot,
    )
    watcher.start()
    watcher.stop()
    assert top.closed
    assert bot.closed


# ── Test helpers ─────────────────────────────────────────────────────


def _noop_shutdown(event: asyncio.Event):
    """Return an async callback that sets `event` when called."""
    async def cb() -> None:
        event.set()
    return cb
