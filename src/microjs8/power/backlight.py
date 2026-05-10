"""LCD backlight control via the Linux sysfs ``backlight`` class.

Why sysfs not a userspace library: the CardputerZero's backlight is
already a kernel-managed PWM device. The ``/sys/class/backlight/backlight/``
node is created and torn down by the device-tree overlay; reading and
writing the ``brightness`` file is the standard interface — no library
dependency, no GPIO claim, no permission gymnastics beyond the standard
udev rule that gives the ``video`` group write access.

Confirmed paths from the M5CardputerZero-UserDemo's APPLaunch HAL
(``projects/APPLaunch/main/hal/linux/hal_settings_linux.cpp``):

    /sys/class/backlight/backlight/brightness        (RW, integer)
    /sys/class/backlight/backlight/max_brightness    (RO, integer)

The directory is literally named ``backlight`` — that's the kernel
device name on this hardware, not a placeholder.

Toggle semantics: binary on/off. When the operator presses ``Fn+B`` and
we're currently lit, we cache the current brightness and write 0. On
the next press we restore the cached value. If the operator power-cycles
between toggles, the cached value is lost; on first toggle after boot,
"off" caches whatever brightness the kernel started at, and "on" goes
to ``max_brightness``.

This module is host-test friendly: pass an alternate ``base_path`` to
``Backlight`` to point at any directory containing the two files. The
unit tests use ``tmp_path`` for that.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

_log = logging.getLogger(__name__)

# Default sysfs root. Tests override.
DEFAULT_BACKLIGHT_DIR: Final = Path("/sys/class/backlight/backlight")


class Backlight:
    """Binary on/off toggle for the LCD backlight.

    All filesystem errors are caught and logged; ``toggle()`` is a
    best-effort operation. Keeping the daemon alive when the backlight
    sysfs node disappears (hardware fault, runtime PM quirk) is much
    more important than knowing the toggle worked.
    """

    def __init__(self, base_path: Path | str = DEFAULT_BACKLIGHT_DIR) -> None:
        self._base = Path(base_path)
        self._brightness_path = self._base / "brightness"
        self._max_path = self._base / "max_brightness"
        # Cached "on" brightness — populated when we transition to off.
        # Starts as None so the first "on" press goes to max_brightness.
        self._on_brightness: int | None = None
        # Read max once at construction; if it fails, fall back to a
        # conservative value. Most kernel backlight drivers expose this
        # statically, so reading it more than once is wasted I/O.
        self._max = self._read_int(self._max_path, default=255)

    # ── Public API ────────────────────────────────────────────────────

    def is_on(self) -> bool:
        """True if current brightness is non-zero. False on read error."""
        return self._read_int(self._brightness_path, default=0) > 0

    def toggle(self) -> None:
        """Flip backlight state. Logs and swallows any I/O error."""
        current = self._read_int(self._brightness_path, default=-1)
        if current < 0:
            _log.warning("backlight toggle: cannot read %s", self._brightness_path)
            return

        if current > 0:
            # Going off — remember where we were so we can restore.
            self._on_brightness = current
            self._write_int(self._brightness_path, 0)
            _log.info("backlight off (was %d/%d)", current, self._max)
        else:
            # Going on — restore previous, or full if we never saw one.
            target = self._on_brightness if self._on_brightness is not None else self._max
            self._write_int(self._brightness_path, target)
            _log.info("backlight on (%d/%d)", target, self._max)

    # ── Internals ─────────────────────────────────────────────────────

    @staticmethod
    def _read_int(path: Path, *, default: int) -> int:
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            _log.debug("backlight: cannot read %s, using default=%d", path, default)
            return default

    @staticmethod
    def _write_int(path: Path, value: int) -> None:
        try:
            path.write_text(str(value))
        except OSError:
            _log.exception("backlight: write to %s failed", path)
