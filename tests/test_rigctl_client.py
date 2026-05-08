"""Tests for minijs8.cat.rigctl_client.RigctlClient.

We don't need a real rigctld. We bind a real socket to a high port and
script responses byte-for-byte — that exercises the full
network-protocol path without external dependencies.

Why a real socket and not an in-memory mock: the client's robustness
to fragmented chunks and partial line buffering is one of the things
we most want to test. A real socket lets us send `b"R"`, then `b"PRT"`,
then `b" 0\\n"` and confirm the client buffers correctly.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

import pytest

from minijs8.cat.rigctl_client import (
    RigctlClient,
    RigctlError,
    RigctlNotOk,
)


class _FakeRigctld:
    """Tiny TCP server that simulates rigctld's response protocol.

    Construct with a callable ``responder`` that takes a command line
    (already stripped of newline) and returns the bytes to send back.
    The server runs on a background thread and accepts one client.
    """

    def __init__(self, responder: Callable[[str], bytes]) -> None:
        self._responder = responder
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))  # ephemeral port
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stop = False
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._sock.settimeout(2.0)
            client_sock, _ = self._sock.accept()
        except (socket.timeout, OSError):
            return
        client_sock.settimeout(2.0)
        buf = b""
        try:
            while not self._stop:
                try:
                    chunk = client_sock.recv(1024)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    cmd = line.decode("ascii", errors="replace").strip()
                    response = self._responder(cmd)
                    if response is None:
                        continue
                    try:
                        client_sock.sendall(response)
                    except OSError:
                        return
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


@pytest.fixture
def fake_rigctld():
    """Factory fixture — give it a responder, get back a fake server."""
    servers: list[_FakeRigctld] = []

    def make(responder: Callable[[str], bytes]) -> _FakeRigctld:
        s = _FakeRigctld(responder)
        servers.append(s)
        return s

    yield make

    for s in servers:
        s.stop()


# ── Basic connect / disconnect ──────────────────────────────────────


def test_connect_and_close(fake_rigctld):
    server = fake_rigctld(lambda cmd: b"RPRT 0\n")
    client = RigctlClient("127.0.0.1", server.port)
    client.connect()
    assert client.is_connected
    client.close()
    assert not client.is_connected


def test_connect_failure_raises(fake_rigctld):
    """Connect to a port nobody's listening on."""
    # Find an unused port by binding then closing.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    client = RigctlClient("127.0.0.1", port)
    with pytest.raises(OSError):
        client.connect()


def test_context_manager(fake_rigctld):
    server = fake_rigctld(lambda cmd: b"RPRT 0\n")
    with RigctlClient("127.0.0.1", server.port) as client:
        assert client.is_connected
    assert not client.is_connected


# ── Frequency operations ────────────────────────────────────────────


def test_get_frequency_value_only_response(fake_rigctld):
    """Some hamlib versions return just the value, no RPRT."""
    server = fake_rigctld(lambda cmd: b"7078000\n" if cmd == "f" else b"RPRT 0\n")
    with RigctlClient("127.0.0.1", server.port) as client:
        assert client.get_frequency_hz() == 7_078_000


def test_get_frequency_with_rprt_response(fake_rigctld):
    """Other hamlib versions return value followed by RPRT 0."""
    def responder(cmd):
        if cmd == "f":
            return b"7078000\nRPRT 0\n"
        return b"RPRT 0\n"
    server = fake_rigctld(responder)
    with RigctlClient("127.0.0.1", server.port) as client:
        assert client.get_frequency_hz() == 7_078_000


def test_set_frequency(fake_rigctld):
    captured = {"cmd": None}

    def responder(cmd):
        captured["cmd"] = cmd
        return b"RPRT 0\n"

    server = fake_rigctld(responder)
    with RigctlClient("127.0.0.1", server.port) as client:
        client.set_frequency_hz(7_078_000)
    assert captured["cmd"] == "F 7078000"


def test_set_frequency_negative_rejected(fake_rigctld):
    server = fake_rigctld(lambda cmd: b"RPRT 0\n")
    with RigctlClient("127.0.0.1", server.port) as client:
        with pytest.raises(ValueError):
            client.set_frequency_hz(-1)


# ── PTT operations ──────────────────────────────────────────────────


def test_ptt_on_sends_T1(fake_rigctld):
    captured = {"cmd": None}

    def responder(cmd):
        captured["cmd"] = cmd
        return b"RPRT 0\n"

    server = fake_rigctld(responder)
    with RigctlClient("127.0.0.1", server.port) as client:
        client.ptt_on()
    assert captured["cmd"] == "T 1"


def test_ptt_off_sends_T0(fake_rigctld):
    captured = {"cmd": None}

    def responder(cmd):
        captured["cmd"] = cmd
        return b"RPRT 0\n"

    server = fake_rigctld(responder)
    with RigctlClient("127.0.0.1", server.port) as client:
        client.ptt_off()
    assert captured["cmd"] == "T 0"


def test_get_ptt_returns_true_when_keyed(fake_rigctld):
    server = fake_rigctld(lambda cmd: b"1\n" if cmd == "t" else b"RPRT 0\n")
    with RigctlClient("127.0.0.1", server.port) as client:
        assert client.get_ptt() is True


