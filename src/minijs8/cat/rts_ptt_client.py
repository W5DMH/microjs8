"""RTS-PTT client — direct pyserial RTS toggle for radios without CAT.

For radios that don't have CAT (FM walkie-talkies, uSDX, TRX-DUO,
etc.) we use a DigiRig Mobile interface and toggle its serial
port's RTS line to key/unkey the radio. The DigiRig has a CP2102
USB-serial bridge whose RTS line drives an optoisolator that grounds
the radio's PTT input.

This module is the thin pyserial wrapper. ``RtsPttService`` (sibling
file) wraps it with the same lifecycle / watchdog guarantees the
``CatService`` provides for CAT-controlled radios.

Why direct pyserial rather than rigctld with model 1 (Dummy):

  * No rigctld process to manage — saves a process and avoids the
    "rigctld can't open the port at startup" failure mode
  * Simpler: open port, toggle RTS, done
  * Direct control over edge timing (rigctld adds a few ms of TCP
    round-trip + its own internal scheduling)

Connection lifecycle:

  * ``RtsPttClient`` keeps the serial port open continuously while
    the daemon is running (cheap; ~50 byte fd) so PTT toggles are
    just RTS line writes (microseconds, no port-open overhead).
  * The port stays at default settings (RTS=False, DTR=False) when
    not transmitting. NOTE: Linux briefly asserts RTS high when the
    port is FIRST opened — this causes a momentary PTT key. The
    DigiRig community considers this a known and unavoidable
    behavior; we open the port at startup and never close it
    until shutdown so we only see this glitch once per daemon
    lifetime.

Failure modes:

  * Port file disappears (USB unplug): pyserial raises SerialException
    on the next write. ``RtsPttService.ptt_on/off`` catches this and
    surfaces it as is_connected → False.
  * Port file present but locked by another process: open() raises
    SerialException("Could not exclusively lock"). Reported clearly.
  * Bit-flips / hardware glitches: out of scope; PTT watchdog (in
    RtsPttService) is the safety net.
"""

from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger(__name__)


class RtsPttError(Exception):
    """Raised when RTS-PTT serial port operations fail."""


class RtsPttClient:
    """Owns a pyserial port; toggles RTS to assert/release PTT.

    Stateless apart from the open file descriptor. Thread safety
    is the caller's responsibility — ``RtsPttService`` serializes
    via its lock.

    Construction does NOT open the port — call ``open()``. This
    matches ``RigctlClient`` lifecycle so the two can be swapped at
    the service layer without behavioral surprises.
    """

    def __init__(
        self,
        port: str,
        *,
        # Baud rate has NO effect on RTS-PTT (we never send data),
        # but pyserial requires a value to open the port.
        baudrate: int = 9600,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._serial = None  # type: ignore[assignment]
        self._opened = False

    # ── Lifecycle ───────────────────────────────────────────────────

    def open(self) -> None:
        """Open the serial port, RTS de-asserted (PTT released).

        Raises ``RtsPttError`` on failure. Idempotent: calling twice
        is a no-op (does NOT re-open).
        """
        if self._opened:
            return
        # Lazy import — host-side tests don't need pyserial installed.
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RtsPttError(
                "pyserial not installed — required for RTS-PTT radios"
            ) from exc

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                timeout=1.0,
                # IMPORTANT: rtscts=False so we control RTS manually.
                # If rtscts is True, the kernel manages RTS for flow
                # control and we can't drive PTT via RTS toggling.
                rtscts=False,
                # Default these to False so the line state is
                # predictable from the moment the port opens. Linux
                # briefly asserts RTS high during open() before our
                # first write — this is a known DigiRig-community
                # behavior we can't avoid (the optoisolator briefly
                # keys PTT for a few ms during startup).
                dsrdtr=False,
            )
            # Belt-and-suspenders: assert PTT-released state right
            # after open. Some kernels leave the line state from the
            # previous user.
            self._serial.rts = False
            self._serial.dtr = False
        except Exception as exc:
            self._serial = None
            raise RtsPttError(
                f"could not open RTS-PTT serial port {self._port}: {exc}"
            ) from exc

        self._opened = True
        _log.info(
            "RTS-PTT serial port opened: %s (baudrate=%d, rtscts=False)",
            self._port, self._baudrate,
        )

    def close(self) -> None:
        """Close the port, releasing PTT first as a safety measure.

        Idempotent. Safe to call from shutdown paths even if open()
        raised.
        """
        if not self._opened:
            return
        # Make absolutely sure PTT is released before we close — the
        # kernel may leave RTS asserted in some edge cases otherwise.
        if self._serial is not None:
            try:
                self._serial.rts = False
            except Exception:
                _log.exception("failed to release RTS during close")
            try:
                self._serial.close()
            except Exception:
                _log.exception("failed to close RTS-PTT serial port")
        self._serial = None
        self._opened = False
        _log.info("RTS-PTT serial port closed: %s", self._port)

    @property
    def is_connected(self) -> bool:
        """True iff the serial port is currently open."""
        return self._opened and self._serial is not None

    # ── PTT operations ──────────────────────────────────────────────

    def ptt_on(self) -> None:
        """Assert PTT (drive RTS high).

        Raises ``RtsPttError`` if the port isn't open or pyserial
        fails. ``RtsPttService`` translates this into a clean
        is_connected → False transition.
        """
        if not self._opened or self._serial is None:
            raise RtsPttError(
                f"RTS-PTT port {self._port} is not open"
            )
        try:
            self._serial.rts = True
        except Exception as exc:
            raise RtsPttError(f"RTS assert failed: {exc}") from exc

    def ptt_off(self) -> None:
        """Release PTT (drive RTS low).

        Raises ``RtsPttError`` on failure. Always-safe in the sense
        that calling when already-off is fine — the kernel writes
        the line state regardless of current state.
        """
        if not self._opened or self._serial is None:
            raise RtsPttError(
                f"RTS-PTT port {self._port} is not open"
            )
        try:
            self._serial.rts = False
        except Exception as exc:
            raise RtsPttError(f"RTS release failed: {exc}") from exc

    # ── Stub CAT operations (RTS-only radios have no CAT) ───────────
    # These exist for API symmetry with RigctlClient. The factory
    # ensures we never construct an RtsPttService for a CAT-required
    # radio, so these should never be called in practice. They raise
    # rather than silently lie so a programmer error gets caught.

    def get_frequency_hz(self) -> int:
        raise RtsPttError(
            "RTS-PTT-only radios have no CAT — frequency is "
            "operator-managed on the radio's front panel"
        )

    def set_frequency_hz(self, hz: int) -> None:
        raise RtsPttError(
            "RTS-PTT-only radios have no CAT — frequency is "
            "operator-managed on the radio's front panel"
        )
