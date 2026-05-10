"""Tests for microjs8.cat.service.CatService.

The service does three jobs we need to verify:

  1. Connect to rigctld and reconnect on failure.
  2. Forward CAT commands while connected, return None when not.
  3. Watchdog releases stuck PTT after a max-hold timeout.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

import pytest

from microjs8.cat import service as service_module
from microjs8.cat.service import CatService


# ── Tiny rigctld fake — same shape as test_rigctl_client.py ─────────


class _FakeRigctld:
    def __init__(self, responder: Callable[[str], bytes]) -> None:
        self._responder = responder
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(2)
        self.host, self.port = self._sock.getsockname()
        self._stop = False
        self._threads: list[threading.Thread] = []
        self._listener = threading.Thread(target=self._accept_loop, daemon=True)
        self._listener.start()

    def _accept_loop(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                client_sock, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            t = threading.Thread(
                target=self._handle, args=(client_sock,), daemon=True,
            )
            t.start()
            self._threads.append(t)

    def _handle(self, cl: socket.socket) -> None:
        cl.settimeout(0.5)
        buf = b""
        try:
            while not self._stop:
                try:
                    chunk = cl.recv(1024)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    cmd = line.decode("ascii", errors="replace").strip()
                    response = self._responder(cmd)
                    if response is None:
                        return  # signal: drop the connection
                    try:
                        cl.sendall(response)
                    except OSError:
                        return
        finally:
            try:
                cl.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def fake_rigctld():
    servers: list[_FakeRigctld] = []

    def make(responder: Callable[[str], bytes]) -> _FakeRigctld:
        s = _FakeRigctld(responder)
        servers.append(s)
        return s

    yield make

    for s in servers:
        s.stop()


def _normal_responder(cmd: str) -> bytes:
    if cmd == "f":
        return b"7078000\nRPRT 0\n"
    if cmd == "\\chk_vfo":
        return b"VFOA\nRPRT 0\n"
    return b"RPRT 0\n"


# ── Lifecycle ───────────────────────────────────────────────────────


def test_service_connects_on_start(fake_rigctld, monkeypatch):
    server = fake_rigctld(_normal_responder)
    # Speed up reconnect for the test.
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        # Give the reconnect thread a moment to connect.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        assert svc.is_connected
    finally:
        svc.stop()


def test_service_calls_when_connected(fake_rigctld, monkeypatch):
    server = fake_rigctld(_normal_responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        # Wait for connection.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        assert svc.is_connected
        freq = svc.get_frequency_hz()
        assert freq == 7_078_000
    finally:
        svc.stop()


def test_service_returns_none_when_not_connected(monkeypatch):
    """Calls before connection-attempt completes return None safely."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    # Bind a port nobody listens on.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    svc = CatService(host="127.0.0.1", port=port)
    svc.start()
    try:
        assert svc.get_frequency_hz() is None
        assert svc.set_frequency_hz(7_100_000) is False
        assert svc.ptt_on() is False
        assert svc.ptt_off() is False
    finally:
        svc.stop()


