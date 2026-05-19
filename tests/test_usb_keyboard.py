"""Phase 16 tests: multi-device keyboard discovery + USB Ctrl+B remap.

Covers:
  - ``discover_keyboards()`` classification (TCA8418 vs USB by symlink name)
  - Empty-host edge case (no keyboards present)
  - Mixed-host case (TCA8418 + USB both present — CardputerZero with
    USB keyboard plugged in for bench typing)
  - ``find_keyboard_device()`` backward-compat preference order
  - ``KeyboardThread(source="usb")`` Ctrl+B remap to ``Key.FN_B``
  - ``KeyboardThread(source="usb")`` Ctrl+Q stays as ``Key.CTRL_Q``
    (Phase 11 ALLCALL navigation — Ctrl+Q is NOT remapped to FN_Q)
  - ``KeyboardThread(source="tca8418")`` does NOT remap Ctrl+B
  - Default ``source`` is ``"tca8418"`` (backward compat)

Reuses the ``_FakeDevice`` + ``_drive_kbd_thread`` pattern from
``test_keyboard.py`` for evdev-free determinism.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import pytest

from microjs8.input.events import Key, KeyEvent
from microjs8.input.keyboard import (
    KeyboardThread,
    discover_keyboards,
    find_keyboard_device,
    _classify_keyboard,
)


# ── _classify_keyboard ──────────────────────────────────────────────


def test_classify_usb_by_id_path():
    """USB by-id symlinks start with ``usb-`` per udev convention."""
    p = "/dev/input/by-id/usb-Logitech_USB_Receiver-event-kbd"
    assert _classify_keyboard(p) == "usb"


def test_classify_tca8418_by_id_path():
    """The CardputerZero's TCA8418 keypad gets a platform-prefixed
    or i2c-prefixed by-id symlink from the kernel input subsystem."""
    p = "/dev/input/by-id/platform-3f804000.i2c-tca8418-event-kbd"
    assert _classify_keyboard(p) == "tca8418"


def test_classify_tca8418_explicit_name():
    """Any by-id symlink that contains 'tca8418' classifies as TCA8418
    regardless of prefix — defends against udev rule variations across
    kernel versions."""
    p = "/dev/input/by-id/i2c-tca8418-keypad-event-kbd"
    assert _classify_keyboard(p) == "tca8418"


def test_classify_unknown_defaults_to_usb():
    """Unrecognised symlink (e.g., bluetooth keyboard, virtual evdev
    device) defaults to USB — safer because the Ctrl+B remap is a
    no-op unless the operator presses Ctrl+B."""
    p = "/dev/input/by-id/bluez-keyboard-event-kbd"
    assert _classify_keyboard(p) == "usb"


# ── discover_keyboards ──────────────────────────────────────────────


def test_discover_empty_returns_empty_list(monkeypatch):
    """No keyboards at all → empty list (daemon will log + exit)."""
    monkeypatch.setattr("microjs8.input.keyboard.glob.glob", lambda pat: [])
    assert discover_keyboards() == []


def test_discover_returns_usb_only(monkeypatch):
    """Bare Pi Zero 2 W with one USB keyboard plugged in."""
    paths = ["/dev/input/by-id/usb-Vendor_Product-event-kbd"]
    monkeypatch.setattr(
        "microjs8.input.keyboard.glob.glob",
        lambda pat: paths if "by-id" in pat else [],
    )
    result = discover_keyboards()
    assert result == [("usb", paths[0])]


def test_discover_returns_tca8418_only(monkeypatch):
    """CardputerZero with no USB keyboard attached."""
    paths = ["/dev/input/by-id/platform-tca8418-event-kbd"]
    monkeypatch.setattr(
        "microjs8.input.keyboard.glob.glob",
        lambda pat: paths if "by-id" in pat else [],
    )
    result = discover_keyboards()
    assert result == [("tca8418", paths[0])]


def test_discover_returns_both_sources(monkeypatch):
    """CardputerZero with a USB keyboard plugged in for bench typing.
    Both are returned, lexicographically sorted by path."""
    paths = [
        "/dev/input/by-id/platform-tca8418-event-kbd",
        "/dev/input/by-id/usb-Logitech-event-kbd",
    ]
    monkeypatch.setattr(
        "microjs8.input.keyboard.glob.glob",
        lambda pat: paths if "by-id" in pat else [],
    )
    result = discover_keyboards()
    assert len(result) == 2
    sources = {r[0] for r in result}
    assert sources == {"tca8418", "usb"}


def test_discover_sorts_by_path_for_determinism(monkeypatch):
    """Multiple USB keyboards return in sorted order so behaviour is
    reproducible across boots."""
    paths_unsorted = [
        "/dev/input/by-id/usb-Zebra-event-kbd",
        "/dev/input/by-id/usb-Apple-event-kbd",
        "/dev/input/by-id/usb-Logitech-event-kbd",
    ]
    monkeypatch.setattr(
        "microjs8.input.keyboard.glob.glob",
        lambda pat: paths_unsorted if "by-id" in pat else [],
    )
    result = discover_keyboards()
    paths_out = [r[1] for r in result]
    assert paths_out == sorted(paths_unsorted)


# ── find_keyboard_device backward-compat ────────────────────────────


def test_find_keyboard_device_prefers_tca8418_when_both_present(monkeypatch):
    """Legacy single-device API should prefer the TCA8418 when both
    sources are available — matches the prior CardputerZero behaviour
    (the on-board keypad was always the chosen device)."""
    paths = [
        "/dev/input/by-id/usb-Vendor-event-kbd",
        "/dev/input/by-id/platform-tca8418-event-kbd",
    ]
    monkeypatch.setattr(
        "microjs8.input.keyboard.glob.glob",
        lambda pat: paths if "by-id" in pat else [],
    )
    found = find_keyboard_device()
    assert found is not None
    assert "tca8418" in found


def test_find_keyboard_device_returns_usb_when_only_usb(monkeypatch):
    """USB-only host (bare Pi Zero 2 W) — legacy API returns the USB
    path. Should keep working for any callers we missed in the
    refactor."""
    paths = ["/dev/input/by-id/usb-Vendor-event-kbd"]
    monkeypatch.setattr(
        "microjs8.input.keyboard.glob.glob",
        lambda pat: paths if "by-id" in pat else [],
    )
    found = find_keyboard_device()
    assert found == paths[0]


# ── KeyboardThread with source="usb" — Ctrl+B remap ────────────────


class _FakeEvent:
    """Stand-in for evdev event (same shape as test_keyboard.py)."""

    def __init__(self, ev_type: int, code: int, value: int) -> None:
        self.type = ev_type
        self.code = code
        self.value = value


class _FakeDevice:
    """Fake evdev.InputDevice yielding a scripted sequence.

    Same implementation as test_keyboard.py's _FakeDevice. Duplicated
    here intentionally to keep the test file self-contained — pytest
    collection works file-by-file, and a cross-file fixture would
    couple the two test files in a way that obscures intent.
    """

    def __init__(self, events: list[_FakeEvent]) -> None:
        self.path = "/dev/input/event-fake"
        self._events = list(events)
        self.closed = False
        self._read_fd, self._write_fd = os.pipe()
        os.write(self._write_fd, b"\x01" * len(events))

    def fileno(self) -> int:
        return self._read_fd

    def read(self):
        try:
            n = os.read(self._read_fd, 4096)
        except BlockingIOError:
            n = b""
        out = []
        for _ in range(len(n)):
            if not self._events:
                break
            out.append(self._events.pop(0))
        return out

    def close(self) -> None:
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
    from evdev import ecodes
    return ecodes


def _drive_kbd_thread(events: list[_FakeEvent], *, source: str = "tca8418"):
    """Drive a KeyboardThread with the given source tag, return events."""
    captured: list[KeyEvent] = []

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def on_event(ev: KeyEvent) -> None:
        captured.append(ev)

    fake_device = _FakeDevice(events)
    delivered = {"count": 0}

    def factory():
        if delivered["count"] == 0:
            delivered["count"] += 1
            return fake_device
        return None

    thread = KeyboardThread(
        loop, on_event,
        device_factory=factory,
        source=source,
    )
    thread.start()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        loop.call_soon(loop.stop)
        loop.run_forever()
        if delivered["count"] == 1 and len(fake_device._events) == 0:
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


def test_usb_source_ctrl_b_emits_fn_b(evdev_codes):
    """On a USB keyboard, Ctrl+B must emit Key.FN_B so the backlight
    gesture is reachable without an Fn key. Press Ctrl, press B,
    release B, release Ctrl."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    captured = _drive_kbd_thread(events, source="usb")
    fn_b_events = [e for e in captured if e.key is Key.FN_B]
    assert len(fn_b_events) == 1


