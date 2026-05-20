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
        """Open the serial port, all modem control lines de-asserted.

        Raises ``RtsPttError`` on failure. Idempotent: calling twice
        is a no-op (does NOT re-open).

        Phase 18.3 hardening: pyserial's ``self._serial.rts = False``
        after open() does not reliably propagate to the CP210x kernel
        driver — observed on RPi OS Bookworm 6.12.x with Silicon Labs
        CP2102N (the Digirig's USB-UART chip). Symptom: the chip
        latches RTS high from the open() syscall and ``.rts = False``
        is a no-op. Result: radio stuck in TX until USB device reset.

        Mitigation: call ``ioctl(TIOCMSET, 0)`` directly on the file
        descriptor — this is the lowest-level modem-control primitive
        the kernel exposes and bypasses all of pyserial's state
        machine. Confirmed to work on this hardware by direct test.
        """
        if self._opened:
            return
        # Lazy imports — host-side tests don't need pyserial installed.
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
                # predictable. We still need the ioctl-based clear
                # below because pyserial's properties don't always
                # propagate on CP210x.
                dsrdtr=False,
            )
            # Belt + suspenders #1: pyserial-level clear.
            self._serial.rts = False
            self._serial.dtr = False

            # Belt + suspenders #2: bypass pyserial entirely and use
            # the kernel's TIOCMSET ioctl to force ALL modem control
            # lines low. This is the primitive that actually works
            # on the CP210x driver — pyserial's properties don't.
            self._force_clear_modem_lines()
        except Exception as exc:
            self._serial = None
            raise RtsPttError(
                f"could not open RTS-PTT serial port {self._port}: {exc}"
            ) from exc

        self._opened = True
        _log.info(
            "RTS-PTT serial port opened: %s (baudrate=%d, rtscts=False, "
            "modem lines force-cleared via ioctl)",
            self._port, self._baudrate,
        )

    def _force_clear_modem_lines(self) -> None:
        """Drive every modem control line (RTS, DTR, etc.) low via ioctl.

        This bypasses pyserial entirely and writes the kernel's
        ``TIOCMSET`` register directly. Used in open() and close() to
        defeat the CP210x's known habit of latching RTS high across
        pyserial property writes.

        Failure is non-fatal because we're called from close() paths
        where the caller wants cleanup to continue even if individual
        steps fail (USB unplug, fd already closed, etc.).
        """
        if self._serial is None:
            return
        try:
            import fcntl
            import struct
            import termios
        except ImportError as exc:
            # On non-Linux hosts (CI dev laptop) these may not exist
            # in the form we expect. Log and continue — the test path
            # injects fakes so it never reaches here in practice.
            _log.debug(
                "ioctl-based modem-line clear unavailable: %s", exc,
            )
            return
        try:
            # TIOCMSET with all-zero bitmask = drive every line low.
            zero = struct.pack("I", 0)
            fcntl.ioctl(self._serial.fileno(), termios.TIOCMSET, zero)
        except Exception as exc:
            # Don't propagate — close() callers must complete the
            # cleanup regardless. The pyserial-level .rts = False
            # ran already so we're at worst no-worse-off than before
            # this defense was added.
            _log.debug(
                "TIOCMSET ioctl failed (non-fatal): %s", exc,
            )

    def close(self) -> None:
        """Close the port, releasing PTT first as a safety measure.

        Idempotent. Safe to call from shutdown paths even if open()
        raised.

        Phase 18.3 hardening: clear modem control lines via ioctl
        BEFORE pyserial's close() runs. The kernel close path doesn't
        reliably drop RTS on CP210x even with HUPCL set, leaving the
        chip latched high. ioctl(TIOCMSET, 0) writes the line state
        register directly, which the chip honors.
        """
        if not self._opened:
            return
        # Make absolutely sure PTT is released before we close — the
        # kernel may leave RTS asserted in some edge cases otherwise.
        if self._serial is not None:
            # First: pyserial-level (in case the ioctl path fails).
            try:
                self._serial.rts = False
                self._serial.dtr = False
            except Exception:
                _log.exception("failed to release RTS/DTR via pyserial during close")
            # Then: ioctl-level (the one that actually works on CP210x).
            self._force_clear_modem_lines()
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

        Phase 18.3: uses ioctl-based clearing because pyserial's
        ``.rts = False`` does not reliably propagate to the CP210x
        driver. See ``_force_clear_modem_lines`` for context.
        """
        if not self._opened or self._serial is None:
            raise RtsPttError(
                f"RTS-PTT port {self._port} is not open"
            )
        try:
            # pyserial-level (no-op on chips where it works, harmless on chips
            # where it doesn't).
            self._serial.rts = False
        except Exception as exc:
            raise RtsPttError(f"RTS release (pyserial) failed: {exc}") from exc
        # ioctl-level — the one that actually drives the CP210x register.
        self._force_clear_modem_lines()

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
