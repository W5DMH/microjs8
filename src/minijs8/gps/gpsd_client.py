"""Minimal gpsd JSON-protocol client.

gpsd accepts TCP connections on port 2947 and emits line-delimited JSON.
Sending ``?WATCH={"enable":true,"json":true}`` opens the firehose. We
care about ``TPV`` reports (time, position, velocity); ``SKY`` reports
(satellite info) we use only to populate the satellites_used field.

We deliberately avoid the ``gpsd-py3`` PyPI package because it's
unmaintained (last release 2017) and is a thin wrapper around the same
JSON socket we can implement in 50 lines of stdlib. Fewer dependencies
== fewer aarch64 wheel headaches at image-build time.

Reference: https://gpsd.io/gpsd_json.html
"""

from __future__ import annotations

import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

from minijs8.gps.types import FixKind, GpsFix

_log = logging.getLogger(__name__)

GPSD_DEFAULT_HOST = "127.0.0.1"
GPSD_DEFAULT_PORT = 2947

# Watch command to start the JSON firehose.
_WATCH_REQUEST = b'?WATCH={"enable":true,"json":true}\n'

# Read buffer for JSON lines. gpsd lines are typically <500 bytes;
# 4 KiB is a comfortable upper bound.
_RECV_BUFSIZE = 4096

# Connect timeout (gpsd is local; if it's down longer than this,
# something is wrong and we should surface that).
_CONNECT_TIMEOUT_S = 3.0
# socket.recv timeout — we want to wake periodically to check the
# stop event in the calling thread, similar to the keyboard pattern.
_RECV_TIMEOUT_S = 0.5


def _parse_iso8601_utc(s: str) -> Optional[float]:
    """gpsd emits ISO 8601 UTC timestamps like '2026-04-28T18:00:00.000Z'."""
    try:
        # Python's fromisoformat accepts +00:00 but not 'Z' until 3.11+.
        # We're 3.11+, so fromisoformat handles it directly.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _tpv_to_fix(tpv: dict, now: float, satellites_used: Optional[int]) -> GpsFix:
    """Translate a gpsd TPV record into our GpsFix dataclass."""
    mode = tpv.get("mode", 0)
    try:
        kind = FixKind(mode)
    except ValueError:
        kind = FixKind.UNKNOWN

    return GpsFix(
        kind=kind,
        lat=tpv.get("lat"),
        lon=tpv.get("lon"),
        altitude_m=tpv.get("altMSL", tpv.get("alt")),
        speed_mps=tpv.get("speed"),
        track_deg=tpv.get("track"),
        hdop=None,  # populated by SKY reports
        fix_time=_parse_iso8601_utc(tpv["time"]) if "time" in tpv else None,
        satellites_used=satellites_used,
        received_at=now,
    )


class GpsdClient:
    """Connect to gpsd and yield GpsFix snapshots.

    Construct, then call ``stream(stop_event)`` in a thread; it yields
    every TPV report until stop_event is set or the connection drops.
    On any socket error the client cleans up and returns; the caller is
    responsible for reconnect logic (the reader thread does this).

    Why a class and not a function: we need to hold the socket and a
    little parsing state (last-seen satellites_used from SKY reports) —
    cleaner as instance fields than as closure variables.
    """

    def __init__(
        self,
        host: str = GPSD_DEFAULT_HOST,
        port: int = GPSD_DEFAULT_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._satellites_used: Optional[int] = None
        # Buffer for partial JSON lines split across recv() calls.
        self._line_buf: bytes = b""

    def connect(self) -> None:
        """Open the TCP connection and send the WATCH request.

        Raises socket.error / ConnectionRefusedError / TimeoutError
        on failure — caller catches.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT_S)
        sock.connect((self._host, self._port))
        sock.sendall(_WATCH_REQUEST)
        # Switch to a shorter recv timeout for the streaming phase.
        sock.settimeout(_RECV_TIMEOUT_S)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._line_buf = b""
        self._satellites_used = None

    def stream(self, stop_event) -> Iterator[GpsFix]:
        """Yield GpsFix records as they arrive.

        ``stop_event`` is a threading.Event; the loop checks it between
        recv calls so the thread can shut down within ~0.5 s.

        On socket error the iterator returns; caller is expected to
        ``close()`` and (if desired) reconnect.
        """
        if self._sock is None:
            raise RuntimeError("call connect() before stream()")

        while not stop_event.is_set():
            try:
                chunk = self._sock.recv(_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError as exc:
                _log.info("gpsd socket error: %s", exc)
                return
            if not chunk:
                # Peer closed cleanly.
                _log.info("gpsd closed the connection")
                return

            self._line_buf += chunk
            while b"\n" in self._line_buf:
                line, _, self._line_buf = self._line_buf.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                fix = self._parse_line(line)
                if fix is not None:
                    yield fix

    def _parse_line(self, line: bytes) -> Optional[GpsFix]:
        """Parse one JSON line. TPV → GpsFix; SKY → updates sat count
        and returns None; everything else returns None.

        Malformed JSON is logged once at DEBUG and dropped — gpsd is
        reliable enough that this should never happen, but we don't
        want a single bad line to kill the stream.
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.debug("gpsd JSON decode error: %s on line %r", exc, line[:80])
            return None

        cls = obj.get("class")
        if cls == "TPV":
            return _tpv_to_fix(obj, time.monotonic(), self._satellites_used)
        if cls == "SKY":
            # Count satellites with .used == True
            sats = obj.get("satellites") or []
            used = sum(1 for s in sats if s.get("used"))
            self._satellites_used = used if sats else None
            return None
        # VERSION / DEVICES / WATCH responses — ignored.
        return None
