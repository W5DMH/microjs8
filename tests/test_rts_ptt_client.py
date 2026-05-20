"""Tests for microjs8.cat.rts_ptt_client.RtsPttClient.

We stub out ``serial.Serial`` so tests don't need a real DigiRig.
The fake records every line-state change (RTS / DTR) so we can
verify exact PTT keying behavior.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from microjs8.cat.rts_ptt_client import RtsPttClient, RtsPttError


# ── Fake serial module ──────────────────────────────────────────────


@pytest.fixture
def fake_serial(monkeypatch):
    """Stub ``serial.Serial`` with a recordable spy.

    The spy:
      - Captures construction kwargs (port, baudrate, rtscts, etc.)
      - Records every assignment to ``rts`` / ``dtr`` so tests can
        verify the exact sequence of line-state changes.
      - Optionally raises on construction or attribute set, so tests
        can exercise error paths without a real device.
    """
    state: dict[str, Any] = {
        "construct_kwargs": None,
        "rts_history": [],   # list of bool values written to .rts
        "dtr_history": [],
        "closed": False,
        "raise_on_construct": None,
        "raise_on_rts_set": None,
    }

    class _FakeSerial:
        def __init__(self, **kwargs):
            if state["raise_on_construct"]:
                raise state["raise_on_construct"]
            state["construct_kwargs"] = kwargs
            self._rts = False
            self._dtr = False

        @property
        def rts(self) -> bool:
            return self._rts

        @rts.setter
        def rts(self, value: bool) -> None:
            if state["raise_on_rts_set"]:
                raise state["raise_on_rts_set"]
            self._rts = bool(value)
            state["rts_history"].append(bool(value))

        @property
        def dtr(self) -> bool:
            return self._dtr

        @dtr.setter
        def dtr(self, value: bool) -> None:
            self._dtr = bool(value)
            state["dtr_history"].append(bool(value))

        def close(self) -> None:
            state["closed"] = True

    class _FakeSerialModule:
        Serial = _FakeSerial
        # Some pyserial users do `serial.SerialException` — provide
        # a shim that's just Exception so test code can construct it.
        SerialException = Exception

    monkeypatch.setitem(sys.modules, "serial", _FakeSerialModule)
    return state


# ── Lifecycle ──────────────────────────────────────────────────────


def test_construct_does_not_open_port(fake_serial):
    """Construction is cheap; open is in open()."""
    RtsPttClient(port="/dev/digirig")
    assert fake_serial["construct_kwargs"] is None


def test_open_constructs_serial_with_correct_kwargs(fake_serial):
    """Verify the kwargs we pass to serial.Serial are sane.

    rtscts MUST be False — if rtscts is True the kernel manages RTS
    for flow control and we can't drive PTT via RTS toggling. This
    is a load-bearing detail for DigiRig.
    """
    c = RtsPttClient(port="/dev/digirig", baudrate=19200)
    c.open()
    kw = fake_serial["construct_kwargs"]
    assert kw["port"] == "/dev/digirig"
    assert kw["baudrate"] == 19200
    assert kw["rtscts"] is False, (
        "rtscts MUST be False for RTS-PTT to work — when True the "
        "kernel manages RTS for flow control and PTT toggling fails"
    )


def test_open_initializes_lines_to_released(fake_serial):
    """After open(), RTS and DTR should both be False (PTT released).

    Linux briefly asserts RTS high during port open as a side effect
    of how the kernel sets up the tty layer; we can't avoid that.
    But the FIRST observable state via our API should be PTT released.
    """
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    # The history should end with RTS=False and DTR=False.
    assert fake_serial["rts_history"] == [False]
    assert fake_serial["dtr_history"] == [False]


def test_open_idempotent(fake_serial):
    """Calling open() twice is a no-op (does NOT re-open)."""
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    initial_history = list(fake_serial["rts_history"])
    c.open()  # second call must not crash or re-init
    assert fake_serial["rts_history"] == initial_history


def test_open_failure_raises_RtsPttError(fake_serial):
    """When the port can't be opened, raise RtsPttError, not pyserial's
    own exception type. The service layer catches RtsPttError to
    decide reconnect behavior."""
    fake_serial["raise_on_construct"] = OSError("no such device")
    c = RtsPttClient(port="/dev/digirig")
    with pytest.raises(RtsPttError, match="could not open"):
        c.open()
    # Failed open must NOT leave the client in a half-opened state.
    assert not c.is_connected


def test_close_releases_ptt_first(fake_serial):
    """close() must set RTS=False before closing the underlying port,
    so a fault that happens during close doesn't leave the radio keyed.
    """
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    c.ptt_on()  # asserts RTS
    assert fake_serial["rts_history"][-1] is True
    c.close()
    # The last RTS write before close should be False.
    last_rts = fake_serial["rts_history"][-1]
    assert last_rts is False
    assert fake_serial["closed"] is True


def test_close_idempotent(fake_serial):
    """close() is safe to call multiple times."""
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    c.close()
    c.close()  # second call must not raise


def test_close_safe_when_never_opened(fake_serial):
    """close() before open() is safe."""
    c = RtsPttClient(port="/dev/digirig")
    c.close()  # must not raise


def test_is_connected_reflects_state(fake_serial):
    c = RtsPttClient(port="/dev/digirig")
    assert not c.is_connected
    c.open()
    assert c.is_connected
    c.close()
    assert not c.is_connected


# ── PTT operations ─────────────────────────────────────────────────


def test_ptt_on_asserts_rts(fake_serial):
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    c.ptt_on()
    # Last RTS write was True.
    assert fake_serial["rts_history"][-1] is True


def test_ptt_off_releases_rts(fake_serial):
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    c.ptt_on()
    c.ptt_off()
    assert fake_serial["rts_history"][-1] is False


def test_ptt_on_off_sequence_recorded(fake_serial):
    """The complete sequence after open() should be: False (init),
    True (on), False (off). Tests that ptt_on/off don't overwrite
    each other's state."""
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    c.ptt_on()
    c.ptt_off()
    c.ptt_on()
    c.ptt_off()
    # First False is from open() init; rest from on/off sequence.
    assert fake_serial["rts_history"] == [False, True, False, True, False]