def test_usb_source_ctrl_q_stays_as_ctrl_q(evdev_codes):
    """On a USB keyboard, Ctrl+Q must NOT be remapped to FN_Q —
    the existing Phase 11 binding (Ctrl+Q = ALLCALL navigation)
    is preserved. USB-only shutdown goes through SSH +
    ``systemctl poweroff`` until a config-driven gesture lands."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_Q, 1),
        _ev(ec.EV_KEY, ec.KEY_Q, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    captured = _drive_kbd_thread(events, source="usb")
    ctrl_q_events = [e for e in captured if e.key is Key.CTRL_Q]
    fn_q_events = [e for e in captured if e.key is Key.FN_Q]
    assert len(ctrl_q_events) == 1
    assert len(fn_q_events) == 0


def test_tca8418_source_ctrl_b_stays_as_ctrl_letter(evdev_codes):
    """On the TCA8418, Ctrl+B should NOT be remapped — the on-board
    Fn key produces the actual FN_B scancode via the kernel keymap.
    Remapping Ctrl+B on TCA8418 would steal a chord the operator
    might use for something else later."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    captured = _drive_kbd_thread(events, source="tca8418")
    fn_b_events = [e for e in captured if e.key is Key.FN_B]
    # No FN_B should be emitted from Ctrl+B on TCA8418. (And no
    # CTRL_B mapping exists in _CTRL_KEYS, so this combo is a no-op.)
    assert len(fn_b_events) == 0


