"""microjs8.input — keyboard, system-key gesture handling.

Pre-Phase-3 (MiniJS8) this also exported ``ButtonWatcher`` for the two
TFT GPIO buttons. The CardputerZero has no tactile buttons; that module
was removed in Phase 3 and replaced with ``shutdown_gesture`` for
``Fn+Q`` press-and-hold powerdown. Backlight toggle moved to
``microjs8.power.backlight`` (triggered by ``Fn+B`` from the router).
"""

from microjs8.input.events import Key, KeyEvent
from microjs8.input.i2c_keyboard import I2cKeyboardThread
from microjs8.input.keyboard import KeyboardThread, find_keyboard_device
from microjs8.input.router import InputRouter
from microjs8.input.shutdown_gesture import (
    SHUTDOWN_HOLD_S,
    ShutdownGesture,
    fake_shutdown,
    systemctl_poweroff,
)

__all__ = [
    "I2cKeyboardThread",
    "InputRouter",
    "Key",
    "KeyEvent",
    "KeyboardThread",
    "SHUTDOWN_HOLD_S",
    "ShutdownGesture",
    "fake_shutdown",
    "find_keyboard_device",
    "systemctl_poweroff",
]

# v0.0.16: I2cKeyboardThread is safe to import at module load time
# even on hosts without smbus2 installed -- smbus2 is only imported
# inside I2cKeyboardThread.run() (same lazy pattern UartKeyboardThread
# uses for pyserial).
