"""Tests for the UART keyboard backend.

The parser is a pure function (``parse_line``) so most coverage lives
in straight-line tests with golden inputs. The thread loop is exercised
via a fake serial port that hands out pre-baked byte chunks.

What we explicitly test:
  - Every named-key event (ENTER, TAB, ESC, BACKSPACE, UP/DOWN/LEFT/RIGHT)
    parses to the correct ``Key`` enum.
  - ``CHAR:X`` produces a ``KeyEvent`` with ``char=X`` for every
    printable ASCII range (32-126).
  - Wire-malformed lines (empty, comment, truncated CHAR, unknown name,
    non-printable CHAR, trailing whitespace) all return ``None``
    rather than raising.
  - The thread loop reads from a fake port, parses, and dispatches
    via the on_event callback exactly once per keypress.
  - The thread exits cleanly when ``stop`` is called.
  - A buggy callback that raises doesn't kill the thread.
"""

from __future__ import annotations

import io
import threading
import time
from typing import List, Optional
from unittest.mock import patch

import pytest

from microjs8.input.events import Key, KeyEvent
from microjs8.input.uart_keyboard import (
    UartKeyboardThread,
    parse_line,
)


# ── parse_line ────────────────────────────────────────────────────────


class TestParseLineNamedKeys:
    """Each named event maps to the right Key enum value."""

    @pytest.mark.parametrize(
        "wire,expected",
        [
            ("ENTER",     Key.ENTER),
            ("TAB",       Key.TAB),
            ("ESC",       Key.ESC),
            ("BACKSPACE", Key.BACKSPACE),
            ("UP",        Key.UP),
            ("DOWN",      Key.DOWN),
            ("LEFT",      Key.LEFT),
            ("RIGHT",     Key.RIGHT),
        ],
    )
    def test_named_event(self, wire: str, expected: Key) -> None:
        ev = parse_line(wire)
        assert ev is not None
        assert ev.key is expected
        assert ev.char is None

    def test_trailing_whitespace_ignored(self) -> None:
        # The firmware terminates with \n but we strip in parse_line;
        # tolerate accidental whitespace on either end as well.
        assert parse_line("  ENTER  ") == KeyEvent(key=Key.ENTER)
        assert parse_line("ENTER\r") == KeyEvent(key=Key.ENTER)
        assert parse_line("\tENTER\t") == KeyEvent(key=Key.ENTER)

    def test_case_sensitive(self) -> None:
        # Wire format is uppercase; lowercase isn't an alias.
        assert parse_line("enter") is None
        assert parse_line("Enter") is None


class TestParseLineChar:
    """CHAR:X printable handling across the full ASCII printable range."""

    @pytest.mark.parametrize("ch", [chr(c) for c in range(0x20, 0x7F)])
    def test_every_printable_ascii(self, ch: str) -> None:
        ev = parse_line(f"CHAR:{ch}")
        assert ev is not None
        assert ev.char == ch
        assert ev.key is None

    def test_letter_case_preserved(self) -> None:
        # The firmware pre-resolves Shift so 'K' means shift+k was
        # pressed — the Pi side just takes the byte as-is.
        assert parse_line("CHAR:k") == KeyEvent(char="k")
        assert parse_line("CHAR:K") == KeyEvent(char="K")

    def test_digits_and_shifted_symbols(self) -> None:
        assert parse_line("CHAR:7") == KeyEvent(char="7")
        assert parse_line("CHAR:&") == KeyEvent(char="&")
        assert parse_line("CHAR:!") == KeyEvent(char="!")
        assert parse_line("CHAR:?") == KeyEvent(char="?")

    def test_space_is_printable(self) -> None:
        # Space is character 0x20 — bottom of the printable range.
        assert parse_line("CHAR: ") == KeyEvent(char=" ")

    def test_punctuation_for_compose(self) -> None:
        # These are the chars JS8 operators use in COMPOSE messages.
        for ch in ".,;:'\"-_+=()[]{}<>/\\?!@#$%^&*":
            assert parse_line(f"CHAR:{ch}") == KeyEvent(char=ch), ch


