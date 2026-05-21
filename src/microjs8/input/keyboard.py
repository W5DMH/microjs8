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

from microjs8.input.events import Key, KeyEvent

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


# Ctrl-letter combinations the router cares about.
#
# Phase 19 (v0.0.8): removed Ctrl-S/Ctrl-Q/Ctrl-H/Ctrl-C as user-
# facing shortcuts. Operators now navigate exclusively via arrow
# keys + the on-screen Exit button (HOME), per the v0.0.8 UX
# simplification. Ctrl-B for the backlight gesture is NOT in this
# dict — it's handled via the FN_B remap one block below (the USB
# keyboard path) and via dedicated TCA8418 scancodes on Cardputer-
# Zero. Keeping this dict empty makes the gap explicit: no Ctrl+
# letter shortcuts reach the router from the keyboard layer.
_CTRL_KEYS: dict[str, Key] = {}


# ── CardputerZero Fn-modified keycodes ──────────────────────────────
#
# The CardputerZero's TCA8418 I²C keypad goes through a kernel keymap
# (``/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map``) that
# translates Fn+key combinations into distinct evdev keycodes BEFORE
# we see them. Userspace doesn't track ``Fn`` as a modifier — we just
# bind whatever keycodes the keymap produces for ``Fn+B`` and ``Fn+Q``.
#
# The exact integer values are unconfirmed until first hardware. To
# discover them, run on the device::
#
#     evtest /dev/input/by-path/platform-3f804000.i2c-event
#
# and press Fn+B then Fn+Q while watching the output. Then either:
#   - update the defaults below, or
#   - set ``MICROJS8_FN_B_KEYCODE`` / ``MICROJS8_FN_Q_KEYCODE`` in the
#     systemd unit (no code change needed for bring-up).
#
# Placeholders are KEY_F11 (87) and KEY_F12 (88) — both are real evdev
# keycodes that won't collide with anything on the 46-key matrix
# layout, so unit tests can exercise the dispatch path safely.
import os as _os
_FN_B_SCANCODE: int = int(_os.environ.get("MICROJS8_FN_B_KEYCODE", 87))
_FN_Q_SCANCODE: int = int(_os.environ.get("MICROJS8_FN_Q_KEYCODE", 88))


def find_keyboard_device() -> Optional[str]:
    """Look up the keyboard device path (legacy single-device API).

    Prefers the by-id symlink (stable across reboots and across multiple
    keyboards). Returns None if no keyboard found.

    Phase 16: this function is preserved for backward compatibility
    but new callers should use ``discover_keyboards()`` which returns
    a tagged list of all available keyboards (TCA8418 + USB
    simultaneously, for hosts with both).
    """
    keyboards = discover_keyboards()
    if not keyboards:
        return None
    # Preference order: TCA8418 first (matches previous behaviour on
    # the CardputerZero), then USB. Within each source, lexicographic.
    keyboards.sort(key=lambda kb: (0 if kb[0] == "tca8418" else 1, kb[1]))
    return keyboards[0][1]


# Phase 16: keyboard source tags. The reader uses this to decide
# whether to apply the Ctrl+B → FN_B remap (USB only — the TCA8418
# kernel keymap already produces FN_B/FN_Q scancodes directly, and
# remapping Ctrl on the TCA8418 would steal the operator's Ctrl+Q
# = ALLCALL hotkey).
_SOURCE_TCA8418 = "tca8418"
_SOURCE_USB = "usb"
KeyboardSource = str  # typing alias: one of "tca8418" | "usb"


def _classify_keyboard(by_id_path: str) -> KeyboardSource:
    """Classify a /dev/input/by-id/*-event-kbd symlink.

    The TCA8418 reaches userspace via the kernel input subsystem
    just like a USB keyboard, but the by-id symlink name encodes
    the bus type: USB devices have ``usb-`` in the name, while
    platform/I²C devices have ``platform-`` or ``i2c-``.

    We default UNKNOWN paths to ``usb`` (the safer default — Ctrl+B
    remap is a no-op if the operator never presses Ctrl+B, but
    missing it on an unrecognised device would silently break the
    backlight gesture on USB hardware that doesn't match our regex).
    """
    name = by_id_path.rsplit("/", 1)[-1].lower()
    if "tca8418" in name or name.startswith("platform-") or name.startswith("i2c-"):
        return _SOURCE_TCA8418
    return _SOURCE_USB


def discover_keyboards() -> list[tuple[KeyboardSource, str]]:
    """Return all available keyboards, tagged by source.

    Phase 16: a host can have both a TCA8418 (CardputerZero on-board
    keypad) and one or more USB keyboards plugged in. Both can be
    used simultaneously — the daemon spawns one reader thread per
    discovered device.

    Returns a list of (source, path) tuples. Empty list means no
    keyboard at all (the daemon will log an error and exit non-zero
    so systemd doesn't loop).
    """
    by_id_glob = "/dev/input/by-id/*-event-kbd"
    paths = sorted(glob.glob(by_id_glob))
    out: list[tuple[KeyboardSource, str]] = []
    for p in paths:
        out.append((_classify_keyboard(p), p))
    return out


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
        # Phase 16: source tag controls Fn-mapping behaviour.
        #   "tca8418" — keep Phase 3 behaviour; Fn+B/Fn+Q arrive as
        #     dedicated scancodes via the kernel keymap; Ctrl+letter
        #     emits Key.CTRL_* (Ctrl+Q reaches ALLCALL navigation).
        #   "usb"     — no Fn key on most USB keyboards; remap
        #     Ctrl+B to Key.FN_B so the backlight gesture is still
        #     reachable. Ctrl+Q is left as Key.CTRL_Q so ALLCALL
        #     navigation stays available (USB-only shutdown uses
        #     ssh + systemctl until a config-driven gesture lands).
        source: KeyboardSource = _SOURCE_TCA8418,
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
        self._source: KeyboardSource = source

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

        # CardputerZero Fn+B (backlight toggle) — press-only, like other
        # function keys. Auto-repeat from a long hold is treated as a
        # single press: it's harmless because the router's toggle is
        # debounced at the backlight layer.
        if kc == _FN_B_SCANCODE:
            if state == EvKeyEvent.key_down:
                self._emit(KeyEvent(key=Key.FN_B))
            return

        # CardputerZero Fn+Q (shutdown gesture) — emit BOTH press and
        # release so the gesture state machine can arm and cancel.
        # ``key_hold`` events (auto-repeat) are NOT re-emitted: the
        # gesture's ``arm()`` is idempotent and a stream of "press"
        # events during the 3-second hold would be wasted work.
        if kc == _FN_Q_SCANCODE:
            if state == EvKeyEvent.key_down:
                self._emit(KeyEvent(key=Key.FN_Q, pressed=True))
            elif state == EvKeyEvent.key_up:
                self._emit(KeyEvent(key=Key.FN_Q, pressed=False))
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
                # Phase 16: USB keyboards have no Fn key (firmware-
                # handled; the keycode never reaches userspace).
                # Remap Ctrl+B to Key.FN_B so the backlight gesture
                # is still reachable for USB-only setups. We do NOT
                # remap Ctrl+Q because that's a Phase 11 ALLCALL
                # navigation hotkey — operators have learned that
                # binding and overloading it would surprise them.
                if self._source == _SOURCE_USB and base.lower() == "b":
                    self._emit(KeyEvent(key=Key.FN_B))
                    return
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
