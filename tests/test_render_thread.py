"""Tests for microjs8.ui.display.RenderThread.

These exercise the actual RenderThread (not just the fake device) so we
catch issues like the _stop / threading.Thread._stop attribute collision.
We use the FakeDisplayDevice so no real SPI traffic is needed.
"""

from __future__ import annotations

import threading
import time

import pytest

from microjs8.ui.display import FakeDisplayDevice, RenderThread
from microjs8.ui.fonts import load_fonts
from microjs8.ui.state import Screen, UIState


@pytest.fixture(scope="module")
def fonts():
    return load_fonts()


def test_render_thread_starts_and_stops_cleanly(fonts):
    """Catch attribute-shadowing of threading.Thread internals.

    Specifically, naming our shutdown flag `self._stop` would shadow
    threading.Thread._stop and cause join() to raise:
        TypeError: 'Event' object is not callable
    This test would catch that even on a host with no real GPIO/SPI.
    """
    device = FakeDisplayDevice()
    state = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)

    thread = RenderThread(device, state, fonts)
    thread.start()

    # Wait until the initial dirty render has produced at least one frame.
    deadline = time.monotonic() + 2.0
    while not device.frames and time.monotonic() < deadline:
        time.sleep(0.02)
    assert device.frames, "render thread did not produce a frame within 2s"

    # Mutate state — confirm the thread picks up the change.
    state.advance_ring()
    deadline = time.monotonic() + 2.0
    initial_count = len(device.frames)
    while len(device.frames) == initial_count and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(device.frames) > initial_count

    thread.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "render thread did not stop within 2s"


def test_render_thread_join_works():
    """Direct regression test for the _stop / Thread._stop collision.

    Even with no frames rendered, .join() must succeed without raising.
    """
    device = FakeDisplayDevice()
    state = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    thread = RenderThread(device, state, fonts=load_fonts())
    thread.start()
    thread.stop()
    # Without the fix, this raises 'Event' object is not callable.
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_render_thread_multiple_stops_safe(fonts):
    """stop() is idempotent."""
    device = FakeDisplayDevice()
    state = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    thread = RenderThread(device, state, fonts)
    thread.start()
    thread.stop()
    thread.stop()
    thread.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