def test_tca8418_source_ctrl_q_emits_ctrl_q(evdev_codes):
    """TCA8418 keeps the Phase 11 Ctrl+Q = ALLCALL hotkey."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_Q, 1),
        _ev(ec.EV_KEY, ec.KEY_Q, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    captured = _drive_kbd_thread(events, source="tca8418")
    ctrl_q_events = [e for e in captured if e.key is Key.CTRL_Q]
    assert len(ctrl_q_events) == 1


def test_usb_source_plain_b_emits_char_b_not_fn_b(evdev_codes):
    """Without Ctrl held, B emits a plain 'b' character on USB.
    The Ctrl+B remap is gated on _ctrl_held."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_B, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 0),
    ]
    captured = _drive_kbd_thread(events, source="usb")
    b_chars = [e for e in captured if e.char == "b"]
    fn_b_events = [e for e in captured if e.key is Key.FN_B]
    assert len(b_chars) == 1
    assert len(fn_b_events) == 0


def test_usb_source_ctrl_h_still_emits_ctrl_h(evdev_codes):
    """Other Ctrl+letter bindings (Ctrl+H, Ctrl+S, Ctrl+C) must
    continue to work on USB — the remap is targeted at Ctrl+B only,
    not a blanket override of the _CTRL_KEYS table."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_H, 1),
        _ev(ec.EV_KEY, ec.KEY_H, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    captured = _drive_kbd_thread(events, source="usb")
    ctrl_h_events = [e for e in captured if e.key is Key.CTRL_H]
    assert len(ctrl_h_events) == 1


def test_usb_source_ctrl_c_still_emits_ctrl_c(evdev_codes):
    """Ctrl+C must keep working on USB — it's the SHUTTING_DOWN
    cancel hotkey (Phase 15) in addition to its common 'abort'
    meaning. Important to verify it's not accidentally swallowed
    by the new remap logic."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_C, 1),
        _ev(ec.EV_KEY, ec.KEY_C, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    captured = _drive_kbd_thread(events, source="usb")
    ctrl_c_events = [e for e in captured if e.key is Key.CTRL_C]
    assert len(ctrl_c_events) == 1


def test_default_source_is_tca8418_for_backward_compat(evdev_codes):
    """KeyboardThread without source kwarg must default to 'tca8418'
    so existing callers (any we missed in app.py wiring) keep the
    Phase 3 behaviour. Verified by checking that Ctrl+B doesn't
    remap to FN_B in the default case."""
    ec = evdev_codes
    events = [
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]
    # Call with no source kwarg.
    captured: list[KeyEvent] = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    fake_device = _FakeDevice(events)
    delivered = {"count": 0}

    def factory():
        if delivered["count"] == 0:
            delivered["count"] += 1
            return fake_device
        return None

    thread = KeyboardThread(
        loop, captured.append, device_factory=factory,
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        loop.call_soon(loop.stop)
        loop.run_forever()
        if delivered["count"] == 1 and len(fake_device._events) == 0:
            loop.call_soon(loop.stop)
            loop.run_forever()
            break
        time.sleep(0.05)
    thread.stop()
    thread.join(timeout=2.0)
    fake_device.close()
    loop.close()

    fn_b_events = [e for e in captured if e.key is Key.FN_B]
    # Default = tca8418 → no Ctrl+B remap → no FN_B emitted.
    assert len(fn_b_events) == 0


# ── Coexistence smoke test ──────────────────────────────────────────
#
# Two threads, two source tags, one shared callback — events from
# both arrive at the same callback in interleaved order. This is the
# CardputerZero + USB-bench-keyboard scenario.


def test_two_keyboard_threads_share_callback(evdev_codes):
    """Two KeyboardThreads with different source tags can drive the
    same on_event callback without stepping on each other.

    Verifies the architectural promise of Phase 16: the router sees
    a single unified KeyEvent stream regardless of which physical
    keyboard produced it.
    """
    ec = evdev_codes

    # TCA8418 keyboard emits 'a'
    tca_events = [
        _ev(ec.EV_KEY, ec.KEY_A, 1),
        _ev(ec.EV_KEY, ec.KEY_A, 0),
    ]
    # USB keyboard emits 'z' AND Ctrl+B (should remap to FN_B)
    usb_events = [
        _ev(ec.EV_KEY, ec.KEY_Z, 1),
        _ev(ec.EV_KEY, ec.KEY_Z, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 1),
        _ev(ec.EV_KEY, ec.KEY_B, 0),
        _ev(ec.EV_KEY, ec.KEY_LEFTCTRL, 0),
    ]

    captured: list[KeyEvent] = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tca_dev = _FakeDevice(tca_events)
    usb_dev = _FakeDevice(usb_events)
    tca_delivered = {"count": 0}
    usb_delivered = {"count": 0}

    def tca_factory():
        if tca_delivered["count"] == 0:
            tca_delivered["count"] += 1
            return tca_dev
        return None

    def usb_factory():
        if usb_delivered["count"] == 0:
            usb_delivered["count"] += 1
            return usb_dev
        return None

    tca_thread = KeyboardThread(
        loop, captured.append,
        device_factory=tca_factory, name="kbd-tca", source="tca8418",
    )
    usb_thread = KeyboardThread(
        loop, captured.append,
        device_factory=usb_factory, name="kbd-usb", source="usb",
    )
    tca_thread.start()
    usb_thread.start()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        loop.call_soon(loop.stop)
        loop.run_forever()
        if (tca_delivered["count"] == 1 and usb_delivered["count"] == 1
                and not tca_dev._events and not usb_dev._events):
            loop.call_soon(loop.stop)
            loop.run_forever()
            break
        time.sleep(0.05)

    tca_thread.stop()
    usb_thread.stop()
    tca_thread.join(timeout=2.0)
    usb_thread.join(timeout=2.0)
    tca_dev.close()
    usb_dev.close()
    loop.close()

    # All three events should appear, regardless of order.
    chars = {e.char for e in captured if e.char}
    keys = {e.key for e in captured if e.key}
    assert "a" in chars  # from TCA8418
    assert "z" in chars  # from USB
    assert Key.FN_B in keys  # Ctrl+B from USB → FN_B remap