def test_status_callback_fires_on_connect(fake_rigctld, monkeypatch):
    server = fake_rigctld(_normal_responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    states: list[bool] = []
    svc = CatService(
        host="127.0.0.1", port=server.port,
        on_status_change=lambda c: states.append(c),
    )
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        assert True in states  # at least one True notification
    finally:
        svc.stop()


# ── PTT operations ──────────────────────────────────────────────────


def test_ptt_on_then_off_clean_cycle(fake_rigctld, monkeypatch):
    captured: list[str] = []

    def responder(cmd):
        captured.append(cmd)
        return _normal_responder(cmd)

    server = fake_rigctld(responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        assert svc.is_connected
        assert svc.ptt_on() is True
        assert svc.ptt_off() is True
        assert "T 1" in captured
        assert "T 0" in captured
    finally:
        svc.stop()


def test_stop_releases_ptt(fake_rigctld, monkeypatch):
    """Critical safety property: shutdown must always send T 0
    before tearing down the connection."""
    captured: list[str] = []

    def responder(cmd):
        captured.append(cmd)
        return _normal_responder(cmd)

    server = fake_rigctld(responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        # Key the radio.
        assert svc.is_connected
        svc.ptt_on()
        captured.clear()
    finally:
        svc.stop()

    # After stop(), the captured commands must include T 0.
    assert "T 0" in captured, \
        f"PTT was not released during shutdown! commands: {captured}"


# ── Watchdog ────────────────────────────────────────────────────────


def test_watchdog_forces_ptt_release_after_max_hold(fake_rigctld, monkeypatch):
    """If TxBackend forgets to release PTT, the watchdog must do it."""
    captured: list[str] = []

    def responder(cmd):
        captured.append(cmd)
        return _normal_responder(cmd)

    server = fake_rigctld(responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    # Speed up the watchdog timeout for the test.
    monkeypatch.setattr(service_module, "_PTT_MAX_HOLD_S", 0.3)

    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        captured.clear()

        # Simulate buggy code path: ptt_on without ever calling ptt_off.
        svc.ptt_on()
        # Wait for the watchdog to fire.
        time.sleep(1.0)
        # The watchdog should have sent T 0 by now.
        assert "T 0" in captured, \
            f"watchdog did not release PTT! captured: {captured}"
    finally:
        svc.stop()


def test_watchdog_does_not_fire_on_normal_release(fake_rigctld, monkeypatch):
    """When ptt_off() runs in normal time, the watchdog must NOT fire."""
    captured: list[str] = []

    def responder(cmd):
        captured.append(cmd)
        return _normal_responder(cmd)

    server = fake_rigctld(responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    monkeypatch.setattr(service_module, "_PTT_MAX_HOLD_S", 0.5)

    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        captured.clear()

        svc.ptt_on()
        time.sleep(0.1)  # well within the 0.5s budget
        svc.ptt_off()
        captured.clear()
        # Wait longer than the watchdog timeout.
        time.sleep(0.7)
        # No additional T 0 should have been sent.
        assert "T 0" not in captured, \
            f"watchdog spuriously fired: {captured}"
    finally:
        svc.stop()


def test_set_ptt_max_hold_override_extends_watchdog(
    fake_rigctld, monkeypatch,
):
    """set_ptt_max_hold(seconds) overrides _PTT_MAX_HOLD_S for the
    next PTT cycle. Used by the scheduler so multi-frame bursts
    aren't watchdog'd off mid-message.

    Note: the watchdog loop polls every 500 ms, so the actual fire
    moment is up to 500 ms LATER than the configured cap. We wait
    long enough to give the next tick room to run."""
    captured: list[str] = []

    def responder(cmd):
        captured.append(cmd)
        return _normal_responder(cmd)

    server = fake_rigctld(responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    # Default is 0.3s — short for fast test.
    monkeypatch.setattr(service_module, "_PTT_MAX_HOLD_S", 0.3)

    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        captured.clear()

        # Override: allow 1.0s before watchdog fires.
        svc.set_ptt_max_hold(1.0)
        svc.ptt_on()
        # Wait long enough that the DEFAULT (0.3s) would have fired,
        # but BEFORE the override (1.0s).
        time.sleep(0.6)
        assert "T 0" not in captured, (
            f"watchdog fired before override deadline (0.6s elapsed, "
            f"override 1.0s): {captured}"
        )
        # Now wait until well past the override deadline. The watchdog
        # loop ticks every 500 ms so we need to give it at least one
        # full tick past the cap (so up to 1.5s total elapsed). Wait
        # 1.0s more (1.6s total) to be safely past that.
        time.sleep(1.0)
        assert "T 0" in captured, (
            f"watchdog did not fire by 1.6s elapsed "
            f"(override was 1.0s): {captured}"
        )
    finally:
        svc.stop()


def test_ptt_off_clears_max_hold_override(fake_rigctld, monkeypatch):
    """Override is per-cycle: ptt_off clears it so subsequent PTT
    cycles fall back to the default _PTT_MAX_HOLD_S."""
    captured: list[str] = []

    def responder(cmd):
        captured.append(cmd)
        return _normal_responder(cmd)

    server = fake_rigctld(responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    monkeypatch.setattr(service_module, "_PTT_MAX_HOLD_S", 0.3)

    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)

        # First cycle: override extends to 1s.
        svc.set_ptt_max_hold(1.0)
        svc.ptt_on()
        time.sleep(0.1)
        svc.ptt_off()  # clears the override
        captured.clear()

        # Second cycle: NO new override. The default (0.3s) should
        # apply — watchdog fires within ~0.5s.
        svc.ptt_on()
        time.sleep(0.7)
        assert "T 0" in captured, (
            f"watchdog did not fire with default cap after override "
            f"cleared: {captured}"
        )
    finally:
        svc.stop()


def test_set_ptt_max_hold_zero_clears_override(
    fake_rigctld, monkeypatch,
):
    """set_ptt_max_hold(0) immediately clears the override —
    rare, but useful for tests / programmatic reset."""
    captured: list[str] = []

    def responder(cmd):
        captured.append(cmd)
        return _normal_responder(cmd)

    server = fake_rigctld(responder)
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    monkeypatch.setattr(service_module, "_PTT_MAX_HOLD_S", 0.3)

    svc = CatService(host="127.0.0.1", port=server.port)
    svc.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not svc.is_connected:
            time.sleep(0.05)
        captured.clear()

        # Override to 2s, then immediately revert.
        svc.set_ptt_max_hold(2.0)
        svc.set_ptt_max_hold(0)  # clears
        svc.ptt_on()
        # Default (0.3s) should fire.
        time.sleep(0.7)
        assert "T 0" in captured, (
            f"watchdog did not revert to default after clear: {captured}"
        )
    finally:
        svc.stop()
