"""ST7789 display driver wrapper and render thread.

Two collaborating pieces:

  - ``DisplayDevice`` wraps the Adafruit ST7789 driver with the exact
    GPIO map for the Mini PiTFT 1.3" 240x240 (verified against
    Adafruit's pinouts page):

        SPI0: SCLK=GPIO11, MOSI=GPIO10, CE0=GPIO8 (CS)
        DC   = GPIO25
        RST  = None (this board has no GPIO-driven reset; the ST7789
                     is reset by an RC on the carrier on power-up.
                     The Adafruit example showing rst=board.D24 is for
                     the bare 1.3" breakout, NOT the Mini PiTFT —
                     GPIO24 on the Mini PiTFT is BUTTON B.)
        BL   = GPIO22 (active-high; we drive it high during init)

  - ``RenderThread`` owns all SPI traffic. It blocks on the UIState's
    dirty event, takes a snapshot, renders the screen via the pure
    functions in ``screens.py``, and pushes the frame to the panel.
    A full 240x240 frame at 24 MHz SPI takes ~40-60 ms on a Pi Zero 2W
    including the per-row Python overhead — fine for a UI that only
    redraws on state change.

The asyncio loop never touches SPI directly. It only mutates
``UIState`` and lets the render thread pick up the dirty flag.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Protocol

from PIL import Image

from minijs8.ui import screens
from minijs8.ui.fonts import Fonts, load_fonts
from minijs8.ui.state import UIState

_log = logging.getLogger(__name__)


# Rate-limit redraws to ~30 FPS max. Even though we redraw on dirty
# events, a stream of mutations during the shutdown countdown could
# otherwise hammer SPI. 33 ms between frames is generous.
_MIN_FRAME_INTERVAL_S = 1.0 / 30.0

# Adafruit driver default is 24 MHz; the Pi Zero 2W is comfortable
# at 40 MHz with this panel. We keep the conservative 24 MHz default
# until on-target measurements justify pushing it.
_SPI_BAUDRATE = 24_000_000


class _DisplayDriver(Protocol):
    """Subset of adafruit_rgb_display.st7789.ST7789 we depend on."""

    width: int
    height: int

    def image(self, image: Image.Image) -> None: ...


class DisplayDevice:
    """Owns the panel + backlight. SPI traffic only happens via this object.

    Constructed only on real hardware. Tests use ``FakeDisplayDevice``
    (defined below) instead so they don't need ``board``, ``digitalio``,
    or ``adafruit_rgb_display`` installed.
    """

    def __init__(
        self,
        driver: _DisplayDriver,
        backlight: Optional["BacklightControl"],
    ) -> None:
        self._driver = driver
        self._backlight = backlight

    @classmethod
    def open(cls) -> "DisplayDevice":
        """Initialise the panel, turn the backlight on, return the device.

        Imports the Adafruit / digitalio / board modules here, lazily,
        so host-side tests can import the rest of the ui package
        without those libraries being installed.
        """
        # Imported inside the method so host tests don't need these.
        import board  # type: ignore[import-not-found]
        import digitalio  # type: ignore[import-not-found]
        from adafruit_rgb_display import st7789  # type: ignore[import-not-found]

        spi = board.SPI()
        cs_pin = digitalio.DigitalInOut(board.CE0)
        dc_pin = digitalio.DigitalInOut(board.D25)

        # The Mini PiTFT 1.3" 240x240 is mounted such that 'native'
        # 0,0 starts at row 80; we apply a 180° rotation so logical
        # 0,0 is at the top-left as the user looks at the panel with
        # the buttons on the LEFT (button A=GPIO23 on top, B=GPIO24
        # below). In our naming, GPIO23 is BUTTON_TOP and GPIO24 is
        # BUTTON_BOTTOM, matching the silkscreen orientation. Tweak
        # rotation here if the operator mounts the device differently.
        driver = st7789.ST7789(
            spi,
            cs=cs_pin,
            dc=dc_pin,
            rst=None,
            baudrate=_SPI_BAUDRATE,
            width=240,
            height=240,
            x_offset=0,
            y_offset=80,
            rotation=180,
        )

        backlight = BacklightControl.open()
        backlight.on()

        # Paint a clean black frame so we don't show whatever was in
        # GDDRAM at boot.
        blank = Image.new("RGB", (240, 240), (0, 0, 0))
        driver.image(blank)

        _log.info("display initialised (ST7789 240x240, baud=%d)", _SPI_BAUDRATE)
        return cls(driver, backlight)

    def show(self, image: Image.Image) -> None:
        """Push a frame. Must be 240x240 RGB; we do not resize."""
        self._driver.image(image)

    def close(self) -> None:
        """Best-effort teardown."""
        if self._backlight is not None:
            try:
                self._backlight.off()
            except Exception:
                pass


class BacklightControl:
    """GPIO22 backlight enable. Active-high.

    On the Mini PiTFT 1.3", GPIO22 controls the backlight FET.
    Driving high turns the backlight on; low turns it off. There's a
    solder jumper on the back of the board to hard-tie it on if the
    user prefers — we just always assume software control.
    """

    BACKLIGHT_PIN = 22

    def __init__(self, led: object) -> None:  # gpiozero.LED runtime type
        self._led = led

    @classmethod
    def open(cls) -> "BacklightControl":
        # Lazy import so host-side tests don't pull gpiozero/lgpio.
        from gpiozero import LED  # type: ignore[import-not-found]

        led = LED(cls.BACKLIGHT_PIN)
        return cls(led)

    def on(self) -> None:
        self._led.on()  # type: ignore[attr-defined]

    def off(self) -> None:
        self._led.off()  # type: ignore[attr-defined]


class RenderThread(threading.Thread):
    """Render loop. Owns SPI traffic for the lifetime of the program.

    Algorithm:
      1. Wait on UIState.dirty (with a small timeout so we can also
         honour stop-events promptly).
      2. consume_dirty() to atomically clear the flag.
      3. snapshot() the state, render a PIL.Image, push to the panel.
      4. Sleep for the remainder of the min-frame-interval if we
         rendered very quickly (rate-limit).

    On any unexpected exception, log and continue — a single bad frame
    must not take the daemon offline.
    """

    def __init__(
        self,
        device: DisplayDevice,
        ui_state: UIState,
        fonts: Optional[Fonts] = None,
        *,
        name: str = "ui-render",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._device = device
        self._ui = ui_state
        self._fonts = fonts if fonts is not None else load_fonts()
        # Note: this attribute is named `_stop_event`, NOT `_stop`.
        # threading.Thread has an internal `_stop()` method that it
        # calls during `join()` — naming our flag `_stop` shadows it
        # and causes "TypeError: 'Event' object is not callable" the
        # first time someone joins this thread.
        self._stop_event = threading.Event()
        self._last_render_t: float = 0.0

    def stop(self) -> None:
        """Request a clean shutdown. Idempotent."""
        self._stop_event.set()
        # Wake the wait() so the thread observes the flag promptly.
        self._ui.dirty.set()

    def run(self) -> None:
        _log.info("render thread starting")
        try:
            while not self._stop_event.is_set():
                # Wait up to 1s for dirty so we still observe stop()
                # if no UI activity happens.
                self._ui.dirty.wait(timeout=1.0)
                if self._stop_event.is_set():
                    break
                if not self._ui.consume_dirty():
                    continue

                # Rate-limit
                now = time.monotonic()
                since = now - self._last_render_t
                if since < _MIN_FRAME_INTERVAL_S:
                    time.sleep(_MIN_FRAME_INTERVAL_S - since)

                state = self._ui.snapshot()
                try:
                    image = screens.render(state, self._fonts)
                    self._device.show(image)
                except Exception:
                    # screens.render() already returns an error frame
                    # for renderer-level exceptions. This catches the
                    # SPI write itself failing — bad cable, panel
                    # disconnected, etc. We log and keep looping.
                    _log.exception("display.show() raised")
                self._last_render_t = time.monotonic()
        finally:
            _log.info("render thread stopping")
            try:
                self._device.close()
            except Exception:
                _log.exception("display.close() raised")


# ── Test double ──────────────────────────────────────────────────────


class FakeDisplayDevice:
    """Test double that records frames instead of pushing them to SPI.

    Used by host-side tests so we never need the Adafruit / gpiozero
    libraries on the dev machine.
    """

    def __init__(self) -> None:
        self.frames: list[Image.Image] = []

    def show(self, image: Image.Image) -> None:
        self.frames.append(image)

    def close(self) -> None:
        pass
