"""UART keyboard backend.

Reads pre-parsed ASCII key events from a UART device (typically the
Pi's hardware UART on `/dev/serial0`) and emits ``KeyEvent`` instances
identical to what the existing USB ``KeyboardThread`` produces.

Used when MicroJS8 is paired with an M5Stack Cardputer ADV via the
Pi's GPIO UART header rather than a USB HID keyboard. The Cardputer
runs the `microjs8-cardputer-link` firmware which scans its 56-key
matrix, resolves Shift / Fn modifiers, and emits one event per line
in the wire format documented below.

Wire format — one event per ``\\n``-terminated line at 115200 8N1:

    CHAR:X       printable ASCII (pre-shift-resolved; 'K' = shift+k)
    ENTER
    TAB
    ESC
    BACKSPACE
    UP / DOWN / LEFT / RIGHT
    # comment    banners, ignored by the parser

Why pre-shifted: keeps the Pi-side router simple — every printable
char is emitted as a single ``KeyEvent(char=X)`` regardless of which
physical modifiers the operator held to produce it. Modifier-tracking
lives in the firmware where it belongs.

Edge-detect: the firmware fires exactly one event per key DOWN edge.
There is no auto-repeat. If the operator wants repeat-on-hold for
arrow keys (e.g. scrolling Inbox), it should be added on the Pi side
in the router, not in this backend.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from microjs8.input.events import Key, KeyEvent

_log = logging.getLogger(__name__)


# ── Wire format → Key mapping ─────────────────────────────────────────
#
# These are the named events the firmware emits for navigation keys.
# Any other line that isn't ``CHAR:X`` or a ``#`` comment is dropped
# silently — we don't want a corrupted byte to crash the daemon.
_NAMED_KEYS: dict[str, Key] = {
    "ENTER":     Key.ENTER,
    "TAB":       Key.TAB,
    "ESC":       Key.ESC,
    "BACKSPACE": Key.BACKSPACE,
    "UP":        Key.UP,
    "DOWN":      Key.DOWN,
    "LEFT":      Key.LEFT,
    "RIGHT":     Key.RIGHT,
}


def parse_line(line: str) -> Optional[KeyEvent]:
    """Parse a single wire-format line into a ``KeyEvent``.

    Pure function — exposed so tests can exercise the parser without
    standing up a real serial port. The thread loop also uses it.

    Returns ``None`` for unparseable input (comments, blank lines,
    unrecognized event names, malformed CHAR lines). Callers should
    treat ``None`` as "drop silently"; we never raise on bad input.

    Whitespace policy: line terminators (``\\r``, ``\\n``) are
    stripped first. CHAR lines are then matched against an EXACT
    6-character format — this preserves a legitimate trailing space
    in ``CHAR: ``. Named events (ENTER / TAB / ESC / etc.) tolerate
    surrounding whitespace via a secondary full strip.
    """
    # Strip only line terminators here, NOT all whitespace — otherwise
    # we'd lose the trailing space in ``CHAR: `` (which is how the
    # space character is reported on the wire).
    raw = line.rstrip("\r\n")
    if not raw:
        return None

    # CHAR lines: exact 6-character match. Preserves trailing space.
    # ``CHAR:X`` where X is one printable ASCII byte (0x20..0x7E).
    if raw.startswith("CHAR:") and len(raw) == 6:
        char = raw[5]
        if 0x20 <= ord(char) < 0x7F:
            return KeyEvent(char=char)
        # Defense against UART noise: a corrupted byte that lands in
        # the X position but isn't printable. The firmware never
        # sends control codes here.
        _log.debug("uart_keyboard: non-printable CHAR dropped: %r", char)
        return None

    # Everything else tolerates surrounding whitespace (operators
    # writing test scripts, stray indentation, etc.).
    text = raw.strip()
    if not text:
        return None
    if text.startswith("#"):
        # Boot banner ("# microjs8-cardputer-link v0.1.0 ready") or
        # other comments. Logged at DEBUG to keep journals quiet.
        _log.debug("uart_keyboard: comment: %s", text)
        return None
    if text in _NAMED_KEYS:
        return KeyEvent(key=_NAMED_KEYS[text])
    _log.debug("uart_keyboard: unknown event: %r", text)
    return None


class UartKeyboardThread(threading.Thread):
    """Background thread that reads key events from a UART device.

    Mirrors the public contract of ``microjs8.input.keyboard.KeyboardThread``:
    each successfully-parsed event is delivered via ``on_event`` callback.
    The router consumes ``KeyEvent`` instances identically regardless of
    which backend produced them.

    Lifecycle:
      - ``__init__`` — store config; do NOT open the device (defer to
        ``run`` so a missing device doesn't crash app startup; the
        thread logs and exits cleanly).
      - ``start`` — standard threading.Thread; calls our ``run``.
      - ``run``   — opens the serial port, loops reading lines until
        ``stop`` is called or an unrecoverable error occurs.
      - ``stop``  — signal the loop to exit; the thread will close the
        port and terminate on the next read timeout.

    Error policy:
      - Open failures log+return (the daemon keeps running headless).
      - Read errors log and continue (transient I/O hiccups recoverable).
      - Decode/parse failures drop silently (handled by ``parse_line``).
      - Callback exceptions log but don't kill the thread (a buggy
        router shouldn't take down keyboard input).
    """

    def __init__(
        self,
        device: str = "/dev/serial0",
        baud: int = 115200,
        on_event: Optional[Callable[[KeyEvent], None]] = None,
    ) -> None:
        super().__init__(name="UartKeyboardThread", daemon=True)
        self._device = device
        self._baud = baud
        self._on_event = on_event
        self._stop_event = threading.Event()
        self._port = None  # opened in run()

    def stop(self) -> None:
        """Signal the read loop to exit. Idempotent."""
        self._stop_event.set()

    def run(self) -> None:
        # Import here so the rest of the module imports cleanly on
        # hosts without pyserial (e.g. CI dev boxes that only need to
        # run the parser tests). The thread is only started when the
        # operator has chosen ``keyboard = "uart"`` in config.
        try:
            import serial  # type: ignore
        except ImportError:
            _log.error(
                "uart_keyboard: pyserial not installed; "
                "install it or switch [hmi] keyboard back to 'usb'"
            )
            return

        try:
            self._port = serial.Serial(
                self._device,
                baudrate=self._baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,  # short enough to react to stop() promptly
            )
        except Exception:
            _log.exception(
                "uart_keyboard: failed to open %s @ %d baud",
                self._device, self._baud,
            )
            return

        _log.info(
            "uart_keyboard: reading from %s @ %d baud",
            self._device, self._baud,
        )

        # Line-buffered reader. We do byte-level reads so we can drop
        # cleanly on stop() without blocking forever on readline().
        # Each `read(64)` returns within `timeout` seconds whether or
        # not bytes arrived, giving us a tick to check `_stop_event`.
        buf = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = self._port.read(64)
            except Exception:
                _log.exception("uart_keyboard: read error; will retry")
                # Brief sleep prevents a hot loop if the port is
                # permanently broken (e.g. someone yanked the cable).
                self._stop_event.wait(0.5)
                continue

            if chunk:
                buf.extend(chunk)
                # Drain whole lines from the buffer. Anything trailing
                # without a newline stays in `buf` for the next pass.
                while b"\n" in buf:
                    raw, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    try:
                        line = raw.decode("ascii", errors="replace")
                    except Exception:
                        # decode("ascii", errors="replace") should
                        # never raise but defend in depth.
                        continue
                    event = parse_line(line)
                    if event is None:
                        continue
                    if self._on_event is None:
                        continue
                    try:
                        self._on_event(event)
                    except Exception:
                        _log.exception(
                            "uart_keyboard: on_event callback raised "
                            "(event=%r) — keyboard thread continuing",
                            event,
                        )

        # Clean shutdown.
        try:
            if self._port is not None:
                self._port.close()
        except Exception:
            _log.exception("uart_keyboard: error closing port")
        _log.info("uart_keyboard: thread exited")
