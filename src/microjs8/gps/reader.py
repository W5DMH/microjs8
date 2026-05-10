"""GPS reader thread.

Owns the connection to gpsd over its TCP socket. Yields GpsFix records
to a callback as they arrive. Handles disconnect / reconnect gracefully,
mirroring the keyboard-thread pattern.

In production, the callback marshals the fix into UIState via
``loop.call_soon_threadsafe``. Tests can pass a synchronous callback
that just appends to a list.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

from microjs8.gps.gpsd_client import GpsdClient, GPSD_DEFAULT_HOST, GPSD_DEFAULT_PORT
from microjs8.gps.types import FixKind, GpsFix, no_fix

_log = logging.getLogger(__name__)

# How long to wait between reconnect attempts when gpsd is unreachable
# or the connection drops. 2 s feels right — long enough to not spam,
# short enough that recovery feels instant.
_RECONNECT_DELAY_S = 2.0


FixCallback = Callable[[GpsFix], None]


class GpsReader(threading.Thread):
    """Background thread that streams fixes from gpsd.

    The thread runs forever (until ``stop()``), reconnecting on any
    socket error. On disconnect it emits a synthetic NO_FIX snapshot
    so the UI can display "GPS connection lost" rather than holding
    the last-known position indefinitely.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_fix: FixCallback,
        *,
        host: str = GPSD_DEFAULT_HOST,
        port: int = GPSD_DEFAULT_PORT,
        # Override for tests.
        client_factory: Optional[Callable[[], GpsdClient]] = None,
        name: str = "gps-reader",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._loop = loop
        self._on_fix = on_fix
        self._host = host
        self._port = port
        self._client_factory = client_factory
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        _log.info("gps reader starting (gpsd=%s:%d)", self._host, self._port)
        try:
            while not self._stop_event.is_set():
                client = self._make_client()
                try:
                    client.connect()
                except OSError as exc:
                    # gpsd not yet up, or refused. Wait and retry.
                    _log.debug("gpsd connect failed: %s", exc)
                    self._stop_event.wait(_RECONNECT_DELAY_S)
                    continue

                _log.info("gpsd connected — streaming")
                try:
                    for fix in client.stream(self._stop_event):
                        self._emit(fix)
                except Exception:
                    _log.exception("unexpected gpsd stream error")
                finally:
                    client.close()

                # Connection dropped (either we stopped or gpsd died).
                if not self._stop_event.is_set():
                    _log.info("gpsd disconnected, emitting NO_FIX")
                    self._emit(no_fix(time.monotonic()))
                    self._stop_event.wait(_RECONNECT_DELAY_S)
        finally:
            _log.info("gps reader stopping")

    def _make_client(self) -> GpsdClient:
        if self._client_factory is not None:
            return self._client_factory()
        return GpsdClient(self._host, self._port)

    def _emit(self, fix: GpsFix) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_fix, fix)
        except RuntimeError:
            # Loop already closed — happens during shutdown. Ignore.
            pass
