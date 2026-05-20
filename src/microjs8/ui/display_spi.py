"""Phase 18 — userspace SPI driver for ST7789V/V2 displays.

This module gives microjs8 a self-contained pixel pipeline that pushes
frames to a Waveshare 1.9" 170×320 ST7789V2 panel (or compatible)
over SPI without relying on the kernel's fbtft framebuffer driver.

Why we need this
================
The fbtft path that ``ui/display.DisplayDevice`` uses works on the
CardputerZero (whose M5Stack-built kernel ships the panel driver
pre-bound to ``/dev/fb1``) but is fragile on stock Raspberry Pi OS
Bookworm with a bare Pi Zero 2 W:

  - ``fbtft`` lives in the kernel staging directory and is no longer
    actively maintained.
  - The Bookworm 6.12.x kernel logs
    "SPI driver fb_st7789v has no spi_device_id for sitronix,st7789v"
    when the standard ``dtoverlay=fbtft,…,name=fb_st7789v,…`` line is
    used — the driver loads but never binds to the device tree node,
    so no /dev/fb1 ever appears.
  - The official Raspberry Pi firmware does not ship a standalone
    ``fb_st7789v.dtbo`` overlay file.
  - Modern Pi display projects (Pimoroni's pirate-audio, Adafruit's
    CircuitPython displays, luma.lcd, etc.) all bypass fbtft and
    push pixels via ``spidev`` from userspace for exactly this
    reason.

Architecture mirrors DisplayDevice
==================================
The class deliberately exposes the same ``show(image)`` / ``close()``
surface as ``ui.display.DisplayDevice`` so that ``ui.display.open_display()``
can return either backend interchangeably. The RenderThread doesn't
need to know which one it's driving — same PIL image in, frame
rendered on the panel either way.

Test surface
============
The module's runtime deps (``spidev``, ``gpiozero``) are only
imported inside the ``open()`` classmethod, never at module load.
The unit tests inject fake SPI and GPIO objects via the constructor,
so the whole module is exercisable on a non-Pi host (CI, dev laptop)
without touching any real hardware.

Wiring assumptions (Waveshare 1.9" → Pi Zero 2 W, the documented
microjs8-enable-display defaults)
==================================================================
+---------+------------+--------------+
| Display | Pi pin     | BCM GPIO     |
+---------+------------+--------------+
| VCC     | pin 1 / 17 | 3V3          |
| GND     | pin 6      | GND          |
| DIN     | pin 19     | GPIO 10 MOSI |
| CLK     | pin 23     | GPIO 11 SCLK |
| CS      | pin 24     | GPIO 8  CE0  |
| DC      | pin 22     | GPIO 25      |
| RST     | pin 13     | GPIO 27      |
| BL      | pin 12     | GPIO 18      |
+---------+------------+--------------+

CS, MOSI, SCLK are handled entirely by the spidev kernel driver
(/dev/spidev0.0). We only manage DC, RST, BL ourselves.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from PIL import Image

_log = logging.getLogger(__name__)


# ── ST7789V command constants ────────────────────────────────────────
#
# These are the standard ST7789 command codes from the datasheet,
# Chapter 10 ("Command"). We name them with the conventional
# CMD_ prefix so call sites read like
#     self._cmd(CMD_CASET, payload)
# instead of magic hex.

CMD_SWRESET = 0x01  # Software Reset
CMD_SLPOUT  = 0x11  # Sleep Out
CMD_NORON   = 0x13  # Normal Display Mode On
CMD_INVON   = 0x21  # Display Inversion On (ST7789 panels need this)
CMD_DISPOFF = 0x28  # Display Off
CMD_DISPON  = 0x29  # Display On
CMD_CASET   = 0x2A  # Column Address Set
CMD_RASET   = 0x2B  # Row Address Set
CMD_RAMWR   = 0x2C  # Memory Write (start pixel transfer)
CMD_MADCTL  = 0x36  # Memory Access Control (orientation, RGB/BGR)
CMD_COLMOD  = 0x3A  # Interface Pixel Format

# COLMOD payload for 16-bit RGB565.
# Layout from datasheet 10.1.30 (the 0x55 nibbles encode both DPI
# and DBI as 16-bit, which is the standard config).
COLMOD_RGB565 = 0x55

# MADCTL payloads for our supported orientations.
#   Bit 7 (MY) — row address order
#   Bit 6 (MX) — column address order
#   Bit 5 (MV) — row/col exchange (swap → landscape)
#   Bit 4 (ML) — vertical refresh order
#   Bit 3 (BGR) — color order (0 = RGB, 1 = BGR)
# For the Waveshare 1.9" we want landscape with the right "up" side.
# Setting MV=1, MX=1 (0x60) gives 320 wide × 170 tall — what the
# renderer expects.
MADCTL_LANDSCAPE = 0x60

# 1.9" 170×320 panel-specific offsets — the ST7789V controller has a
# 240×320 RAM but this panel only exposes the central 170 columns,
# so in landscape we need to start writing at row 35 of controller
# memory. Confirmed against russhughes/st7789_mpy's rotation tables
# (the canonical reference for these offsets in MicroPython).
PANEL_1_9_LANDSCAPE_WIDTH  = 320
PANEL_1_9_LANDSCAPE_HEIGHT = 170
PANEL_1_9_LANDSCAPE_X_OFFSET = 0
PANEL_1_9_LANDSCAPE_Y_OFFSET = 35


# ── SPI / GPIO protocols ─────────────────────────────────────────────
#
# Defining structural Protocols here lets the test suite inject fake
# objects without any spidev / gpiozero on the host. The protocols
# capture only the methods we actually call.


@runtime_checkable
class _SpiPort(Protocol):
    """Minimal slice of the spidev.SpiDev interface we use."""

    max_speed_hz: int
    mode: int

    def writebytes2(self, data: bytes | bytearray | memoryview) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class _DigitalOut(Protocol):
    """Minimal slice of gpiozero.DigitalOutputDevice we use."""

    def on(self) -> None: ...
    def off(self) -> None: ...
    def close(self) -> None: ...


# ── RGB565 big-endian conversion ────────────────────────────────────


def _rgb888_to_rgb565_be(image: Image.Image) -> bytes:
    """Convert a PIL RGB image to RGB565 bytes in BIG-endian order.

    The ST7789 expects pixel data MSB-first over SPI. We force
    big-endian at conversion time so the writebytes path doesn't have
    to think about byte order.

    Bit layout for one 16-bit pixel (MSB first):
        byte 0:  R R R R R | G G G
        byte 1:  G G G | B B B B B

    numpy's ``astype('>u2')`` does the cast + endianness flip in one
    step; ``tobytes()`` then yields BE bytes regardless of host
    architecture.
    """
    import numpy as np

    if image.mode != "RGB":
        image = image.convert("RGB")

    arr = np.asarray(image, dtype=np.uint8)
    r = arr[..., 0].astype(np.uint16) >> 3
    g = arr[..., 1].astype(np.uint16) >> 2
    b = arr[..., 2].astype(np.uint16) >> 3
    rgb565 = (r << 11) | (g << 5) | b
    # Force big-endian on output. astype('>u2') is endian-safe on
    # any host: it produces the same byte sequence regardless of
    # whether the CPU is little-endian (aarch64, x86_64) or
    # big-endian (rare, but covered for free).
    return rgb565.astype(">u2").tobytes()


# ── SpiDisplayDevice ────────────────────────────────────────────────


class SpiDisplayDevice:
    """Userspace SPI driver for the ST7789V/V2 panel.

    Mirrors ``DisplayDevice`` — same ``show(image)`` and ``close()``
    surface. Construction is two-flavor:

      - Production: ``SpiDisplayDevice.open()`` opens
        ``/dev/spidev0.0``, instantiates ``gpiozero.DigitalOutputDevice``
        for DC/RST/BL, runs the panel init sequence, and returns a
        ready-to-use device.

      - Tests: ``SpiDisplayDevice(spi=fake_spi, dc=fake_dc, …)`` —
        no real hardware accessed. The ``init`` flag can be False
        if the test wants to verify init-sequence output separately
        from frame writes.
    """

    # Conservative chunk size when sending pixel data. Linux spidev's
    # default buffer is 4096 bytes; the Pi raises this to 65536 via
    # /sys/module/spidev/parameters/bufsiz, but writing larger chunks
    # crashes older spidev userspace. 4096 is safe everywhere and
    # gives us ~26 ``writebytes2`` calls per 320×170 frame — overhead
    # is negligible because each call is a single kernel transition.
    _SPI_CHUNK = 4096

    def __init__(
        self,
        *,
        spi: _SpiPort,
        dc: _DigitalOut,
        rst: _DigitalOut,
        bl: _DigitalOut,
        width: int = PANEL_1_9_LANDSCAPE_WIDTH,
        height: int = PANEL_1_9_LANDSCAPE_HEIGHT,
        x_offset: int = PANEL_1_9_LANDSCAPE_X_OFFSET,
        y_offset: int = PANEL_1_9_LANDSCAPE_Y_OFFSET,
        madctl: int = MADCTL_LANDSCAPE,
        run_init: bool = True,
        sleep: Optional[object] = None,  # injectable for fast tests
    ) -> None:
        """Construct an SpiDisplayDevice over injected SPI + GPIO.

        Parameters that aren't typically overridden (width/height/
        offsets/madctl) default to Waveshare 1.9" 170×320 landscape.
        The factory ``SpiDisplayDevice.open()`` accepts these as
        kwargs for future panel variants.

        ``run_init`` controls whether the panel init sequence runs
        in the constructor. Production always wants True. Tests
        that want to verify *only* the show() side may pass False,
        construct, then call ``run_init_sequence()`` separately and
        inspect the recorded transfers.

        ``sleep`` is the function called for inter-command delays.
        Defaults to ``time.sleep``; tests override with a recording
        no-op to keep the suite fast (the real init sequence has
        ~650 ms of cumulative sleeps that we don't want in CI).
        """
        self._spi = spi
        self._dc = dc
        self._rst = rst
        self._bl = bl
        self._width = int(width)
        self._height = int(height)
        self._x_offset = int(x_offset)
        self._y_offset = int(y_offset)
        self._madctl = int(madctl)
        self._sleep = sleep if sleep is not None else time.sleep
        self._closed = False

        if self._width <= 0 or self._height <= 0:
            raise ValueError(
                f"SpiDisplayDevice dimensions must be positive: "
                f"got width={self._width}, height={self._height}"
            )

        if run_init:
            self.run_init_sequence()

    # ── Public surface (mirrors DisplayDevice) ──────────────────────

    @property
    def width(self) -> int:
        """Visible width in pixels (the size show() expects)."""
        return self._width

    @property
    def height(self) -> int:
        """Visible height in pixels (the size show() expects)."""
        return self._height

    def show(self, image: Image.Image) -> None:
        """Convert a PIL RGB image to RGB565 and push to the panel.

        The image's dimensions must match ``self.width`` × ``self.height``
        exactly — the renderer is supposed to produce frames at the
        panel's resolution, and silently resizing here would mask a
        bug in the layout pipeline.
        """
        if self._closed:
            raise RuntimeError("SpiDisplayDevice.show() called after close()")

        if image.size != (self._width, self._height):
            raise ValueError(
                f"SpiDisplayDevice.show: image size {image.size} doesn't match "
                f"panel size ({self._width}, {self._height})"
            )

        # 1. Set the column address window (CASET).
        #    Format: high(start), low(start), high(end), low(end)
        x_start = self._x_offset
        x_end = self._x_offset + self._width - 1
        self._cmd(CMD_CASET, bytes([
            (x_start >> 8) & 0xFF, x_start & 0xFF,
            (x_end   >> 8) & 0xFF, x_end   & 0xFF,
        ]))

        # 2. Set the row address window (RASET).
        y_start = self._y_offset
        y_end = self._y_offset + self._height - 1
        self._cmd(CMD_RASET, bytes([
            (y_start >> 8) & 0xFF, y_start & 0xFF,
            (y_end   >> 8) & 0xFF, y_end   & 0xFF,
        ]))

        # 3. Begin memory write — after this the panel expects raw
        #    pixel bytes until the next command.
        self._cmd(CMD_RAMWR)

        # 4. Stream pixel data, DC pin high (data mode), chunked.
        pixels = _rgb888_to_rgb565_be(image)
        self._dc.on()
        for i in range(0, len(pixels), self._SPI_CHUNK):
            self._spi.writebytes2(pixels[i:i + self._SPI_CHUNK])

    def close(self) -> None:
        """Blank the panel, turn backlight off, release resources.

        Idempotent — second close() is a no-op. We swallow exceptions
        from individual device closes so we don't leak SPI fds because
        one GPIO close raised.
        """
        if self._closed:
            return
        self._closed = True

        # Try to leave the panel in a clean state: backlight off,
        # display off. We don't error if these fail (e.g. SPI bus
        # already lost during a daemon crash) — close() must succeed
        # so that the rest of cleanup runs.
        for fn in (
            lambda: self._cmd(CMD_DISPOFF),
            lambda: self._bl.off(),
        ):
            try:
                fn()
            except Exception:
                _log.debug("SpiDisplayDevice cleanup step raised", exc_info=True)

        # Release each underlying resource. Iterate so one failure
        # doesn't skip the rest.
        for resource in (self._spi, self._dc, self._rst, self._bl):
            try:
                resource.close()
            except Exception:
                _log.debug("SpiDisplayDevice resource close raised", exc_info=True)

    # Backlight on/off — wired to Fn+B / Ctrl+B via the Backlight
    # facade in ui/backlight.py. Phase 18 ships on/off only;
    # Phase 19 will add PWM dimming if operators want it.

    def backlight_on(self) -> None:
        """Turn the backlight on (full brightness)."""
        self._bl.on()

    def backlight_off(self) -> None:
        """Turn the backlight off (dark panel — visible image gone)."""
        self._bl.off()

    # ── Internal: panel init sequence ───────────────────────────────

    def run_init_sequence(self) -> None:
        """Hardware-reset the panel and program it for our orientation.

        Sequence comes from the ST7789V datasheet (Chapter 10) cross-
        checked against three reference implementations:
          - Pimoroni's st7789-python (PyPI)
          - Adafruit's CircuitPython ST7789 driver
          - russhughes/st7789_mpy

        All three agree on the order; the delays come from the
        datasheet "Reset and Initialization" notes.
        """
        # Hardware reset: pulse RST low for ≥10 µs then wait ≥120 ms
        # before sending the first command. We use 10 ms / 150 ms for
        # margin — the panel's own boot-up takes ~120 ms.
        self._rst.on()
        self._sleep(0.010)
        self._rst.off()
        self._sleep(0.010)
        self._rst.on()
        self._sleep(0.150)

        # Software reset belt-and-suspenders. Some ST7789V batches
        # ship with garbage in their registers from the factory.
        self._cmd(CMD_SWRESET)
        self._sleep(0.150)

        # Out of sleep, into normal mode.
        self._cmd(CMD_SLPOUT)
        self._sleep(0.500)

        # 16-bit RGB565 pixel format.
        self._cmd(CMD_COLMOD, bytes([COLMOD_RGB565]))
        self._sleep(0.010)

        # Memory orientation: landscape, RGB order.
        self._cmd(CMD_MADCTL, bytes([self._madctl]))

        # ST7789 panels need inversion ON to render correct colors
        # (it's documented in the datasheet as "for some IPS
        # variants" — and the 1.9" Waveshare is one of them).
        self._cmd(CMD_INVON)
        self._sleep(0.010)

        # Normal display mode (vs partial or scroll modes).
        self._cmd(CMD_NORON)
        self._sleep(0.010)

        # Turn the panel on. After this point, sending RAMWR followed
        # by pixel bytes draws to the visible area.
        self._cmd(CMD_DISPON)
        self._sleep(0.500)

        # Backlight on by default — operators expect to see something
        # immediately when the daemon starts.
        self._bl.on()

    # ── Internal: command helper ────────────────────────────────────

    def _cmd(self, command: int, data: bytes | bytearray = b"") -> None:
        """Send a one-byte command, optionally followed by data bytes.

        DC pin controls whether the next byte is interpreted as a
        command (low) or data (high). We toggle DC, write the
        command byte, then if there's a payload toggle DC again and
        write the payload.

        Errors from spi.writebytes2 propagate — the caller (show or
        run_init_sequence) decides whether to surface them or
        swallow. close() swallows; show() lets them propagate so the
        render thread logs them.
        """
        self._dc.off()
        self._spi.writebytes2(bytes([command & 0xFF]))
        if data:
            self._dc.on()
            self._spi.writebytes2(bytes(data))

    # ── Production factory ──────────────────────────────────────────

    @classmethod
    def open(
        cls,
        *,
        spi_bus: int = 0,
        spi_device: int = 0,
        spi_max_hz: int = 40_000_000,
        dc_gpio: int = 25,
        rst_gpio: int = 27,
        bl_gpio: int = 18,
        spidev_glob: Optional[Path] = None,
        **kwargs,
    ) -> "SpiDisplayDevice":
        """Open the SPI bus + GPIO pins for a real ST7789V panel.

        Imports spidev + gpiozero lazily so this module is still
        importable on a non-Pi host (CI, dev laptop) — the imports
        will fail with a clear error if you actually call open()
        there, but importing the module to read constants or
        introspect classes works fine.

        ``spidev_glob`` is for the test path of the doctor probe —
        production should leave it None (the real check is inside
        the spidev call).

        Extra kwargs (width, height, x_offset, y_offset, madctl,
        run_init) forward to ``__init__`` so future panel variants
        can be wired up without subclassing.
        """
        # Lazy imports — keeps the module importable on non-Pi.
        try:
            import spidev  # type: ignore[import-not-found]
            from gpiozero import DigitalOutputDevice  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "SpiDisplayDevice.open() requires python3-spidev and "
                "python3-gpiozero. On Bookworm: sudo apt install "
                "python3-spidev python3-gpiozero. "
                f"Original error: {exc}"
            ) from exc

        # Sanity check that the bus actually exists before we try to
        # open it. Gives a much clearer error than spidev's OSError.
        if spidev_glob is None:
            spidev_path = Path(f"/dev/spidev{spi_bus}.{spi_device}")
        else:
            spidev_path = spidev_glob
        if not spidev_path.exists():
            raise RuntimeError(
                f"SPI device {spidev_path} not found. Enable SPI via "
                f"`sudo raspi-config` (Interface Options → SPI) OR add "
                f"`dtparam=spi=on` to /boot/firmware/config.txt and "
                f"reboot."
            )

        spi = spidev.SpiDev()
        spi.open(spi_bus, spi_device)
        spi.max_speed_hz = int(spi_max_hz)
        spi.mode = 0  # CPOL=0, CPHA=0 — the ST7789 default.

        # gpiozero infers the GPIO chip automatically (lgpio on
        # modern Pi OS, RPi.GPIO on older). DigitalOutputDevice
        # gives us .on() / .off() with no PWM overhead — we want
        # plain on/off for now, per the Phase 18 scope decision.
        dc = DigitalOutputDevice(dc_gpio, initial_value=False)
        rst = DigitalOutputDevice(rst_gpio, initial_value=True)
        bl = DigitalOutputDevice(bl_gpio, initial_value=False)

        _log.info(
            "SpiDisplayDevice.open: %s @ %d Hz, DC=GPIO%d RST=GPIO%d BL=GPIO%d",
            spidev_path, spi_max_hz, dc_gpio, rst_gpio, bl_gpio,
        )

        return cls(spi=spi, dc=dc, rst=rst, bl=bl, **kwargs)
