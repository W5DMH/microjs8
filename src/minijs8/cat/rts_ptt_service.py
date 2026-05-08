"""RTS-PTT service — wraps RtsPttClient with watchdog + reconnect.

Drop-in replacement for ``CatService`` when the radio doesn't use
CAT (FM walkie-talkies via DigiRig, etc.). Public interface matches
``CatService`` so ``RealTxBackend`` can use either without changes.

What's the same as CatService:

  * ``ptt_on`` / ``ptt_off`` — assert/release with watchdog tracking
  * ``ptt_kick`` — reset the watchdog deadline (called between frames
    in a multi-frame burst)
  * ``set_ptt_max_hold`` — per-cycle override for the watchdog
  * ``is_connected`` — port is open and operations are succeeding
  * ``start`` / ``stop`` — lifecycle
  * Watchdog thread — forces PTT release after max-hold expires
  * Reconnect thread — re-opens the port if the USB device cycles

What's different:

  * No rigctld, no TCP, no host/port — the constructor takes the
    serial port path directly.
  * ``get_frequency_hz`` / ``set_frequency_hz`` return None / False
    (no CAT). Callers should check the radio's ``cat_required``
    field before calling these.
  * ``ptt_on`` / ``ptt_off`` are microsecond operations (RTS toggle)
    rather than millisecond TCP round-trips, so the watchdog has a
    tighter response time.

Threading model is identical to CatService — see that file's
docstring for the rationale.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from minijs8.cat.rts_ptt_client import RtsPttClient, RtsPttError

_log = logging.getLogger(__name__)

# How long to wait before retrying a failed open. Same as CatService
# for consistency — operator perceives identical recovery timing
# across radios.
_RECONNECT_DELAY_S = 2.0
# Max time PTT may be held before the watchdog forcibly releases.
# Same default as CatService.
_PTT_MAX_HOLD_S = 20.0

# Sentinel returned by _call when an underlying invocation failed.
# Distinct from None because some methods legitimately return None.
_CALL_FAILED = object()


# Notification callback: called on connection state changes so the
# UI can reflect "PTT OK" / "PTT disconnected" status. Same shape
# as CatService's StatusCallback.
StatusCallback = Callable[[bool], None]


class RtsPttService:
    """Long-lived RTS-PTT controller, thread-safe, watchdog-protected.

    Constructor takes the serial port path (e.g. ``/dev/digirig``)
    rather than a host/port — there's no rigctld in this path.

    Public methods exactly mirror ``CatService`` so the TX backend
    is service-agnostic. The factory in ``ptt_factory.py`` picks
    which service to instantiate based on the radio's
    ``cat_required`` field.
    """

    def __init__(
        self,
        serial_port: str,
        *,
        baudrate: int = 9600,
        on_status_change: Optional[StatusCallback] = None,
    ) -> None:
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._on_status_change = on_status_change
        self._client: Optional[RtsPttClient] = None
        self._connected = False
        # Lock serializes all operations that touch _client.
        self._lock = threading.Lock()
        # Watchdog state.
        self._watchdog_stop = threading.Event()
        self._ptt_held_at: Optional[float] = None
        self._ptt_max_hold_override_s: Optional[float] = None
        # Reconnect state.
        self._reconnect_stop = threading.Event()
        self._reconnect_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

    # ── Status ──────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True iff the port is open and ready for PTT operations."""
        return self._connected

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Begin background open + watchdog. Returns immediately.

        The serial port is opened asynchronously on the reconnect
        thread, so ``start()`` doesn't block startup if the DigiRig
        is plugged in slowly. Idempotent.
        """
        if self._reconnect_thread is not None:
            return
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="rts-ptt-reconnect",
            daemon=True,
        )
        self._reconnect_thread.start()

        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="rts-ptt-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        _log.info(
            "RTS-PTT service started (port=%s)", self._serial_port,
        )

    def stop(self) -> None:
        """Stop the service. Always releases PTT first — defense in
        depth against shutdown-during-TX leaving the radio keyed."""
        # 1. Try to release PTT before tearing down. Multiple safety
        # nets: client.ptt_off (preferred), then close which also
        # releases.
        try:
            with self._lock:
                if self._client is not None and self._client.is_connected:
                    try:
                        self._client.ptt_off()
                    except Exception:
                        _log.exception(
                            "PTT release during shutdown raised"
                        )
        except Exception:
            pass

        self._reconnect_stop.set()
        self._watchdog_stop.set()
        if self._reconnect_thread is not None:
            self._reconnect_thread.join(timeout=3.0)
            self._reconnect_thread = None
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
        self._set_connected(False)

    # ── Public PTT operations ───────────────────────────────────────

    def get_frequency_hz(self) -> Optional[int]:
        """RTS-only radios have no CAT. Always returns None.

        Provided so callers (UI, scheduler) can use the same code
        path regardless of which service they have. Not an error.
        """
        return None

    def set_frequency_hz(self, hz: int) -> bool:
        """RTS-only radios have no CAT. Always returns False.

        Operator must set frequency manually on the radio. Not an
        error — failing softly lets the UI show "set frequency
        unavailable" without breaking the daemon.
        """
        _log.debug(
            "set_frequency_hz(%d) ignored — no CAT for this radio", hz,
        )
        return False

    def ptt_on(self) -> bool:
        """Assert PTT via RTS. Returns True if successful.

        Caller must call ``ptt_off()`` to release. The watchdog
        will forcibly release after _PTT_MAX_HOLD_S regardless (or
        after ``set_ptt_max_hold()``'s override if set).
        """
        if self._call("ptt_on") is _CALL_FAILED:
            return False
        # Record when PTT was asserted so the watchdog can fire.
        self._ptt_held_at = time.monotonic()
        return True

    def ptt_off(self) -> bool:
        """Release PTT. Always safe — calling when already-off is a
        no-op. Clears the watchdog timer AND any per-cycle hold
        override so the next ptt_on() starts fresh."""
        # Clear FIRST so the watchdog doesn't fire on a normal release.
        self._ptt_held_at = None
        self._ptt_max_hold_override_s = None
        return self._call("ptt_off") is not _CALL_FAILED

    def set_ptt_max_hold(self, seconds: float) -> None:
        """Override the watchdog's max-hold for the NEXT PTT cycle.

        Same semantics as CatService.set_ptt_max_hold — see that
        method's docstring.
        """
        if seconds <= 0:
            self._ptt_max_hold_override_s = None
            return
        self._ptt_max_hold_override_s = seconds

    def ptt_kick(self) -> None:
        """Reset the watchdog deadline by re-stamping ``_ptt_held_at``.

        Called between frames in a multi-frame burst so the watchdog
        doesn't fire on a long burst. Same semantics as
        CatService.ptt_kick.
        """
        if self._ptt_held_at is not None:
            self._ptt_held_at = time.monotonic()

    # ── Internal: thread-safe call wrapper ──────────────────────────

    def _call(self, method: str, *args):
        """Run a method on the client under the lock, with reconnect
        handling. Mirrors CatService._call's contract.
        """
        with self._lock:
            if self._client is None or not self._client.is_connected:
                return _CALL_FAILED
            try:
                fn = getattr(self._client, method)
                return fn(*args)
            except RtsPttError as exc:
                _log.warning(
                    "RTS-PTT %s failed: %s — invalidating connection",
                    method, exc,
                )
                # Drop the client so the reconnect thread will retry.
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                self._set_connected(False)
                return _CALL_FAILED
            except Exception:
                _log.exception(
                    "unexpected error in RTS-PTT %s — invalidating", method,
                )
                try:
                    self._client.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                self._client = None
                self._set_connected(False)
                return _CALL_FAILED

    # ── Internal: reconnect loop ────────────────────────────────────

    def _reconnect_loop(self) -> None:
        """Background thread that opens the port and re-opens it on
        failure. Runs forever until stop() sets _reconnect_stop.
        """
        while not self._reconnect_stop.is_set():
            with self._lock:
                already_connected = (
                    self._client is not None
                    and self._client.is_connected
                )
            if already_connected:
                # Sleep until either we get told to stop, or until
                # we should re-check (in case the client got
                # invalidated by _call).
                self._reconnect_stop.wait(timeout=_RECONNECT_DELAY_S)
                continue

            # Try to open.
            client = RtsPttClient(
                port=self._serial_port,
                baudrate=self._baudrate,
            )
            try:
                client.open()
            except RtsPttError as exc:
                _log.debug(
                    "RTS-PTT open attempt failed: %s", exc,
                )
                self._reconnect_stop.wait(timeout=_RECONNECT_DELAY_S)
                continue

            # Success.
            with self._lock:
                self._client = client
            self._set_connected(True)
            _log.info(
                "RTS-PTT port opened — PTT operational at %s",
                self._serial_port,
            )

    def _set_connected(self, connected: bool) -> None:
        """Update the connected flag and fire the status callback."""
        if self._connected == connected:
            return
        self._connected = connected
        if self._on_status_change is not None:
            try:
                self._on_status_change(connected)
            except Exception:
                _log.exception(
                    "RTS-PTT status callback raised; suppressing",
                )

    # ── Internal: watchdog ──────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """Force PTT release if held longer than the max-hold limit.

        Mirrors CatService._watchdog_loop. Runs every 200 ms — fast
        enough to release PTT well within the next slot if a TX hangs,
        slow enough to be cheap on a Pi Zero 2W.
        """
        while not self._watchdog_stop.is_set():
            self._watchdog_stop.wait(timeout=0.2)
            if self._watchdog_stop.is_set():
                break
            held_at = self._ptt_held_at
            if held_at is None:
                continue
            max_hold = (
                self._ptt_max_hold_override_s
                if self._ptt_max_hold_override_s is not None
                else _PTT_MAX_HOLD_S
            )
            held_for = time.monotonic() - held_at
            if held_for > max_hold:
                _log.warning(
                    "PTT watchdog: held for %.1f s (max %.1f) — "
                    "forcibly releasing",
                    held_for, max_hold,
                )
                try:
                    self.ptt_off()
                except Exception:
                    _log.exception(
                        "watchdog ptt_off raised; PTT state uncertain",
                    )
