"""Tests for microjs8.input.keyboard.

We don't open real /dev/input/eventN — instead we use a fake evdev
device that yields scripted events. This validates the keymap +
modifier handling without needing actual evdev installed (it IS
installed via deps, but we want determinism).

The discovery / device-open path is exercised separately with a
filesystem mock.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from microjs8.input.events import Key, KeyEvent
from microjs8.input.keyboard import (
    KeyboardThread,
    find_keyboard_device,
)


# ── find_keyboard_device ────────────────────────────────────────────


def test_find_keyboard_device_prefers_by_id(tmp_path, monkeypatch):
    """Stable by-id symlink must win when present."""
    # Build a fake /dev/input/by-id/ in tmp.
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    target = tmp_path / "event0"
    target.touch()
    link = by_id / "usb-Vendor_Product-event-kbd"
    link.symlink_to(target)

    monkeypatch.setattr(
        "microjs8.input.keyboard.glob.glob",
        lambda pat: [str(link)] if "by-id" in pat else [],
    )
    found = find_keyboard_device()
    assert found == str(link)


def test_find_keyboard_device_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr("microjs8.input.keyboard.glob.glob", lambda pat: [])
    assert find_keyboard_device() is None


# ── KeyboardThread integration via fake device ─────────────────────


class _FakeEvent:
    """Stand-in for evdev event."""

    def __init__(self, ev_type: int, code: int, value: int) -> None:
        self.type = ev_type
        self.code = code
        self.value = value


class _FakeDevice:
    """Fake evdev.InputDevice that yields a scripted sequence.

    Implements the read()/fileno() shape that the keyboard thread
    now expects. We use a real pipe so select() works against it.
    """

    def __init__(self, events: list[_FakeEvent]) -> None:
        import os
        self.path = "/dev/input/event-fake"
        self._events = list(events)
        self.closed = False
        # Real pipe so select() returns "ready" once we write a byte
        # for each event we want to deliver.
        self._read_fd, self._write_fd = os.pipe()
        # Pre-write one byte per scripted event so select() sees them.
        os.write(self._write_fd, b"\x01" * len(events))

    def fileno(self) -> int:
        return self._read_fd

    def read(self):
        """Return all currently-pending scripted events."""
        import os
        # Drain the pipe to match the events we'll yield.
        try:
            n = os.read(self._read_fd, 4096)
        except BlockingIOError:
            n = b""
        # Yield as many events as we drained bytes.
        out = []
        for _ in range(len(n)):
            if not self._events:
                break
            out.append(self._events.pop(0))
        return out

    def close(self) -> None:
        import os
        if not self.closed:
            self.closed = True
            try:
                os.close(self._read_fd)
                os.close(self._write_fd)
            except OSError:
                pass

    def grab(self) -> None:
        pass

    def ungrab(self) -> None:
        pass


def _ev(t: int, c: int, v: int) -> _FakeEvent:
    return _FakeEvent(t, c, v)


@pytest.fixture
def evdev_codes():
    """Return the evdev.ecodes module.

    evdev is a hard runtime dependency declared in pyproject.toml; if
    it's missing, the dev environment is broken and we want a hard
    failure here rather than a silent skip that hides the problem.
    """
    from evdev import ecodes
    return ecodes


def _drive_kbd_thread(events: list[_FakeEvent]):
    """Run a KeyboardThread against a scripted event list, capture
    the events emitted into the asyncio loop."""
    captured: list[KeyEvent] = []

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def on_event(ev: KeyEvent) -> None:
        captured.append(ev)

    fake_device = _FakeDevice(events)
    delivered = {"count": 0}

    def factory():
        # Return device once, then None forever after so the thread
        # sleeps in the reconnect loop instead of grabbing again.
        if delivered["count"] == 0:
            delivered["count"] += 1
            return fake_device
        return None

    thread = KeyboardThread(loop, on_event, device_factory=factory)
    thread.start()

    # Pump the asyncio loop briefly so call_soon_threadsafe-scheduled
    # callbacks actually run. The thread itself wakes within 200 ms
    # after select() times out, so we give it 1 s total to deliver
    # all scripted events.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        loop.call_soon(loop.stop)
        loop.run_forever()
        # Once all scripted events are drained AND the device is
        # closed (thread has moved on to the reconnect loop with no
        # device), we're done.
        if delivered["count"] == 1 and len(fake_device._events) == 0:
            # Run the loop one more time to drain any pending callbacks.
            loop.call_soon(loop.stop)
            loop.run_forever()
            break
        time.sleep(0.05)

    thread.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "keyboard thread did not stop within 2s"
    fake_device.close()
    loop.close()
    return captured


def test_keyboard_emits_letter(evdev_codes):
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_K, 1),
        _ev(ec.EV_KEY, ec.KEY_K, 0),
    ]
    captured = _drive_kbd_thread(events)
    assert any(e.char == "k" for e in captured)


def test_keyboard_shift_uppercases_letter(evdev_codes):
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTSHIFT, 1),
        _ev(ec.EV_KEY, ec.KEY_K, 1),
        _ev(ec.EV_KEY, ec.KEY_K, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTSHIFT, 0),
    ]
    captured = _drive_kbd_thread(events)
    assert any(e.char == "K" for e in captured)


def test_keyboard_shift_digit_emits_symbol(evdev_codes):
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTSHIFT, 1),
        _ev(ec.EV_KEY, ec.KEY_2, 1),
        _ev(ec.EV_KEY, ec.KEY_2, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTSHIFT, 0),
    ]
    captured = _drive_kbd_thread(events)
    # Shift+2 = @
    assert any(e.char == "@" for e in captured)


def test_keyboard_function_keys(evdev_codes):
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFT, 1),
        _ev(ec.EV_KEY, ec.KEY_LEFT, 0),
        _ev(ec.EV_KEY, ec.KEY_ENTER, 1),
        _ev(ec.EV_KEY, ec.KEY_ENTER, 0),
        _ev(ec.EV_KEY, ec.KEY_TAB, 1),
        _ev(ec.EV_KEY, ec.KEY_TAB, 0),
        _ev(ec.EV_KEY, ec.KEY_ESC, 1),
        _ev(ec.EV_KEY, ec.KEY_ESC, 0),
    ]
    captured = _drive_kbd_thread(events)
    keys = [e.key for e in captured if e.key is not None]
    assert Key.LEFT in keys
    assert Key.ENTER in keys
    assert Key.TAB in keys
    assert Key.ESC in keys


def test_keyboard_ctrl_s_combination(evdev_codes):
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_S, 1),
        _ev(ec.EV_KEY, ec.KEY_S, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    captured = _drive_kbd_thread(events)
    keys = [e.key for e in captured if e.key is not None]
    assert Key.CTRL_S in keys
    # And no plain "s" was emitted.
    assert all(e.char != "s" for e in captured)


def test_keyboard_thread_stops_cleanly(evdev_codes):
    """No events scripted; thread should stop on stop() within 1s.

    Regression test: an earlier implementation used evdev's blocking
    read_loop() generator, which left the thread stuck in the kernel
    waiting for keystrokes that never came. Calling stop() didn't wake
    it, and the daemon's shutdown sequence reported "keyboard thread
    did not stop within 2s" every time. With select()-based polling,
    the thread checks the stop event every 200 ms.
    """
    captured: list[KeyEvent] = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # Provide a real device so we exercise the read path, not just
    # the no-device-found reconnect loop.
    fake_device = _FakeDevice([])
    served = {"n": 0}
    def factory():
        if served["n"] == 0:
            served["n"] += 1
            return fake_device
        return None
    thread = KeyboardThread(loop, captured.append, device_factory=factory)
    thread.start()
    time.sleep(0.3)  # let the thread enter the read loop
    t0 = time.monotonic()
    thread.stop()
    thread.join(timeout=1.0)
    elapsed = time.monotonic() - t0
    assert not thread.is_alive(), \
        f"keyboard thread did not stop within 1s (elapsed {elapsed:.2f}s)"
    assert elapsed < 1.0, f"stop() took too long: {elapsed:.2f}s"
    fake_device.close()
    loop.close()


def test_keyboard_thread_stops_when_no_device_found(evdev_codes):
    """Thread must also stop promptly when sleeping in reconnect loop."""
    captured: list[KeyEvent] = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    thread = KeyboardThread(
        loop, captured.append, device_factory=lambda: None,
    )
    thread.start()
    time.sleep(0.1)
    thread.stop()
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    loop.close()