def test_get_ptt_returns_false_when_unkeyed(fake_rigctld):
    server = fake_rigctld(lambda cmd: b"0\n" if cmd == "t" else b"RPRT 0\n")
    with RigctlClient("127.0.0.1", server.port) as client:
        assert client.get_ptt() is False


# ── Error handling ──────────────────────────────────────────────────


def test_rprt_nonzero_raises_RigctlNotOk(fake_rigctld):
    """rigctld returning RPRT -8 (unimplemented) must raise the
    typed exception so the caller can handle it specifically."""
    server = fake_rigctld(lambda cmd: b"RPRT -8\n")
    with RigctlClient("127.0.0.1", server.port) as client:
        with pytest.raises(RigctlNotOk) as exc:
            client.set_frequency_hz(7_078_000)
        assert exc.value.code == -8
        assert "F 7078000" in exc.value.command


def test_command_when_disconnected_raises():
    client = RigctlClient("127.0.0.1", 65000)  # never connected
    with pytest.raises(RigctlError, match="not connected"):
        client.get_frequency_hz()


def test_socket_drop_invalidates_client(fake_rigctld):
    """If the server closes mid-command, client.is_connected goes False."""
    closed = {"yes": False}

    def responder(cmd):
        if cmd == "f":
            closed["yes"] = True
            # Returning None and the server thread won't send anything;
            # we close the socket from the server side instead. To do
            # that cleanly with this fake, send EOF by returning b"".
            return b""
        return b"RPRT 0\n"

    server = fake_rigctld(responder)
    with RigctlClient("127.0.0.1", server.port) as client:
        # First command — server will EOF us mid-protocol.
        with pytest.raises(RigctlError):
            client.get_frequency_hz()
        assert not client.is_connected


def test_chunked_response_assembled_correctly(fake_rigctld):
    """Server sends the response in tiny chunks (less than full line);
    the client must buffer correctly."""
    # We'll craft a server that uses a low-level send pattern by
    # sending the response one byte at a time.
    responder_called = {"n": 0}

    class _ChunkyServer:
        def __init__(self, port_holder):
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(1)
            port_holder.append(self.sock.getsockname()[1])
            self._t = threading.Thread(target=self._serve, daemon=True)
            self._t.start()

        def _serve(self):
            self.sock.settimeout(2.0)
            try:
                cl, _ = self.sock.accept()
            except (socket.timeout, OSError):
                return
            cl.settimeout(2.0)
            buf = b""
            try:
                while True:
                    try:
                        chunk = cl.recv(1024)
                    except socket.timeout:
                        return
                    if not chunk:
                        return
                    buf += chunk
                    if b"\n" in buf:
                        # Send "7078000\nRPRT 0\n" one byte at a time.
                        for byte in b"7078000\nRPRT 0\n":
                            cl.sendall(bytes([byte]))
                            time.sleep(0.005)
                        return
            finally:
                try:
                    cl.close()
                except OSError:
                    pass
                try:
                    self.sock.close()
                except OSError:
                    pass

    port_holder = []
    server = _ChunkyServer(port_holder)
    time.sleep(0.05)  # let the server bind
    try:
        with RigctlClient("127.0.0.1", port_holder[0]) as client:
            assert client.get_frequency_hz() == 7_078_000
    finally:
        try:
            server.sock.close()
        except OSError:
            pass


def test_thread_safety_serializes_commands(fake_rigctld):
    """Two threads calling commands at once must not interleave —
    the lock serializes them."""
    seen_cmds: list[str] = []

    def responder(cmd):
        seen_cmds.append(cmd)
        # Simulate slow CAT response so concurrent threads can race.
        time.sleep(0.05)
        if cmd == "f":
            return b"7078000\nRPRT 0\n"
        if cmd == "F 7100000":
            return b"RPRT 0\n"
        return b"RPRT 0\n"

    server = fake_rigctld(responder)
    with RigctlClient("127.0.0.1", server.port) as client:
        results = [None, None]

        def get_freq():
            results[0] = client.get_frequency_hz()

        def set_freq():
            client.set_frequency_hz(7_100_000)
            results[1] = "ok"

        t1 = threading.Thread(target=get_freq)
        t2 = threading.Thread(target=set_freq)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert results[0] == 7_078_000
        assert results[1] == "ok"
        # Both commands made it in some order.
        assert "f" in seen_cmds
        assert "F 7100000" in seen_cmds


# ── ping ────────────────────────────────────────────────────────────


def test_ping_round_trips(fake_rigctld):
    """ping() must use a real CAT command (f = get frequency), not
    \\chk_vfo, so that keepalive actually detects when the serial port
    to the radio is dead. \\chk_vfo is answered by the hamlib library
    itself, which gives false-positive "connected" status when the
    QDX is unplugged but rigctld is still running."""
    captured = {"cmd": None}

    def responder(cmd):
        captured["cmd"] = cmd
        return b"7078000\nRPRT 0\n"

    server = fake_rigctld(responder)
    with RigctlClient("127.0.0.1", server.port) as client:
        client.ping()
    assert captured["cmd"] == "f"