class TestParseLineMalformed:
    """Defensive: every flavor of malformed input returns None, never raises."""

    def test_empty_string(self) -> None:
        assert parse_line("") is None

    def test_only_whitespace(self) -> None:
        assert parse_line("   ") is None
        assert parse_line("\n") is None
        assert parse_line("\r\n") is None

    def test_comment(self) -> None:
        assert parse_line("# microjs8-cardputer-link v0.1.0 ready") is None
        assert parse_line("#") is None
        assert parse_line("#anything at all") is None

    def test_unknown_named_event(self) -> None:
        assert parse_line("SPACE") is None  # not in our wire format
        assert parse_line("FOO") is None
        assert parse_line("CTRL_C") is None

    def test_char_truncated(self) -> None:
        # "CHAR:" with no character or partial format — 6-byte invariant.
        assert parse_line("CHAR:") is None
        assert parse_line("CHAR") is None
        assert parse_line("CHAR:ab") is None  # multi-char

    def test_char_with_control_byte(self) -> None:
        # Defense against noise on the wire: don't emit ESC (0x1b),
        # TAB (0x09), bell (0x07), DEL (0x7f), etc.
        assert parse_line(f"CHAR:{chr(0x09)}") is None
        assert parse_line(f"CHAR:{chr(0x1b)}") is None
        assert parse_line(f"CHAR:{chr(0x7f)}") is None
        assert parse_line(f"CHAR:{chr(0x00)}") is None

    def test_char_with_high_byte(self) -> None:
        # Anything above 0x7E is dropped (8-bit / UTF-8 garbage).
        assert parse_line(f"CHAR:{chr(0x80)}") is None

    def test_arbitrary_garbage(self) -> None:
        assert parse_line("xyzzy") is None
        assert parse_line("12345") is None
        assert parse_line("CHAR:K extra") is None  # CHAR line must be exactly 6


# ── UartKeyboardThread ────────────────────────────────────────────────


class FakeSerial:
    """Minimal pyserial.Serial stand-in for thread tests.

    Hands out pre-baked byte chunks one read() at a time so we can
    inject controlled traffic without a real device. Returns b"" once
    the script is exhausted so the thread will keep looping (and we
    test stop() cleanly terminates it).
    """

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        # Simulate the read timeout returning empty.
        time.sleep(0.01)
        return b""

    def close(self) -> None:
        self.closed = True


def _run_thread_against(
    chunks: List[bytes],
    *,
    timeout: float = 1.0,
    on_event: Optional[callable] = None,  # type: ignore[name-defined]
) -> List[KeyEvent]:
    """Drive a UartKeyboardThread against a FakeSerial, return collected events.

    Default on_event accumulates into a list. Caller can override for
    tests that want a callback that raises (to verify thread survives).
    """
    fake = FakeSerial(chunks)
    collected: List[KeyEvent] = []

    def default_cb(ev: KeyEvent) -> None:
        collected.append(ev)

    cb = on_event if on_event is not None else default_cb

    thread = UartKeyboardThread(
        device="/dev/null",  # ignored; we monkey-patch the Serial ctor
        baud=115200,
        on_event=cb,
    )

    # Patch pyserial.Serial() inside the thread's run() so when it
    # imports + instantiates serial.Serial(...) we return our fake.
    fake_module = type("FakeSerialModule", (), {})()
    fake_module.Serial = lambda *a, **kw: fake  # type: ignore[attr-defined]
    fake_module.EIGHTBITS = 8
    fake_module.PARITY_NONE = "N"
    fake_module.STOPBITS_ONE = 1

    with patch.dict("sys.modules", {"serial": fake_module}):
        thread.start()
        # Wait for the thread to consume the scripted chunks.
        deadline = time.monotonic() + timeout
        expected_min = sum(c.count(b"\n") for c in chunks if c)
        while time.monotonic() < deadline and len(collected) < expected_min:
            time.sleep(0.01)
        thread.stop()
        thread.join(timeout=1.0)

    assert not thread.is_alive(), "thread did not exit after stop()"
    return collected


