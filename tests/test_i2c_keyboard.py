"""Tests for the I2C keyboard backend (v0.0.16).

Verifies:
  - decode_byte() correctly maps every byte we observed during
    PI-2W-TEST hardware bring-up plus a representative sample of
    bytes that should be silently dropped.
  - The I2cKeyboardThread defers I/O to run() (so missing hardware
    doesn't crash app startup) and handles import/open/read failures
    gracefully.

The thread-level tests mock smbus2.SMBus so we don't need real I2C
hardware (and the tests run on any CI host).

ASCII-only policy enforced per the v0.0.14 paste-encoding incident.
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from microjs8.input.events import Key, KeyEvent
from microjs8.input.i2c_keyboard import (
    DEFAULT_ADDRESS,
    DEFAULT_BUS,
    DEFAULT_POLL_INTERVAL_S,
    I2cKeyboardThread,
    decode_byte,
)


# --- decode_byte() pure tests ----------------------------------------


class TestDecodeByteIdle:
    def test_zero_returns_none(self) -> None:
        # The CardKB returns 0x00 between key presses (idle).
        # decode_byte must drop these silently -- they happen 33
        # times per second and would flood the router otherwise.
        assert decode_byte(0x00) is None


class TestDecodeByteControl:
    """Standard ASCII control bytes that map to named keys."""

    def test_backspace(self) -> None:
        event = decode_byte(0x08)
        assert event is not None
        assert event.key == Key.BACKSPACE
        # Confirmed via PI-2W-TEST hardware bring-up.

    def test_tab(self) -> None:
        event = decode_byte(0x09)
        assert event is not None
        assert event.key == Key.TAB

    def test_enter(self) -> None:
        event = decode_byte(0x0D)
        assert event is not None
        assert event.key == Key.ENTER

    def test_esc(self) -> None:
        event = decode_byte(0x1B)
        assert event is not None
        assert event.key == Key.ESC


class TestDecodeBytePrintable:
    """Printable ASCII (0x20-0x7E) maps to KeyEvent(char=...)."""

    def test_space(self) -> None:
        event = decode_byte(0x20)
        assert event is not None
        assert event.char == " "

    def test_lowercase_a(self) -> None:
        event = decode_byte(0x61)
        assert event is not None
        assert event.char == "a"

    def test_uppercase_a(self) -> None:
        event = decode_byte(0x41)
        assert event is not None
        assert event.char == "A"

    def test_digit_zero(self) -> None:
        event = decode_byte(0x30)
        assert event is not None
        assert event.char == "0"

    def test_digit_nine(self) -> None:
        event = decode_byte(0x39)
        assert event is not None
        assert event.char == "9"

    def test_semicolon(self) -> None:
        # Verified from PI-2W-TEST log: ; key returns 0x3B.
        event = decode_byte(0x3B)
        assert event is not None
        assert event.char == ";"

    def test_open_brace_via_sym_q(self) -> None:
        # PI-2W-TEST bring-up confirmed Sym+Q returns 0x7B = '{'.
        # The CardKB resolves Sym+key to the "second character" in
        # firmware -- we receive it as ordinary printable ASCII and
        # forward it unchanged.
        event = decode_byte(0x7B)
        assert event is not None
        assert event.char == "{"

    def test_tilde_top_of_range(self) -> None:
        # 0x7E is the upper bound of the printable range.
        event = decode_byte(0x7E)
        assert event is not None
        assert event.char == "~"


class TestDecodeByteArrows:
    """CardKB-specific arrow codes (0xB4-0xB7)."""

    def test_left(self) -> None:
        event = decode_byte(0xB4)
        assert event is not None
        assert event.key == Key.LEFT

    def test_up(self) -> None:
        # Confirmed via PI-2W-TEST: pressing Up arrow returned 0xB5.
        event = decode_byte(0xB5)
        assert event is not None
        assert event.key == Key.UP

    def test_down(self) -> None:
        # Confirmed via PI-2W-TEST: pressing Down arrow returned 0xB6.
        event = decode_byte(0xB6)
        assert event is not None
        assert event.key == Key.DOWN

    def test_right(self) -> None:
        event = decode_byte(0xB7)
        assert event is not None
        assert event.key == Key.RIGHT


class TestDecodeByteDropped:
    """Bytes outside the documented ranges drop silently."""

    @pytest.mark.parametrize("byte", [
        0x01,  # SOH -- not in our control map
        0x07,  # BEL -- not in our control map
        0x0A,  # LF -- the CardKB uses 0x0D for Enter, not LF
        0x0B,  # VT
        0x0C,  # FF
        0x1F,  # US -- last byte below printable range
        0x7F,  # DEL -- one above printable range upper bound
    ])
    def test_below_or_between_printable_dropped(self, byte: int) -> None:
        assert decode_byte(byte) is None

    @pytest.mark.parametrize("byte", [
        0x80,  # bottom of Fn+key range
        0x8D,  # confirmed Fn+Q from PI-2W-TEST hardware bring-up
        0x9F,  # top of Fn+key range
        0xA0,  # gap between Fn range and arrow range
        0xB3,  # one below first arrow code (LEFT = 0xB4)
        0xB8,  # one above last arrow code (RIGHT = 0xB7)
        0xC0,  # high range, unmapped
        0xFF,  # max byte, unmapped
    ])
    def test_unmapped_high_bytes_dropped(self, byte: int) -> None:
        # Fn+key codes (0x80-0x9F) and any other unmapped high bytes
        # are dropped silently rather than misinterpreted. The
        # decode_byte docstring documents this policy.
        assert decode_byte(byte) is None


# --- Thread lifecycle tests ------------------------------------------


class TestI2cKeyboardThreadInit:
    """__init__ defers I/O so missing hardware doesn't crash startup."""

    def test_init_does_not_open_bus(self) -> None:
        # Critical invariant: the constructor MUST NOT touch the I2C
        # bus. App startup constructs the thread before knowing
        # whether the device is present; opening at __init__ would
        # raise on every Pi without a CardKB attached, killing
        # auto-mode plug-and-play behavior.
        thread = I2cKeyboardThread()
        assert thread._bus is None

    def test_init_stores_defaults(self) -> None:
        thread = I2cKeyboardThread()
        assert thread._bus_num == DEFAULT_BUS
        assert thread._address == DEFAULT_ADDRESS
        assert thread._poll_interval == DEFAULT_POLL_INTERVAL_S
        assert thread._on_event is None

    def test_init_accepts_overrides(self) -> None:
        callback = MagicMock()
        thread = I2cKeyboardThread(
            bus=3,
            address=0x42,
            poll_interval=0.1,
            on_event=callback,
        )
        assert thread._bus_num == 3
        assert thread._address == 0x42
        assert thread._poll_interval == 0.1
        assert thread._on_event is callback

    def test_is_daemon_thread(self) -> None:
        # Daemon threads don't block process exit. Critical for the
        # systemctl-stop path -- if the thread weren't daemon and
        # didn't join in cleanup, the daemon would hang on shutdown.
        thread = I2cKeyboardThread()
        assert thread.daemon is True


