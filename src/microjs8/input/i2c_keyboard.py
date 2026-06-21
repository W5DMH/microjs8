"""I2C keyboard backend.

Reads single-byte key events from an I2C keyboard (typically the
M5Stack CardKB v1.1 at address 0x5F) and emits ``KeyEvent`` instances
identical to what the existing USB / UART backends produce.

Used when MicroJS8 is paired with an M5Stack CardKB v1.1 unit
(Digi-Key 2221-U035-B-ND, M5 SKU U035-B) via the Pi's I2C bus on
GPIO 2 (SDA, physical pin 3) and GPIO 3 (SCL, physical pin 5).
See ``docs/I2C_KEYBOARD.md`` for the hardware setup.

Wire format -- the CardKB exposes a single byte at I2C address 0x5F.
Each read returns:

    0x00         idle (no key pressed since last read)
    0x08         Backspace
    0x09         Tab
    0x0D         Enter
    0x1B         Esc
    0x20-0x7E    printable ASCII (lowercase by default; Shift+key
                 gives uppercase, Sym+key gives the second-character
                 value per M5Stack's CardKB user guide)
    0xB4         Left arrow
    0xB5         Up arrow
    0xB6         Down arrow
    0xB7         Right arrow
    0x80-0x9F    Fn+key special codes (we don't map these because
                 they don't correspond to MicroJS8 navigation; logged
                 at DEBUG and dropped silently)

Polling rate: 33 Hz (30 ms interval), set via
``DEFAULT_POLL_INTERVAL_S``. Below the human-perception typing-latency
threshold (~50 ms feels snappy) while keeping CPU overhead negligible
(one I2C read every 30 ms is roughly 0.1% CPU on a Pi Zero 2W).

Why no edge tracking on this side: the CardKB hardware buffers
keystrokes internally and returns one byte per key DOWN edge.
Subsequent reads return 0x00 until the next press. The polling cadence
determines responsiveness, not edge detection. If the operator wants
repeat-on-hold (e.g. scrolling Inbox), it should be added in the
router, not here.

Why a Thread (not asyncio): symmetry with the UART backend, which
already uses a blocking Thread because pyserial is sync. The smbus2
read is also blocking. Marshalling events back to the asyncio loop
is the caller's responsibility (the App wraps ``on_event`` with
``loop.call_soon_threadsafe``).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from microjs8.input.events import Key, KeyEvent

_log = logging.getLogger(__name__)


# Default polling cadence. 30 ms = ~33 Hz.
# Operators can override via ``[hmi] i2c_poll_ms`` in config if a
# specific keyboard variant needs a different rate. Keep this value
# in sync with docs/I2C_KEYBOARD.md.
DEFAULT_POLL_INTERVAL_S = 0.030

# Default I2C bus number. ``/dev/i2c-1`` is the standard userspace
# I2C interface on Pi GPIO 2/3.
DEFAULT_BUS = 1

# Default I2C address for the M5Stack CardKB v1.1.
# Per the CardKB datasheet this is fixed at 0x5F.
DEFAULT_ADDRESS = 0x5F


# -- Byte -> Key mapping for non-printable special keys ---------------
# The CardKB protocol is direct ASCII for most keys, with a small
# special-code range (0xB4-0xB7) for arrows. Unknown bytes are
# logged at DEBUG and dropped (handled in decode_byte below).

_CONTROL_KEYS: dict[int, Key] = {
    0x08: Key.BACKSPACE,
    0x09: Key.TAB,
    0x0D: Key.ENTER,
    0x1B: Key.ESC,
}

_ARROW_KEYS: dict[int, Key] = {
    0xB4: Key.LEFT,
    0xB5: Key.UP,
    0xB6: Key.DOWN,
    0xB7: Key.RIGHT,
}


def decode_byte(byte: int) -> Optional[KeyEvent]:
    """Decode a single CardKB byte into a KeyEvent.

    Pure function -- exposed so tests can exercise the decoder
    without standing up a real I2C bus. The thread loop also uses it.

    Parameters
    ----------
    byte : int
        A single byte (0..255) read from I2C address 0x5F.

    Returns
    -------
    Optional[KeyEvent]
        ``None`` for unmapped or idle bytes (drop silently).
        ``KeyEvent(char=X)`` for printable ASCII.
        ``KeyEvent(key=Key.X)`` for control / arrow keys.

    Notes
    -----
    Decoding order:
      1. 0x00 -> idle, no key -- return None
      2. Control codes (Backspace / Tab / Enter / Esc) -> Key event
      3. Printable ASCII 0x20-0x7E -> char event
      4. Arrow codes 0xB4-0xB7 -> Key event
      5. Everything else (Fn+key, noise) -> None at DEBUG

    The unmapped Fn+key range (0x80-0x9F) is dropped silently rather
    than misinterpreted. If a future CardKB firmware revision starts
    sending bytes we should handle, this function is where to add them.
    """
    if byte == 0x00:
        return None

    if byte in _CONTROL_KEYS:
        return KeyEvent(key=_CONTROL_KEYS[byte])

    if 0x20 <= byte <= 0x7E:
        return KeyEvent(char=chr(byte))

    if byte in _ARROW_KEYS:
        return KeyEvent(key=_ARROW_KEYS[byte])

    # Unknown -- includes Fn+key codes (0x80-0x9F observed in field
    # test) and any bytes outside the documented ranges. We log at
    # DEBUG to keep the journal quiet while still leaving a paper
    # trail if an operator opens an issue about a "non-working key".
    _log.debug("i2c_keyboard: unmapped byte 0x%02x", byte)
    return None


class I2cKeyboardThread(threading.Thread):
    """Background thread that polls an I2C keyboard.

    Mirrors the public contract of
    ``microjs8.input.uart_keyboard.UartKeyboardThread``:
    each successfully-decoded event is delivered via the ``on_event``
    callback. The router consumes ``KeyEvent`` instances identically
    regardless of which backend produced them, so adding this third
    backend required no router changes.

    Lifecycle
    ---------
    __init__ : store config, create stop event. Does NOT open the
               I2C bus -- defer to ``run`` so a missing or
               misconfigured device doesn't crash app startup; the
               thread logs and exits cleanly.
    start    : inherited from ``threading.Thread``; calls ``run``.
    run      : opens the bus, polls until ``stop`` is called or
               an unrecoverable error.
    stop     : signal the loop to exit; idempotent.

    Error policy
    ------------
    smbus2 import failure : log+return (daemon keeps running without
                            I2C input; in auto mode the USB / UART
                            paths still work).
    Bus open failure       : log+return (typically means I2C isn't
                            enabled in /boot/firmware/config.txt or
                            i2c-dev isn't loaded; postinst should
                            handle both on fresh installs).
    Read errors            : log and continue (transient I2C hiccups
                            recoverable; CardKB unplugged -> we
                            simply read 0x00 once it's reconnected).
    Callback exceptions    : log but don't kill the thread (a buggy
                            router shouldn't take down keyboard input).
    """

    def __init__(
        self,
        bus: int = DEFAULT_BUS,
        address: int = DEFAULT_ADDRESS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        on_event: Optional[Callable[[KeyEvent], None]] = None,
    ) -> None:
        super().__init__(name="I2cKeyboardThread", daemon=True)
        self._bus_num = bus
        self._address = address
        self._poll_interval = poll_interval
        self._on_event = on_event
        self._stop_event = threading.Event()
        self._bus = None  # opened in run()

    def stop(self) -> None:
        """Signal the poll loop to exit. Idempotent."""
        self._stop_event.set()

    def run(self) -> None:
        # Lazy import so the rest of the module imports cleanly on
        # hosts without smbus2 (e.g. CI dev boxes that only need to
        # run the decode_byte tests). The thread is only started when
        # the operator has chosen ``keyboard = "auto"`` or ``"i2c"``
        # in config -- both require smbus2 at runtime.
        try:
            from smbus2 import SMBus  # type: ignore
        except ImportError:
            _log.error(
                "i2c_keyboard: smbus2 not installed; install it "
                "(pip install --break-system-packages smbus2) or "
                "switch [hmi] keyboard back to 'usb' or 'uart'"
            )
            return

        try:
            self._bus = SMBus(self._bus_num)
        except Exception:
            _log.exception(
                "i2c_keyboard: failed to open /dev/i2c-%d -- "
                "is I2C enabled? Check 'dtparam=i2c_arm=on' in "
                "/boot/firmware/config.txt and that i2c-dev is in "
                "/etc/modules",
                self._bus_num,
            )
            return

        # v0.0.18: probe for device presence with a single read before
        # entering the poll loop. The CardKB returns 0x00 when idle so
        # a successful read confirms the device is on the bus. If the
        # read raises (no device at the address, no ack), log once at
        # INFO and exit cleanly -- on hosts running auto-mode without
        # a CardKB attached (e.g. uConsole with only the built-in USB
        # keyboard), this avoids the per-poll OSError flood that the
        # v0.0.16/v0.0.17 implementation produced (33 Hz tracebacks in
        # the journal indefinitely).
        #
        # We do not retry the probe: if the bus is up but nothing
        # answers at 0x5F at startup, plugging a CardKB in later still
        # works because the operator can restart microjs8 to re-probe.
        # The alternative (keep retrying forever) was the v0.0.17
        # behavior and was the source of the journal flood.
        try:
            self._bus.read_byte(self._address)
        except Exception as exc:
            _log.info(
                "i2c_keyboard: no device responds at 0x%02x on "
                "/dev/i2c-%d (%s); i2c keyboard backend disabled. "
                "USB and UART keyboard backends remain active. "
                "Restart microjs8 after plugging in a CardKB to "
                "re-enable.",
                self._address, self._bus_num, exc,
            )
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None
            return

        _log.info(
            "i2c keyboard thread started: bus=/dev/i2c-%d "
            "address=0x%02x poll=%dms",
            self._bus_num, self._address,
            int(self._poll_interval * 1000),
        )

        # Poll loop. Each iteration:
        #   1. Wait for poll interval (or until stop is set)
        #   2. Read one byte from the device
        #   3. Decode and dispatch
        #
        # Using stop_event.wait(timeout) as the cadence source means
        # stop() wakes the thread immediately instead of waiting up
        # to one full poll interval. wait() returns True on stop,
        # False on timeout -- we exit cleanly on True.
        while not self._stop_event.wait(self._poll_interval):
            try:
                byte = self._bus.read_byte(self._address)
            except Exception:
                _log.exception(
                    "i2c_keyboard: read error from 0x%02x; will retry",
                    self._address,
                )
                # Brief backoff prevents a hot loop if the device is
                # gone (cable unplugged, hardware died). Resumes
                # normal polling on next iteration.
                self._stop_event.wait(0.5)
                continue

            event = decode_byte(byte)
            if event is None:
                continue
            if self._on_event is None:
                continue
            try:
                self._on_event(event)
            except Exception:
                _log.exception(
                    "i2c_keyboard: on_event callback raised "
                    "(event=%r) -- keyboard thread continuing",
                    event,
                )

        # Clean shutdown.
        try:
            if self._bus is not None:
                self._bus.close()
        except Exception:
            _log.exception("i2c_keyboard: error closing bus")
        _log.info("i2c_keyboard: thread exited")