class TestThreadLoop:
    """The thread reads from the port, parses, dispatches via callback."""

    def test_single_event(self) -> None:
        events = _run_thread_against([b"ENTER\n"])
        assert events == [KeyEvent(key=Key.ENTER)]

    def test_multiple_events_one_chunk(self) -> None:
        events = _run_thread_against([b"CHAR:k\nCHAR:i\nENTER\n"])
        assert events == [
            KeyEvent(char="k"),
            KeyEvent(char="i"),
            KeyEvent(key=Key.ENTER),
        ]

    def test_event_split_across_chunks(self) -> None:
        # Realistic case: UART read() returns whatever bytes are in
        # the kernel buffer, which may split a single event line.
        events = _run_thread_against([b"CHA", b"R:k\nEN", b"TER\n"])
        assert events == [
            KeyEvent(char="k"),
            KeyEvent(key=Key.ENTER),
        ]

    def test_comment_filtered(self) -> None:
        events = _run_thread_against([
            b"# microjs8-cardputer-link v0.1.0 ready\n",
            b"CHAR:a\n",
        ])
        assert events == [KeyEvent(char="a")]

    def test_unparseable_lines_dropped(self) -> None:
        # Mixed valid + invalid; only valid should reach the callback.
        events = _run_thread_against([
            b"GARBAGE\nCHAR:k\nfoo\nENTER\nCHAR:\n",
        ])
        assert events == [KeyEvent(char="k"), KeyEvent(key=Key.ENTER)]

    def test_buggy_callback_does_not_kill_thread(self) -> None:
        # If the consumer's on_event raises, we should log+continue
        # rather than terminate. Subsequent events still flow.
        call_count = [0]

        def buggy(ev: KeyEvent) -> None:
            call_count[0] += 1
            if ev.key is Key.UP:
                raise RuntimeError("simulated router crash")

        _run_thread_against(
            [b"CHAR:a\nUP\nCHAR:b\n"],
            on_event=buggy,
        )
        # All three events should have reached the callback, even
        # though the middle one raised.
        assert call_count[0] == 3


class TestThreadStartupResilience:
    """Failures during start() are logged but don't crash the daemon."""

    def test_pyserial_missing(self, caplog) -> None:
        # If pyserial isn't installed, the thread logs and exits;
        # the daemon proceeds without keyboard input rather than
        # crashing at startup.
        thread = UartKeyboardThread(device="/dev/null", baud=115200)
        with patch.dict("sys.modules", {"serial": None}):
            # Force import to fail by clearing the module entry.
            import sys
            sys.modules.pop("serial", None)
            with patch(
                "builtins.__import__",
                side_effect=ImportError("pyserial not installed"),
            ):
                thread.start()
                thread.join(timeout=1.0)
        assert not thread.is_alive()

    def test_device_open_failure(self, caplog) -> None:
        # If the device file doesn't exist or isn't a UART, log+exit.
        # The thread should NOT take the daemon down with it.
        thread = UartKeyboardThread(
            device="/nonexistent/device", baud=115200,
        )

        class FailingSerial:
            def __init__(self, *a, **kw):
                raise OSError("device not found")

        fake_module = type("FakeSerialModule", (), {})()
        fake_module.Serial = FailingSerial
        fake_module.EIGHTBITS = 8
        fake_module.PARITY_NONE = "N"
        fake_module.STOPBITS_ONE = 1

        with patch.dict("sys.modules", {"serial": fake_module}):
            thread.start()
            thread.join(timeout=1.0)
        assert not thread.is_alive()


class TestThreadStopBehavior:
    """stop() makes the thread exit promptly and idempotently."""

    def test_stop_when_idle(self) -> None:
        # No data ever arrives; stop() should still terminate the
        # thread within a couple of read timeouts.
        events = _run_thread_against([b""], timeout=2.0)
        assert events == []

    def test_stop_is_idempotent(self) -> None:
        thread = UartKeyboardThread(device="/dev/null", baud=115200)
        thread.stop()
        thread.stop()  # second call should not raise
        thread.stop()
