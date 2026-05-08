"""USB keyboard reader.

Owns ``/dev/input/by-id/*-event-kbd`` exclusively. Translates raw evdev
key events into ``KeyEvent`` objects and pushes them into the asyncio
loop via ``loop.call_soon_threadsafe``.

Why a dedicated thread (not async-evdev): evdev's ``read_loop()`` can
block indefinitely when no input is available, and mixing that with
the existing render-thread + GPIO-thread setup keeps lifecycle code
uniform — every input source lives in its own thread, and the asyncio
loop is the meeting point.

Hot-plug behaviour: if the keyboard isn't present at startup OR is
unplugged mid-session, the reader thread retries discovery every 2 s
(silently logging at DEBUG so the journal doesn't fill up). When the
device reappears, reading resumes without daemon restart.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import threading
import time
from typing import Any, Callable, Optional, Protocol

from minijs8.input.events import Key, KeyEvent

_log = logging.getLogger(__name__)

# How long to wait between rediscovery attempts when no keyboard is
# present. 2 s is comfortable — long enough not to spam the journal,
# short enough that hot-plug feels instant from the operator's side.
_RECONNECT_DELAY_S = 2.0

# evdev key code constants we depend on. We import them lazily inside
# the thread so host tests don't need evdev installed.
# Reference: linux/include/uapi/linux/input-event-codes.h


class _UInputDevice(Protocol):
    """Subset of evdev.InputDevice we use."""

    path: str

    def read(self) -> Any: ...
    def fileno(self) -> int: ...
    def close(self) -> None: ...
    def grab(self) -> None: ...
    def ungrab(self) -> None: ...


# evdev keycode → printable character (no shift)
_BASE_CHARS: dict[int, str] = {}
# evdev keycode → printable character (with shift)
_SHIFT_CHARS: dict[int, str] = {}
# evdev keycode → Key enum (function keys)
_FUNCTION_KEYS: dict[int, Key] = {}


def _build_keymaps() -> None:
    """Populate the keymap tables. Called once on first thread start.

    We import evdev.ecodes here (lazily) so host tests work without it.
    """
    if _BASE_CHARS:
        return  # already built

    try:
        from evdev import ecodes  # type: ignore[import-not-found]
    except ImportError:
        # Test/dev environment without evdev — keymaps stay empty.
        # The reader thread won't actually run anyway.
        return

    # Letters a-z
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        kc = getattr(ecodes, f"KEY_{ch.upper()}")
        _BASE_CHARS[kc] = ch
        _SHIFT_CHARS[kc] = ch.upper()

    # Top-row digits and their shift symbols (US layout — most common
    # for handheld keyboards. Spec doesn't lock a layout, so US it is.)
    digit_pairs = [
        ("1", "!"), ("2", "@"), ("3", "#"), ("4", "$"), ("5", "%"),
        ("6", "^"), ("7", "&"), ("8", "*"), ("9", "("), ("0", ")"),
    ]
    for d, sym in digit_pairs:
        kc = getattr(ecodes, f"KEY_{d}")
        _BASE_CHARS[kc] = d
        _SHIFT_CHARS[kc] = sym

    # Punctuation we expect to type (callsigns: '/', grids: nothing
    # extra, message text: lots).
    punct = [
        ("MINUS", "-", "_"),
        ("EQUAL", "=", "+"),
        ("LEFTBRACE", "[", "{"),
        ("RIGHTBRACE", "]", "}"),
        ("SEMICOLON", ";", ":"),
        ("APOSTROPHE", "'", '"'),
        ("GRAVE", "`", "~"),
        ("BACKSLASH", "\\", "|"),
        ("COMMA", ",", "<"),
        ("DOT", ".", ">"),
        ("SLASH", "/", "?"),
    ]
    for name, base, shift in punct:
        kc = getattr(ecodes, f"KEY_{name}")
        _BASE_CHARS[kc] = base
        _SHIFT_CHARS[kc] = shift

    # Function keys → our Key enum
    _FUNCTION_KEYS[ecodes.KEY_LEFT] = Key.LEFT
    _FUNCTION_KEYS[ecodes.KEY_RIGHT] = Key.RIGHT
    _FUNCTION_KEYS[ecodes.KEY_UP] = Key.UP
    _FUNCTION_KEYS[ecodes.KEY_DOWN] = Key.DOWN
    _FUNCTION_KEYS[ecodes.KEY_ENTER] = Key.ENTER
    _FUNCTION_KEYS[ecodes.KEY_KPENTER] = Key.ENTER
    _FUNCTION_KEYS[ecodes.KEY_ESC] = Key.ESC
    _FUNCTION_KEYS[ecodes.KEY_TAB] = Key.TAB
    _FUNCTION_KEYS[ecodes.KEY_BACKSPACE] = Key.BACKSPACE
    _FUNCTION_KEYS[ecodes.KEY_SPACE] = Key.SPACE
    # KEY_DELETE = forward-delete on most US keyboards (often labeled
    # "Del"). Distinct from KEY_BACKSPACE = backspace key — the
    # router uses BACKSPACE for "rub out the last char" semantics in
    # text fields, and DELETE for destructive list operations like
    # removing the focused inbox row.
    _FUNCTION_KEYS[ecodes.KEY_DELETE] = Key.DELETE


# Modifier key codes — populated lazily.
_MOD_LSHIFT: int = 0
_MOD_RSHIFT: int = 0
_MOD_LCTRL: int = 0
_MOD_RCTRL: int = 0
_KEY_CAPSLOCK: int = 0


def _build_modifier_codes() -> None:
    global _MOD_LSHIFT, _MOD_RSHIFT, _MOD_LCTRL, _MOD_RCTRL, _KEY_CAPSLOCK
    if _MOD_LSHIFT:
        return
    try:
        from evdev import ecodes  # type: ignore[import-not-found]
    except ImportError:
        return
    _MOD_LSHIFT = ecodes.KEY_LEFTSHIFT
    _MOD_RSHIFT = ecodes.KEY_RIGHTSHIFT
    _MOD_LCTRL = ecodes.KEY_LEFTCTRL
    _MOD_RCTRL = ecodes.KEY_RIGHTCTRL
    _KEY_CAPSLOCK = ecodes.KEY_CAPSLOCK


# Ctrl-letter combinations the router cares about
_CTRL_KEYS: dict[str, Key] = {
    "h": Key.CTRL_H,
    "q": Key.CTRL_Q,
    "s": Key.CTRL_S,
    "c": Key.CTRL_C,
}


def find_keyboard_device() -> Optional[str]:
    """Look up the USB keyboard device path.

    Prefers the by-id symlink (stable across reboots and across multiple
    keyboards). Falls back to scanning /dev/input/event* for any device
    whose evdev ``capabilities`` includes EV_KEY with a typical letter
    range — that catches keyboards plugged into hubs that don't get
    by-id symlinks for some reason.

    Returns None if no keyboard found.
    """
    by_id_glob = "/dev/input/by-id/*-event-kbd"
    matches = glob.glob(by_id_glob)
    if matches:
        # Pick the lexicographically first — deterministic if the user
        # has somehow plugged in two keyboards.
        return sorted(matches)[0]
    return None


# Type alias for the router callback.
EventCallback = Callable[[KeyEvent], None]


class KeyboardThread(threading.Thread):
    """Reads /dev/input/by-id/*-event-kbd, emits KeyEvent objects.

    Construct with the asyncio loop and a callback. The callback is
    invoked via ``loop.call_soon_threadsafe`` so router state lives
    purely on the asyncio thread.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_event: EventCallback,
        *,
        # Override for tests — accepts an evdev.InputDevice-like.
        device_factory: Optional[Callable[[], Optional[_UInputDevice]]] = None,
        name: str = "kbd-reader",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._loop = loop
        self._on_event = on_event
        self._device_factory = device_factory or self._default_device_factory
        self._stop_event = threading.Event()
        # Modifier state — only the reader thread touches these.
        self._shift_held = False
        self._ctrl_held = False
        self._capslock_on = False
        self._device: Optional[_UInputDevice] = None

    def stop(self) -> None:
        """Request a clean shutdown. Idempotent."""
        self._stop_event.set()
        # If the read_loop is blocked, closing the device unblocks it.
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass

    def run(self) -> None:
        _build_keymaps()
        _build_modifier_codes()
        _log.info("keyboard thread starting")
        try:
            while not self._stop_event.is_set():
                self._device = self._device_factory()
                if self._device is None:
                    self._stop_event.wait(_RECONNECT_DELAY_S)
                    continue
                try:
                    # Take exclusive access of the keyboard so events
                    # don't ALSO go to the kernel's tty (which would
                    # echo them onto the HDMI console). grab() is
                    # release-on-close, so closing the device returns
                    # the keyboard to normal operation — important if
                    # the daemon crashes; gpiozero/evdev will release
                    # the grab on process exit.
                    try:
                        self._device.grab()
                    except OSError as exc:
                        # EBUSY means something else already grabbed it
                        # (rare, but possible if the daemon is restarted
                        # very rapidly). Log and continue without
                        # exclusive access — better than refusing to
                        # work.
                        _log.warning(
                            "could not grab %s exclusively (%s); keys may "
                            "echo on the HDMI console", self._device.path, exc
                        )
                    _log.info("keyboard attached: %s", self._device.path)
                    self._read_until_disconnect(self._device)
                except OSError as exc:
                    # Device went away (cable pulled, etc.). Try to
                    # rediscover.
                    _log.info("keyboard disconnected: %s", exc)
                except Exception:
                    _log.exception("unexpected keyboard read error")
                finally:
                    if self._device is not None:
                        try:
                            self._device.close()
                        except Exception:
                            pass
                        self._device = None
        finally:
            _log.info("keyboard thread stopping")

    @staticmethod
    def _default_device_factory() -> Optional[_UInputDevice]:
        """Open the keyboard device, returning None if unavailable."""
        path = find_keyboard_device()
        if path is None:
            return None
        try:
            from evdev import InputDevice  # type: ignore[import-not-found]
            return InputDevice(path)
        except OSError as exc:
            _log.debug("could not open %s: %s", path, exc)
            return None

    def _read_until_disconnect(self, dev: _UInputDevice) -> None:
        """Pump events until the device throws or stop is requested.

        Uses select() with a short timeout so we can periodically
        check the stop event. evdev's ``read_loop()`` is a blocking
        generator — calling stop() while it's parked inside read()
        would NOT wake the thread up, leaving it stuck and blocking
        the daemon's shutdown sequence.

        With select() + a 200 ms timeout, the thread is responsive to
        stop within at most 200 ms while still being efficient (it
        sleeps in the kernel waiting for either input or the timeout).
        """
        from evdev import categorize, ecodes, KeyEvent as EvKeyEvent  # type: ignore[import-not-found]
        import select

        # The InputDevice is a file-like object with a usable .fileno().
        fd = dev.fileno()  # type: ignore[attr-defined]

        while not self._stop_event.is_set():
            # Wait up to 200 ms for events. select returns the FDs that
            # have data; an empty list means the timeout fired.
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            # read() returns the events that have arrived since the
            # last call — non-blocking now that select said data is ready.
            for event in dev.read():  # type: ignore[attr-defined]
                if self._stop_event.is_set():
                    return
                if event.type != ecodes.EV_KEY:
                    continue
                ke: EvKeyEvent = categorize(event)
                self._handle_evdev_key(ke)

    def _handle_evdev_key(self, ke: Any) -> None:
        """Process a single evdev KeyEvent into our typed KeyEvent."""
        from evdev import KeyEvent as EvKeyEvent  # type: ignore[import-not-found]

        kc = ke.scancode
        state = ke.keystate  # 0=up, 1=down, 2=hold

        # Modifier state tracking — done on press AND release so we don't
        # miss a release event.
        if kc in (_MOD_LSHIFT, _MOD_RSHIFT):
            self._shift_held = state in (EvKeyEvent.key_down, EvKeyEvent.key_hold)
            return
        if kc in (_MOD_LCTRL, _MOD_RCTRL):
            self._ctrl_held = state in (EvKeyEvent.key_down, EvKeyEvent.key_hold)
            return
        if kc == _KEY_CAPSLOCK:
            if state == EvKeyEvent.key_down:
                self._capslock_on = not self._capslock_on
            return

        # We only act on KEY DOWN; ignore release and hold-repeat for
        # function keys (the router doesn't auto-repeat).
        # However, FOR PRINTABLE CHARS we DO honour key_hold so that
        # holding Backspace deletes multiple characters, which feels
        # natural during a typo cleanup.
        if state == EvKeyEvent.key_up:
            return

        # Function key?
        fkey = _FUNCTION_KEYS.get(kc)
        if fkey is not None:
            # Only fire on key_down for non-repeating keys, but let
            # Backspace repeat (state == key_hold) so holding it deletes.
            if fkey is Key.BACKSPACE:
                self._emit(KeyEvent(key=fkey))
            elif state == EvKeyEvent.key_down:
                self._emit(KeyEvent(key=fkey))
            return

        # Printable character — only fire on key_down. Auto-repeat for
        # printables is a future enhancement; for now one press = one
        # character, which is the right default for short fields.
        if state != EvKeyEvent.key_down:
            return

        if self._ctrl_held:
            base = _BASE_CHARS.get(kc)
            if base is not None:
                ctrl_key = _CTRL_KEYS.get(base.lower())
                if ctrl_key is not None:
                    self._emit(KeyEvent(key=ctrl_key))
            return

        # Resolve shifted vs base, factoring in capslock for letters.
        if self._shift_held:
            ch = _SHIFT_CHARS.get(kc)
        else:
            ch = _BASE_CHARS.get(kc)
        if ch is None:
            return

        # Capslock affects only letters and inverts the shift state for them.
        if self._capslock_on and ch.isalpha():
            ch = ch.upper() if ch.islower() else ch.lower()

        self._emit(KeyEvent(char=ch))

    def _emit(self, event: KeyEvent) -> None:
        """Marshal an event into the asyncio loop."""
        try:
            self._loop.call_soon_threadsafe(self._on_event, event)
        except RuntimeError:
            # Loop already closed — happens during shutdown. Ignore.
            pass
