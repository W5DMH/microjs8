"""CAT service — wraps RigctlClient with reconnect + safety guarantees.

The TxBackend (Step 6) calls ``ptt_on`` / ``ptt_off`` directly on this
service. It exists primarily to:

1. **Manage reconnection** to ``rigctld`` if the connection drops
   (e.g. rigctld restarted, USB unplugged-then-replugged). Without
   this, a transient failure would brick CAT until daemon restart.

2. **Provide a "release PTT no matter what" watchdog**. ``ptt_on()``
   in this service registers a max-hold-time timer; if PTT is still
   asserted after the timer fires (TxBackend should have unkeyed by
   then), we forcibly send ``T 0``. This prevents stuck PTT from
   bugs in the encode/playback path.

3. **Serialize CAT operations** across all threads. The daemon has
   the asyncio loop, the decode thread, the GPS reader, and the
   future TX backend potentially all wanting to read frequency or
   change PTT. CatService is the funnel.

Connection lifecycle handling:

  - ``start()`` connects on a background thread; failures retry.
  - ``stop()`` clean shutdown.
  - On socket error during a command, the inner client invalidates
    itself, and the next operation triggers a reconnect attempt.
  - If reconnect fails repeatedly, we surface the issue via a
    callback so the UI can show "CAT disconnected".
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from minijs8.cat.rigctl_client import (
    RigctlClient,
    RigctlError,
    RigctlNotOk,
)

_log = logging.getLogger(__name__)

# How long to wait before retrying a failed connect.
_RECONNECT_DELAY_S = 2.0
# Max time PTT may be held before the watchdog forcibly releases it.
# JS8 Normal frame is 12.6 s, plus ~0.5 s margin each side; 20 s is
# a generous upper bound that covers any submode.
_PTT_MAX_HOLD_S = 20.0


# Sentinel returned by _call when the underlying invocation failed
# (disconnect, RigctlError, RigctlNotOk). Distinct from None because
# rigctl methods that have no return value return None on success.
_CALL_FAILED = object()


# Notification callback: called on connection state changes so the
# UI can reflect "CAT OK" / "CAT disconnected" status.
StatusCallback = Callable[[bool], None]


class CatService:
    """Long-lived CAT controller, thread-safe, watchdog-protected."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4532,
        on_status_change: Optional[StatusCallback] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_status_change = on_status_change
        self._client: Optional[RigctlClient] = None
        self._connected = False
        # Lock serializes ALL operations that touch _client.
        self._lock = threading.Lock()
        # Watchdog thread + state.
        self._watchdog_stop = threading.Event()
        self._ptt_held_at: Optional[float] = None
        # Per-cycle override for the watchdog's max-hold limit.
        # Set via ``set_ptt_max_hold()`` before start_burst() to give
        # the watchdog enough rope for a multi-frame burst (which can
        # legitimately hold PTT for 60-100+ seconds across multiple
        # slots). Reset to None on every ptt_off so the next cycle
        # falls back to the default _PTT_MAX_HOLD_S.
        self._ptt_max_hold_override_s: Optional[float] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        # Reconnect thread.
        self._reconnect_stop = threading.Event()
        self._reconnect_thread: Optional[threading.Thread] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin background connection + watchdog. Returns immediately."""
        if self._reconnect_thread is not None:
            return
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="cat-reconnect",
            daemon=True,
        )
        self._reconnect_thread.start()

        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="cat-ptt-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        _log.info("CAT service started (rigctld at %s:%d)", self._host, self._port)

    def stop(self) -> None:
        """Stop the service. Always releases PTT first — defense in depth
        against shutdown-during-TX leaving the radio keyed."""
        # 1. Try our best to release PTT before tearing down the connection.
        try:
            with self._lock:
                if self._client is not None and self._client.is_connected:
                    try:
                        self._client.ptt_off()
                    except Exception:
                        _log.exception("PTT release during shutdown raised")
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

    # ── Public CAT operations ────────────────────────────────────────

    def get_frequency_hz(self) -> Optional[int]:
        """Return the radio's current frequency, or None if CAT is down."""
        result = self._call("get_frequency_hz")
        return None if result is _CALL_FAILED else result

    def set_frequency_hz(self, hz: int) -> bool:
        """Set the radio's frequency. Returns True on success."""
        return self._call("set_frequency_hz", hz) is not _CALL_FAILED

    def ptt_on(self) -> bool:
        """Assert PTT. Returns True if successful.

        Caller must call ``ptt_off()`` to release. The watchdog will
        forcibly release after _PTT_MAX_HOLD_S regardless (or after
        ``set_ptt_max_hold()``'s override if set).
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
        # Clear the override so the next PTT cycle falls back to the
        # default — overrides are PER-cycle, not sticky.
        self._ptt_max_hold_override_s = None
        return self._call("ptt_off") is not _CALL_FAILED

    def set_ptt_max_hold(self, seconds: float) -> None:
        """Override the watchdog's max-hold for the NEXT PTT cycle.

        Called by the TX scheduler before ``start_burst()`` to give
        the watchdog enough rope for a multi-frame burst:

            cat.set_ptt_max_hold(n_frames * 20 + 5)
            backend.start_burst()
            # ... transmit_frame() across multiple slots ...
            backend.end_burst()  # ptt_off clears the override

        The override takes effect IMMEDIATELY (the watchdog loop reads
        it on its next tick). Once ``ptt_off()`` runs — whether via
        normal end-of-burst or via the watchdog itself — the override
        is cleared and subsequent PTT cycles use the default
        ``_PTT_MAX_HOLD_S``.

        Pass ``seconds <= 0`` to unset and immediately revert to the
        default (rare; mostly useful for tests).

        This is a thin instrumentation point — the watchdog's
        defense-in-depth role is unchanged. We're just letting the
        scheduler tell us "this is a known-long burst, don't yank
        PTT before it's expected to end."
        """
        if seconds <= 0:
            self._ptt_max_hold_override_s = None
        else:
            self._ptt_max_hold_override_s = float(seconds)

    def ptt_kick(self) -> None:
        """Reset the PTT watchdog timer without changing PTT state.

        Used by long-running multi-frame TX bursts to checkpoint
        progress: the burst spans multiple slots (39+ seconds for a
        3-frame message), well past the watchdog's _PTT_MAX_HOLD_S.
        Calling this between frames says "we're still actively
        progressing through the burst" and keeps the watchdog from
        force-releasing PTT mid-message.

        If the TX path actually hangs (play() blocks forever, scheduler
        thread dies, etc.) without a call to ptt_kick, the watchdog
        still fires within _PTT_MAX_HOLD_S — exactly as designed.

        No-op if PTT is not currently held (e.g. caller raced with
        ptt_off).
        """
        if self._ptt_held_at is not None:
            self._ptt_held_at = time.monotonic()

    # ── Internal: call dispatch with reconnect on failure ────────────

    def _call(self, method: str, *args):
        """Run a method on the wrapped client, handle failures.

        Returns the method's return value, or the ``_CALL_FAILED``
        sentinel if CAT was disconnected or the call raised.

        Using a sentinel rather than None lets the underlying
        method's natural None return value be distinguished from
        "we couldn't even attempt the call".
        """
        with self._lock:
            client = self._client
            if client is None or not client.is_connected:
                return _CALL_FAILED
            try:
                fn = getattr(client, method)
                return fn(*args)
            except RigctlNotOk as exc:
                # Hamlib reported an application error (radio said no).
                # Connection still alive; surface the error to caller
                # via _CALL_FAILED so they know it didn't succeed.
                _log.warning("CAT %s rejected: %s", method, exc)
                return _CALL_FAILED
            except RigctlError as exc:
                # Communication error. The client invalidated itself;
                # the reconnect loop will pick this up.
                _log.warning("CAT %s failed: %s", method, exc)
                self._set_connected(False)
                return _CALL_FAILED

    # ── Reconnect background loop ────────────────────────────────────

    def _reconnect_loop(self) -> None:
        """Maintain the connection. Reconnects on failure forever."""
        while not self._reconnect_stop.is_set():
            with self._lock:
                client = self._client
                connected = client is not None and client.is_connected
            if connected:
                # Periodic ping to keep the connection warm and detect
                # half-open sockets. 5 s strikes a balance between
                # detecting failures fast and CAT bandwidth use.
                self._reconnect_stop.wait(5.0)
                if self._reconnect_stop.is_set():
                    break
                self._ping()
                continue

            # Need to (re)connect.
            try:
                new_client = RigctlClient(self._host, self._port)
                new_client.connect()
                # Sanity check the connection works.
                new_client.ping()
            except (RigctlError, OSError) as exc:
                _log.debug("rigctld connect attempt failed: %s", exc)
                self._reconnect_stop.wait(_RECONNECT_DELAY_S)
                continue

            with self._lock:
                # Replace any stale client.
                if self._client is not None:
                    self._client.close()
                self._client = new_client
            self._set_connected(True)
            _log.info("rigctld connected — CAT operational")

    def _ping(self) -> None:
        """Round-trip the connection to detect drops."""
        with self._lock:
            if self._client is None:
                return
            try:
                self._client.ping()
            except (RigctlError, OSError) as exc:
                _log.warning("CAT ping failed: %s", exc)
                self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        if connected != self._connected:
            self._connected = connected
            if self._on_status_change is not None:
                try:
                    self._on_status_change(connected)
                except Exception:
                    _log.exception("CAT status callback raised")

    # ── PTT watchdog ─────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """Force PTT release if it's been held too long.

        Defense in depth against the encode/playback path getting stuck
        with PTT asserted. Without this, a crash mid-TX would key the
        radio indefinitely until reboot.

        The cap is ``_PTT_MAX_HOLD_S`` by default. The TX scheduler
        can raise it for the duration of a multi-frame burst by
        calling ``set_ptt_max_hold(seconds)`` before ``start_burst()``
        — multi-frame bursts legitimately hold PTT for 60-100+
        seconds. The override is per-cycle: ``ptt_off()`` clears it
        so subsequent TX paths fall back to the default.
        """
        while not self._watchdog_stop.is_set():
            self._watchdog_stop.wait(0.5)
            if self._watchdog_stop.is_set():
                break
            held_at = self._ptt_held_at
            if held_at is None:
                continue
            # Read the override fresh on every tick — the scheduler
            # may set it AFTER ptt_on (depending on the call order)
            # and we want to pick that up.
            cap = self._ptt_max_hold_override_s
            if cap is None:
                cap = _PTT_MAX_HOLD_S
            held_for = time.monotonic() - held_at
            if held_for > cap:
                _log.error(
                    "PTT watchdog firing — PTT held for %.1fs "
                    "(max %.1fs); forcing release",
                    held_for, cap,
                )
                self.ptt_off()
