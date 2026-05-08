"""minijs8.input — USB keyboard and GPIO buttons (Step 3)."""

from minijs8.input.buttons import (
    PIN_BUTTON_BOTTOM,
    PIN_BUTTON_TOP,
    SHUTDOWN_HOLD_S,
    ButtonWatcher,
    fake_shutdown,
    systemctl_poweroff,
)
from minijs8.input.events import Key, KeyEvent
from minijs8.input.keyboard import KeyboardThread, find_keyboard_device
from minijs8.input.router import InputRouter

__all__ = [
    "ButtonWatcher",
    "InputRouter",
    "Key",
    "KeyEvent",
    "KeyboardThread",
    "PIN_BUTTON_BOTTOM",
    "PIN_BUTTON_TOP",
    "SHUTDOWN_HOLD_S",
    "fake_shutdown",
    "find_keyboard_device",
    "systemctl_poweroff",
]
