"""rigctld TCP client.

Hamlib's ``rigctld`` runs as a long-lived daemon owning ``/dev/qdx``
(or whichever serial port the radio is on) and exposes a plain-text
TCP protocol on ``localhost:4532``. We connect once at daemon startup
and stay connected for the whole session.

This mirrors ``gps/gpsd_client.py``: one long-lived consumer of an
external daemon, with reconnect-on-failure handled by the wrapping
thread.

Why this is better than spawning rigctl per command:
  - ~50 ms latency per command vs ~1 ms for an open socket
  - Zero risk of stuck PTT from a half-spawned subprocess that fails
    *after* "T 1" landed but *before* "T 0" did
  - rigctld owns the serial port, so the lifecycle of /dev/qdx is
    handled by systemd, not us — we get hot-plug recovery for free

Protocol reference:
  https://hamlib.sourceforge.net/manuals/4.5/rigctld.html

Commands we use in MiniJS8:
  ``f``           get current frequency (Hz, integer)
  ``F <hz>``      set frequency
  ``T 1``         PTT on
  ``T 0``         PTT off
  ``t``           get PTT state (0 or 1)
  ``\\chk_vfo``   cheap no-op query (NOT used for keepalive — see ping())

All responses are line-oriented. Most commands return either a value
followed by a newline (for queries) or "RPRT 0\\n" (for OK on commands).
Errors are "RPRT N\\n" where N is a hamlib errno.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

_log = logging.getLogger(__name__)

RIGCTLD_DEFAULT_HOST = "127.0.0.1"
RIGCTLD_DEFAULT_PORT = 4532

# Connect timeout — rigctld is local; this is just a sanity bound.
_CONNECT_TIMEOUT_S = 3.0
# Per-command response timeout. CAT commands round-trip in <100 ms
# usually; we give plenty of margin for a USB hiccup. The PTT-on
# command can take a fraction longer because the radio mutes audio
# before acknowledging; 2 s is comfortable.
_COMMAND_TIMEOUT_S = 2.0


class RigctlError(Exception):
    """Communication error with rigctld."""


class RigctlNotOk(RigctlError):
    """rigctld returned RPRT with a non-zero hamlib errno.

    The errno is in ``self.code``. Most common values:
      -1 generic error
      -8 unimplemented (radio doesn't support this command)
      -9 communication timeout
      -11 IO error (USB unplugged?)
    """

    def __init__(self, code: int, command: str) -> None:
        super().__init__(f"rigctld returned RPRT {code} for {command!r}")
        self.code = code
        self.command = command


class RigctlClient:
    """Synchronous client for a long-lived rigctld connection.

    Thread-safe: one ``threading.Lock`` serializes commands. CAT is
    request/response, never streaming, so we don't need a separate
    reader thread — the calling thread waits for the response in-line.

    Construct, call ``connect()``, then call commands. ``close()`` to
    shut down. On any communication error we mark the client as
    disconnected; the wrapping CatService is responsible for reconnect
    if desired.
    """

    def __init__(
        self,
        host: str = RIGCTLD_DEFAULT_HOST,
        port: int = RIGCTLD_DEFAULT_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._buf: bytes = b""
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        """Open the TCP connection. Raises socket.error on failure."""
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT_S)
        sock.connect((self._host, self._port))
        sock.settimeout(_COMMAND_TIMEOUT_S)
        self._sock = sock
        self._buf = b""
        _log.info("rigctld connected at %s:%d", self._host, self._port)

    def close(self) -> None:
        """Close the connection. Idempotent. Safe to call from any
        thread including in finally blocks during shutdown."""
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._buf = b""

    # ── Public commands ──────────────────────────────────────────────

    def get_frequency_hz(self) -> int:
        """Query the radio's current VFO frequency in Hz."""
        resp = self._query("f")
        try:
            return int(resp.strip())
        except ValueError as exc:
            raise RigctlError(
                f"could not parse frequency response: {resp!r}"
            ) from exc

    def set_frequency_hz(self, hz: int) -> None:
        """Set the radio's VFO frequency. Validates rigctld's RPRT."""
        if hz <= 0:
            raise ValueError(f"frequency must be positive, got {hz}")
        self._command_ok(f"F {hz}")

    def ptt_on(self) -> None:
        """Assert PTT. Caller MUST ensure ptt_off() runs eventually
        (use try/finally). Stuck-PTT prevention is critical."""
        self._command_ok("T 1")

    def ptt_off(self) -> None:
        """Release PTT. Always safe to call — calling when already
        off is a no-op as far as the radio is concerned."""
        self._command_ok("T 0")

    def get_ptt(self) -> bool:
        """Query whether PTT is currently asserted. Useful as a
        watchdog double-check before a TX cycle."""
        resp = self._query("t")
        return resp.strip() == "1"

    def ping(self) -> None:
        """Round-trip a low-cost command to confirm the connection AND
        the radio are both alive. Used by the wrapping service for
        keepalive.

        We use ``f`` (get frequency) — a small read that goes all the
        way to the radio over the serial port. We deliberately do NOT
        use ``\\chk_vfo`` here even though it's cheaper: ``\\chk_vfo``
        is answered by the hamlib LIBRARY, not the radio, so it
        succeeds even when the serial port is dead. That gave us a
        false-positive "CAT connected" indicator that stayed green
        while the QDX was unplugged for minutes.

        ``f`` returns immediately when the QDX is responsive (USB
        latency is sub-millisecond) and fails fast when the serial
        port is dead (hamlib reports the IO error, _query() raises
        RigctlError). The CatService reconnect loop catches the
        exception and marks the connection dropped.
        """
        self._query("f")

    # ── Low-level command/response ───────────────────────────────────

    def _query(self, line: str) -> str:
        """Send a query that returns a value.

        Hamlib versions vary in their response shape for value queries:
          - rigctld 3.x: just the value, e.g. ``7078000\\n``
          - rigctld 4.x: value + RPRT, e.g. ``7078000\\nRPRT 0\\n``

        We handle both by reading the first line, treating it as the
        value, and then opportunistically draining a trailing
        ``RPRT 0`` with a short timeout. If the first line is itself
        a ``RPRT`` with non-zero code, it was an error response and
        we raise.

        Raises RigctlError on socket error, RigctlNotOk on RPRT != 0.
        """
        if self._sock is None:
            raise RigctlError("not connected — call connect() first")

        with self._lock:
            try:
                self._sock.sendall((line + "\n").encode("ascii"))
                first = self._read_one_line(timeout_s=_COMMAND_TIMEOUT_S)
            except OSError as exc:
                self.close()
                raise RigctlError(f"socket error during {line!r}: {exc}") from exc
            except RigctlError:
                # Timeout or EOF — connection is in an ambiguous state,
                # invalidate it so the next call triggers a reconnect.
                self.close()
                raise

            # If the first line is RPRT, it's either OK (no value) or error.
            if first.startswith("RPRT "):
                self._check_rprt(first, line)
                return ""

            # Otherwise it's the value. Drain any trailing RPRT 0 with
            # a tight budget — if it's not there in 50 ms, this is the
            # value-only-response hamlib variant, and we move on.
            try:
                trailing = self._read_one_line(timeout_s=0.05)
            except RigctlError:
                trailing = None
            if trailing is not None and trailing.startswith("RPRT "):
                self._check_rprt(trailing, line)
            return first

    def _command_ok(self, line: str) -> None:
        """Send a command that returns only RPRT (no value).

        Used for set-style commands like F, T 1, T 0.
        """
        if self._sock is None:
            raise RigctlError("not connected — call connect() first")

        with self._lock:
            try:
                self._sock.sendall((line + "\n").encode("ascii"))
                resp = self._read_one_line(timeout_s=_COMMAND_TIMEOUT_S)
            except OSError as exc:
                self.close()
                raise RigctlError(f"socket error during {line!r}: {exc}") from exc
            except RigctlError:
                self.close()
                raise

        if not resp.startswith("RPRT "):
            raise RigctlError(
                f"expected RPRT response for {line!r}, got {resp!r}"
            )
        self._check_rprt(resp, line)

    def _check_rprt(self, resp: str, command: str) -> None:
        """Parse a ``RPRT N`` line and raise if N != 0."""
        try:
            code = int(resp.split()[1])
        except (IndexError, ValueError) as exc:
            raise RigctlError(
                f"malformed RPRT response: {resp!r}"
            ) from exc
        if code != 0:
            raise RigctlNotOk(code, command)

    def _read_one_line(self, *, timeout_s: float) -> str:
        """Read exactly one line from the socket, with a deadline.

        Buffers across recv() boundaries so partial chunks don't lose
        data. Raises RigctlError on timeout or EOF.
        """
        assert self._sock is not None
        deadline = time.monotonic() + timeout_s

        while True:
            if b"\n" in self._buf:
                line, _, self._buf = self._buf.partition(b"\n")
                return line.decode("ascii", errors="replace").strip()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RigctlError(
                    f"rigctld response timeout; partial buffer={self._buf!r}"
                )
            self._sock.settimeout(min(remaining, 0.5))
            try:
                chunk = self._sock.recv(1024)
            except socket.timeout:
                continue
            if not chunk:
                raise RigctlError("rigctld closed the connection")
            self._buf += chunk

    # ── Context manager convenience ──────────────────────────────────

    def __enter__(self) -> "RigctlClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