def test_ptt_on_when_not_open_raises(fake_serial):
    c = RtsPttClient(port="/dev/digirig")
    with pytest.raises(RtsPttError, match="not open"):
        c.ptt_on()


def test_ptt_off_when_not_open_raises(fake_serial):
    c = RtsPttClient(port="/dev/digirig")
    with pytest.raises(RtsPttError, match="not open"):
        c.ptt_off()


def test_ptt_on_wraps_pyserial_errors(fake_serial):
    """If the kernel rejects the RTS write (e.g. USB unplugged),
    we wrap it in RtsPttError so the service layer can react."""
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    fake_serial["raise_on_rts_set"] = OSError("device disconnected")
    with pytest.raises(RtsPttError, match="RTS assert"):
        c.ptt_on()


# ── CAT operations are unsupported on RTS-only ─────────────────────


def test_get_frequency_hz_raises(fake_serial):
    """RTS-only radios have no CAT — get_frequency_hz raises rather
    than silently lying. The service layer wraps this so callers see
    None instead of an exception."""
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    with pytest.raises(RtsPttError, match="no CAT"):
        c.get_frequency_hz()


def test_set_frequency_hz_raises(fake_serial):
    c = RtsPttClient(port="/dev/digirig")
    c.open()
    with pytest.raises(RtsPttError, match="no CAT"):
        c.set_frequency_hz(7_078_000)


# ── pyserial missing ────────────────────────────────────────────────


def test_open_without_pyserial_raises_clear_error(monkeypatch):
    """If pyserial isn't installed, give a clear error message
    rather than the cryptic ImportError that bubbles up by default.
    """
    # Make sure 'serial' isn't importable for this test.
    monkeypatch.setitem(sys.modules, "serial", None)
    c = RtsPttClient(port="/dev/digirig")
    with pytest.raises(RtsPttError, match="pyserial not installed"):
        c.open()


# ── Phase 18.3 tests: ioctl-based modem-line clearing ────────────────