class TestI2cKeyboardThreadStop:
    def test_stop_sets_event(self) -> None:
        thread = I2cKeyboardThread()
        assert not thread._stop_event.is_set()
        thread.stop()
        assert thread._stop_event.is_set()

    def test_stop_is_idempotent(self) -> None:
        # Calling stop twice (e.g. SIGTERM during shutdown then a
        # later cleanup pass) must be a silent no-op.
        thread = I2cKeyboardThread()
        thread.stop()
        thread.stop()
        assert thread._stop_event.is_set()


class TestI2cKeyboardThreadRunErrors:
    """run() handles each failure mode gracefully (log + return)."""

    def test_missing_smbus2_returns_cleanly(self) -> None:
        # If smbus2 isn't installed (e.g. CI environment without
        # the package), run() must log + return without raising.
        # Force ImportError by clobbering the module in sys.modules.
        thread = I2cKeyboardThread()
        with patch.dict(sys.modules, {"smbus2": None}):
            # run() should return without raising; thread doesn't
            # enter the poll loop.
            thread.run()
        assert thread._bus is None  # never opened

    def test_bus_open_failure_returns_cleanly(self) -> None:
        # If SMBus(bus_num) raises (e.g. /dev/i2c-1 doesn't exist
        # because i2c-dev not loaded), run() must log + return.
        thread = I2cKeyboardThread()
        fake_smbus2 = MagicMock()
        fake_smbus2.SMBus.side_effect = OSError("no such device")
        with patch.dict(sys.modules, {"smbus2": fake_smbus2}):
            thread.run()
        assert thread._bus is None

    def test_probe_failure_returns_cleanly_without_polling(self) -> None:
        # v0.0.18: if the bus opens but the device doesn't respond at
        # the probe address, run() must log INFO once and exit cleanly
        # without entering the poll loop. Otherwise we get the
        # v0.0.17 journal-flood behavior (33 Hz OSError tracebacks
        # indefinitely) on hosts running auto-mode without a CardKB.
        thread = I2cKeyboardThread()
        fake_bus = MagicMock()
        # SMBus(bus_num) succeeds, but read_byte raises on probe.
        fake_bus.read_byte.side_effect = OSError(
            "[Errno 5] Input/output error"
        )
        fake_smbus2 = MagicMock()
        fake_smbus2.SMBus.return_value = fake_bus

        with patch.dict(sys.modules, {"smbus2": fake_smbus2}):
            thread.run()

        # Bus was opened, probed once, then closed and nulled.
        assert thread._bus is None
        # Probe was attempted exactly once (no retry, no poll loop).
        assert fake_bus.read_byte.call_count == 1
        # Bus was closed before return.
        fake_bus.close.assert_called_once()

    def test_present_device_enters_poll_loop(self) -> None:
        # v0.0.18 inverse case: if probe SUCCEEDS, run() must enter
        # the poll loop normally. This confirms the new probe doesn't
        # break the CardKB-attached path.
        events_received: list[KeyEvent] = []
        stop_after_first = threading.Event()

        def on_event(event: KeyEvent) -> None:
            events_received.append(event)
            stop_after_first.set()

        thread = I2cKeyboardThread(
            poll_interval=0.001,
            on_event=on_event,
        )

        fake_bus = MagicMock()
        # Probe (call 1) returns idle 0x00 -- succeeds, no event.
        # Poll (call 2) returns 'a' (0x61) -- generates event.
        # Subsequent polls return 0x00 (idle) until stop fires.
        fake_bus.read_byte.side_effect = [0x00, 0x61, 0x00, 0x00, 0x00]
        fake_smbus2 = MagicMock()
        fake_smbus2.SMBus.return_value = fake_bus

        original_wait = thread._stop_event.wait

        def gated_wait(timeout: float) -> bool:
            if stop_after_first.is_set():
                return True
            return original_wait(timeout)

        with patch.dict(sys.modules, {"smbus2": fake_smbus2}):
            thread._stop_event.wait = gated_wait  # type: ignore[assignment]
            thread.run()

        # The probe succeeded, so we entered the poll loop and
        # received the 'a' event.
        assert len(events_received) >= 1
        assert events_received[0].char == "a"
        # read_byte called at least twice: probe + at least one poll.
        assert fake_bus.read_byte.call_count >= 2


