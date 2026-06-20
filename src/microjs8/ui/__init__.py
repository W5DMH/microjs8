"""microjs8.ui — framebuffer display + screen state machine.

Public surface used by the rest of the daemon:

    UIState           — mutable UI state, mutated from asyncio thread
    UISnapshot        — frozen snapshot, safe to read from any thread
    Screen            — enum of all screens
    DisplayDevice     — owns the mmap'd /dev/fb<N>; open() at boot
    RenderThread      — render loop; .start() once at boot, .stop() on shutdown
    load_fonts        — pre-load fonts at startup

Phase 5 retargeted the display from MiniJS8's Adafruit-driven SPI
ST7789 to the kernel framebuffer the M5Stack DT overlay exposes
on the CardputerZero. The render-thread architecture is unchanged.
"""

from microjs8.ui.display import (
    DisplayDevice,
    FakeDisplayDevice,
    RenderThread,
    open_display,
)
from microjs8.ui.display_spi import SpiDisplayDevice
from microjs8.ui.display_uconsole import UConsoleFramebufferDevice
from microjs8.ui.fonts import Fonts, load_fonts
from microjs8.ui.state import (
    DirectedRow,
    HB_MODES_ORDERED,
    HbMode,
    InboxRow,
    RING,
    Screen,
    UISnapshot,
    UIState,
)

__all__ = [
    "DirectedRow",
    "DisplayDevice",
    "FakeDisplayDevice",
    "Fonts",
    "HB_MODES_ORDERED",
    "HbMode",
    "InboxRow",
    "RING",
    "RenderThread",
    "Screen",
    "SpiDisplayDevice",
    "UConsoleFramebufferDevice",
    "UISnapshot",
    "UIState",
    "load_fonts",
    "open_display",
]