def test_force_clear_modem_lines_calls_tiocmset_zero(monkeypatch):
    """Phase 18.3: _force_clear_modem_lines must drive ALL modem
    control lines low via ioctl(TIOCMSET, 0). Without this, the
    CP210x latches RTS high across pyserial property writes and
    the radio gets stuck in TX. This test verifies we call the
    right ioctl with the right payload."""
    import struct
    import termios
    from microjs8.cat.rts_ptt_client import RtsPttClient

    # Capture ioctl calls
    ioctl_calls: list[tuple] = []

    def fake_ioctl(fd, op, payload):
        ioctl_calls.append((fd, op, payload))
        return payload

    monkeypatch.setattr("fcntl.ioctl", fake_ioctl)

    # Hand-craft a client with a fake _serial that has fileno().
    client = RtsPttClient(port="/dev/test", baudrate=9600)

    class _FakeSerialWithFileno:
        def __init__(self):
            self._closed = False
        def fileno(self):
            return 99    # arbitrary

    client._serial = _FakeSerialWithFileno()  # type: ignore[assignment]
    client._opened = True

    client._force_clear_modem_lines()

    assert len(ioctl_calls) == 1, f"expected exactly 1 ioctl call, got {len(ioctl_calls)}"
    fd, op, payload = ioctl_calls[0]
    assert fd == 99
    assert op == termios.TIOCMSET, "must call TIOCMSET to set modem lines"
    assert payload == struct.pack("I", 0), (
        f"payload must be all-zero (all lines low); got {payload!r}"
    )


def test_force_clear_modem_lines_swallows_ioctl_errors():
    """Phase 18.3: if ioctl raises (USB unplug, closed fd, etc.) we
    must NOT propagate — the caller wants close() / cleanup paths
    to keep running. Without this, a single ioctl error during
    close() could leak the SPI fd or skip resource releases."""
    from microjs8.cat.rts_ptt_client import RtsPttClient

    client = RtsPttClient(port="/dev/test")

    class _FakeSerialThatBreaks:
        def fileno(self):
            raise OSError("simulated USB unplug")

    client._serial = _FakeSerialThatBreaks()  # type: ignore[assignment]
    client._opened = True

    # Must not raise
    client._force_clear_modem_lines()


def test_force_clear_modem_lines_handles_missing_serial():
    """Phase 18.3: if _serial is None (open never succeeded),
    _force_clear_modem_lines must be a clean no-op, not crash."""
    from microjs8.cat.rts_ptt_client import RtsPttClient
    client = RtsPttClient(port="/dev/test")
    # _serial is None by default
    client._force_clear_modem_lines()    # must not raise


def test_close_invokes_ioctl_clear_before_close(fake_serial, monkeypatch):
    """Phase 18.3: close() must call _force_clear_modem_lines
    BEFORE the serial port's close() is called. This ordering is
    critical — once close() runs, the file descriptor is gone and
    we can't ioctl on it anymore.

    We patch _force_clear_modem_lines on the instance to record
    when it's called relative to the serial close().
    """
    from microjs8.cat.rts_ptt_client import RtsPttClient

    c = RtsPttClient(port="/dev/digirig")
    c.open()

    call_order: list[str] = []

    original_force_clear = c._force_clear_modem_lines
    def spy_force_clear():
        call_order.append("force_clear")
        original_force_clear()

    # Wrap the FakeSerial's close to also record
    original_serial_close = c._serial.close   # type: ignore[union-attr]
    def spy_serial_close():
        call_order.append("serial_close")
        original_serial_close()

    c._force_clear_modem_lines = spy_force_clear  # type: ignore[method-assign]
    c._serial.close = spy_serial_close  # type: ignore[union-attr,method-assign]

    c.close()

    assert call_order == ["force_clear", "serial_close"], (
        f"force_clear must come before serial_close; actual order: {call_order}"
    )


def test_ptt_off_uses_ioctl_clear(fake_serial, monkeypatch):
    """Phase 18.3: ptt_off() must use ioctl-based clearing because
    the pyserial .rts=False assignment doesn't reliably propagate
    to the CP210x driver. Without this, ptt_off() looks like it
    succeeded but the chip keeps RTS asserted."""
    from microjs8.cat.rts_ptt_client import RtsPttClient

    c = RtsPttClient(port="/dev/digirig")
    c.open()

    force_clear_called = [False]
    original = c._force_clear_modem_lines
    def spy():
        force_clear_called[0] = True
        original()

    c._force_clear_modem_lines = spy  # type: ignore[method-assign]
    c.ptt_off()

    assert force_clear_called[0], (
        "ptt_off must call _force_clear_modem_lines to defeat CP210x "
        "RTS latching — pyserial's .rts=False alone is not enough"
    )
