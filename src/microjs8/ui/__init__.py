"""microjs8.ui — ST7789 display + screen state machine (Step 2).

Public surface used by the rest of the daemon:

    UIState           — mutable UI state, mutated from asyncio thread
    UISnapshot        — frozen snapshot, safe to read from any thread
    Screen            — enum of all screens
    DisplayDevice     — owns SPI traffic to the panel; open() at boot
    RenderThread      — render loop; .start() once at boot, .stop() on shutdown
    load_fonts        — pre-load fonts at startup
"""

from microjs8.ui.display import DisplayDevice, FakeDisplayDevice, RenderThread
from microjs8.ui.fonts import Fonts, load_fonts
from microjs8.ui.state import (
    DirectedRow,
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
    "InboxRow",
    "RING",
    "RenderThread",
    "Screen",
    "UISnapshot",
    "UIState",
    "load_fonts",
]