class TestI2cKeyboardThreadEventDispatch:
    """End-to-end: bytes from a mocked bus produce KeyEvents."""

    def test_dispatches_decoded_events(self) -> None:
        # Mock smbus2 to return one byte, then trigger stop so the
        # thread exits after dispatching exactly one event.
        events_received: list[KeyEvent] = []
        stop_after_first = threading.Event()

        def on_event(event: KeyEvent) -> None:
            events_received.append(event)
            stop_after_first.set()

        thread = I2cKeyboardThread(
            poll_interval=0.001,  # fast for test
            on_event=on_event,
        )

        # Mock SMBus.read_byte to return 'a' (0x61) once, then 0x00.
        # The stop signal fires from the callback so we don't loop.
        fake_bus = MagicMock()
        fake_bus.read_byte.return_value = 0x61
        fake_smbus2 = MagicMock()
        fake_smbus2.SMBus.return_value = fake_bus

        # Patch stop_event to fire when the test's callback signals,
        # so the poll loop terminates cleanly.
        original_wait = thread._stop_event.wait

        def gated_wait(timeout: float) -> bool:
            # First call: don't stop (let read happen).
            # After first callback: signal stop.
            if stop_after_first.is_set():
                return True
            return original_wait(timeout)

        with patch.dict(sys.modules, {"smbus2": fake_smbus2}):
            thread._stop_event.wait = gated_wait  # type: ignore[assignment]
            thread.run()

        assert len(events_received) >= 1
        # First event should be 'a' (matches the byte we returned).
        assert events_received[0].char == "a"

    def test_callback_exception_does_not_kill_thread(self) -> None:
        # If on_event raises, we log and continue. The poll loop
        # must keep running -- a buggy router shouldn't take down
        # keyboard input. This is the same policy as UART backend.
        bad_callback = MagicMock(side_effect=RuntimeError("router boom"))

        thread = I2cKeyboardThread(
            poll_interval=0.001,
            on_event=bad_callback,
        )

        fake_bus = MagicMock()
        # Return one event-producing byte, then stop the loop.
        fake_bus.read_byte.return_value = 0x61  # 'a'
        fake_smbus2 = MagicMock()
        fake_smbus2.SMBus.return_value = fake_bus

        # Stop after one iteration to bound the test.
        call_count = {"n": 0}
        original_wait = thread._stop_event.wait

        def gated_wait(timeout: float) -> bool:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return True
            return original_wait(timeout)

        with patch.dict(sys.modules, {"smbus2": fake_smbus2}):
            thread._stop_event.wait = gated_wait  # type: ignore[assignment]
            # Critical: run() must NOT raise even though callback did.
            thread.run()

        bad_callback.assert_called()


class TestI2cKeyboardModuleIsAscii:
    """Per v0.0.14 paste-encoding incident: source must be pure ASCII."""

    def test_source_file_is_ascii(self) -> None:
        import microjs8.input.i2c_keyboard as mod
        raw = open(mod.__file__, "rb").read()
        non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        assert not non_ascii, (
            f"i2c_keyboard.py contains {len(non_ascii)} non-ASCII "
            f"bytes (first at offset {non_ascii[0][0]} = "
            f"0x{non_ascii[0][1]:02x}); source must be pure ASCII "
            "per the v0.0.14 paste-encoding policy"
        )
