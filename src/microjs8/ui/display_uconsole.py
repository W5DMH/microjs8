"""uConsole framebuffer display backend (v0.0.17).

Implements ``UConsoleFramebufferDevice`` -- a sibling to
``DisplayDevice`` (CardputerZero fbdev) and ``SpiDisplayDevice``
(Waveshare SPI). Targets the ClockworkPi uConsole CM4's 5-inch IPS
DSI panel via the kernel's vc4drmfb framebuffer compatibility shim.

Why a parallel class instead of extending ``DisplayDevice``:
  - The Waveshare/CardputerZero rigs are field-validated and we don't
    want to risk regressions there by generalizing the existing class.
  - The uConsole path needs operations the other backends never do:
    rotate 90 degrees, scale 4x nearest, center on a larger framebuffer.
    Keeping these in a dedicated class makes intent clear and tests
    independent.
  - ``open_display()`` is the dispatch point; from the caller's view
    (RenderThread) all three classes expose the same ``show(image) ->
    None`` and ``close() -> None`` interface.

Display geometry (from PI-2W-TEST sister bring-up of a real uConsole):

    Panel: 720 x 1280 (PORTRAIT orientation, vc4drmfb, 16 bpp RGB565)
    Stride: 1440 bytes/row (= 720 * 2, no padding)
    Source: 320 x 170 (theme.SCREEN_W x theme.SCREEN_H, landscape)

The math works out perfectly at 4x scale with a 90-degree rotation:

    Source  PIL Image:        320 x 170   (landscape)
    Rotated 90 degrees CCW:   170 x 320   (portrait)
    Scaled 4x nearest:        680 x 1280  (portrait, fits panel)
    Centered on 720 x 1280:   X offset 20px, Y offset 0px

So the operator sees a 4x-pixel-doubled MicroJS8 UI in landscape
orientation, with 20px black bars on the left and right edges.
No vertical letterboxing.

Pixel format: same RGB565 as the existing backends -- the existing
``_rgb888_to_rgb565`` helper in display.py is reused as-is.

Rotation direction is exposed as a module-level constant so a flipped
panel (different mount orientation in some future uConsole variant
or hardware revision) can be corrected with a single-line edit
without rebuilding the .deb.
"""

from __future__ import annotations

import logging
import mmap
import os
from pathlib import Path
from typing import Optional

from PIL import Image

from microjs8.ui import theme
from microjs8.ui.display import (
    DEFAULT_PROC_FB,
    DEFAULT_SYSFS_ROOT,
    FbInfo,
    _Sliceable,
    _rgb888_to_rgb565,
    find_fbdev_index,
    read_fb_info,
)

_log = logging.getLogger(__name__)


# -- uConsole framebuffer signature -----------------------------------
# Discovery is by framebuffer name + dimensions + bpp, not by
# device-tree model string. The uConsole CM4's /proc/device-tree/model
# reports "Raspberry Pi Compute Module 4 Rev 1.1" -- indistinguishable
# from a bare CM4. The (name, geometry, bpp) tuple is distinctive
# enough: any Pi running KMS-DRM driving a 720x1280 16-bpp DSI panel
# is the uConsole CM4 in practice. Future variants with the same
# signature would benefit from the same rendering pipeline.

UCONSOLE_FB_NAME = "vc4drmfb"
UCONSOLE_PANEL_WIDTH = 720
UCONSOLE_PANEL_HEIGHT = 1280
UCONSOLE_BPP = 16


# -- Render geometry --------------------------------------------------
# Derived from the source dimensions in theme.py. If the operator
# changes SCREEN_W or SCREEN_H, these recompute on import -- the scale
# factor is fixed at 4 because that's what produces the clean 1280px
# fit on the long axis.

_SOURCE_W = theme.SCREEN_W       # 320
_SOURCE_H = theme.SCREEN_H       # 170

SCALE_FACTOR = 4

# After 90-degree rotation, source becomes (H x W). After 4x scale:
# rotated source becomes (H*4 x W*4) = (680 x 1280) for the
# default 320x170 source.
_SCALED_W = _SOURCE_H * SCALE_FACTOR    # 680
_SCALED_H = _SOURCE_W * SCALE_FACTOR    # 1280

# Centering offset on the 720x1280 panel
_OFFSET_X = (UCONSOLE_PANEL_WIDTH - _SCALED_W) // 2      # 20
_OFFSET_Y = (UCONSOLE_PANEL_HEIGHT - _SCALED_H) // 2     # 0


# -- Rotation direction -----------------------------------------------
# PIL rotates CCW for positive angle. The uConsole's panel is mounted
# physically in portrait orientation (long axis vertical in the
# framebuffer); the operator holds the device with the keyboard at
# the bottom, viewing it in landscape (long axis horizontal).
#
# Empirically determined on PI-2W-TEST sister bring-up: rotate +90
# (CCW) puts the operator's "top" of the MicroJS8 UI at the panel's
# left edge, which appears as the top of the user-visible landscape
# view when held normally.
#
# If a future uConsole variant has the panel mounted with reversed
# orientation, change ROTATION_DEGREES to -90 (CW). The rest of the
# math is symmetric.

ROTATION_DEGREES = 90


# -- The device class -------------------------------------------------


class UConsoleFramebufferDevice:
    """Owns the mmap'd uConsole framebuffer; rotates+scales source frames.

    Construction parallels ``DisplayDevice``:

      - Production: ``UConsoleFramebufferDevice.open()`` discovers the
        framebuffer by name, validates the geometry/bpp signature,
        opens the device, mmaps it, blanks it once, returns the device.

      - Tests: the constructor accepts any ``_Sliceable`` (bytearray)
        plus an ``FbInfo``. No file descriptor is opened, so the test
        does not need a real fbdev node.

    Interface (matches DisplayDevice for backend-agnostic callers):
      - ``show(image)`` -- accepts a 320x170 PIL Image; rotates,
        scales 4x, centers on the panel, writes RGB565 via mmap.
      - ``close()`` -- releases mmap and fd; idempotent.
      - ``info`` property -- read-only diagnostic accessor.
    """

    def __init__(
        self,
        target: _Sliceable,
        info: FbInfo,
        *,
        fd: Optional[int] = None,
        mmap_obj: Optional[mmap.mmap] = None,
    ) -> None:
        # Validate the framebuffer signature. We require exact geometry
        # match -- a 720x720 or 1280x720 or 16:9 desktop panel wouldn't
        # produce a usable UI with our rotate+scale pipeline.
        if info.width != UCONSOLE_PANEL_WIDTH:
            raise ValueError(
                f"UConsoleFramebufferDevice expects panel width "
                f"{UCONSOLE_PANEL_WIDTH}; got {info.width}"
            )
        if info.height != UCONSOLE_PANEL_HEIGHT:
            raise ValueError(
                f"UConsoleFramebufferDevice expects panel height "
                f"{UCONSOLE_PANEL_HEIGHT}; got {info.height}"
            )
        if info.bpp != UCONSOLE_BPP:
            raise ValueError(
                f"UConsoleFramebufferDevice supports bpp={UCONSOLE_BPP} "
                f"(RGB565); got bpp={info.bpp}"
            )

        self._target = target
        self._info = info
        self._fd = fd
        self._mmap = mmap_obj

    # -- Production constructor --------------------------------------

    @classmethod
    def open(
        cls,
        *,
        proc_fb: Path = DEFAULT_PROC_FB,
        sysfs_root: Path = DEFAULT_SYSFS_ROOT,
    ) -> "UConsoleFramebufferDevice":
        """Discover the uConsole framebuffer and return a ready device.

        Raises ``RuntimeError`` if the kernel doesn't expose vc4drmfb,
        if the geometry doesn't match the uConsole signature, or if
        sysfs is malformed. Raises ``OSError`` from the underlying
        ``open(2)`` / ``mmap(2)`` calls.

        The dispatch in ``open_display()`` only reaches this method
        after detecting the uConsole signature, so the most common
        cause of a raise here is a partial bring-up (KMS driver loaded
        but not yet probed) -- the caller logs and falls through to
        the next backend in the discovery chain.
        """
        idx = find_fbdev_index(UCONSOLE_FB_NAME, proc_fb=proc_fb)
        if idx is None:
            raise RuntimeError(
                f"no framebuffer named {UCONSOLE_FB_NAME!r} found in {proc_fb}"
            )

        info = read_fb_info(idx, UCONSOLE_FB_NAME, sysfs_root=sysfs_root)
        path = f"/dev/fb{idx}"

        fd = os.open(path, os.O_RDWR)
        try:
            m = mmap.mmap(
                fd, info.frame_bytes, mmap.MAP_SHARED, mmap.PROT_WRITE,
            )
        except Exception:
            os.close(fd)
            raise

        # Blank the entire framebuffer once. Subsequent show() calls
        # only touch the centered 680x1280 region; the 20px margins
        # on each side stay black because we never overwrite them
        # after this initial blank.
        blank = bytes(info.frame_bytes)
        m[:] = blank

        _log.info(
            "uConsole display initialised (fb%d %s, %dx%d @ %dbpp, "
            "stride=%d; source %dx%d rotated %ddeg + scaled %dx, "
            "centered offset=(%d,%d))",
            info.index, info.name, info.width, info.height,
            info.bpp, info.line_length,
            _SOURCE_W, _SOURCE_H, ROTATION_DEGREES, SCALE_FACTOR,
            _OFFSET_X, _OFFSET_Y,
        )
        return cls(m, info, fd=fd, mmap_obj=m)

    # -- Frame push --------------------------------------------------

    def show(self, image: Image.Image) -> None:
        """Push a frame.

        Accepts a PIL Image with the screens.py source dimensions
        (theme.SCREEN_W x theme.SCREEN_H, currently 320x170). Rotates
        90 degrees, scales 4x with nearest-neighbor (crisp pixels,
        no smoothing), centers on the panel, and writes RGB565 bytes
        to the mmap'd framebuffer row by row.

        We use stride-aware row-by-row writes because the destination
        rows (1440 bytes for a 720-pixel panel) are wider than the
        source rows after scaling (1360 bytes for the 680-pixel
        scaled image). The 80-byte (40 + 40) margin on each row
        remains zeroed from the initial blank.

        Raises ``ValueError`` if the input dimensions don't match
        the source size -- screens.py renderers must produce exactly
        SCREEN_W x SCREEN_H, and a mismatch indicates a programming
        error worth surfacing rather than silently distorting.
        """
        if image.size != (_SOURCE_W, _SOURCE_H):
            raise ValueError(
                f"image size {image.size} does not match source "
                f"{_SOURCE_W}x{_SOURCE_H}"
            )

        # Rotate. expand=True grows the canvas to hold the rotated
        # image; without it, PIL crops to the original bounds.
        rotated = image.rotate(ROTATION_DEGREES, expand=True)

        # Scale 4x nearest. PIL's NEAREST resampling preserves crisp
        # pixel edges -- with BILINEAR or BICUBIC, fonts would smear
        # and the pixel-art aesthetic would be lost.
        scaled = rotated.resize(
            (_SCALED_W, _SCALED_H),
            Image.NEAREST,
        )

        # Convert to RGB565 bytes (same helper as the other backends).
        rgb565 = _rgb888_to_rgb565(scaled)

        # Write to framebuffer, centered. Each scaled-image row is
        # _SCALED_W * 2 bytes (680 * 2 = 1360); each panel row is
        # info.line_length bytes (1440). We offset by
        # _OFFSET_X * 2 = 40 bytes into each panel row.
        src_row_bytes = _SCALED_W * 2
        dst_stride = self._info.line_length
        x_offset_bytes = _OFFSET_X * 2
        y_offset_rows = _OFFSET_Y

        for row in range(_SCALED_H):
            src_off = row * src_row_bytes
            dst_off = (row + y_offset_rows) * dst_stride + x_offset_bytes
            self._target[dst_off : dst_off + src_row_bytes] = rgb565[
                src_off : src_off + src_row_bytes
            ]

    # -- Lifecycle ---------------------------------------------------

    def close(self) -> None:
        """Best-effort teardown -- flush and release mmap + fd.

        Safe to call multiple times. Errors are logged at WARNING and
        suppressed so a teardown failure can't take down the daemon's
        graceful-shutdown path.
        """
        if self._mmap is not None:
            try:
                self._mmap.flush()
            except (BufferError, OSError, ValueError) as exc:
                _log.warning("uConsole framebuffer flush failed: %s", exc)
            try:
                self._mmap.close()
            except (BufferError, OSError, ValueError) as exc:
                _log.warning("uConsole framebuffer mmap close failed: %s", exc)
            self._mmap = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError as exc:
                _log.warning("uConsole framebuffer fd close failed: %s", exc)
            self._fd = None

    # -- Read-only diagnostic accessors ------------------------------

    @property
    def info(self) -> FbInfo:
        return self._info


# -- uConsole detection helper (shared with open_display dispatch) ----


def is_uconsole_present(
    *,
    proc_fb: Path = DEFAULT_PROC_FB,
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
) -> bool:
    """Return True if the kernel exposes a uConsole-signature framebuffer.

    Used by ``open_display()`` to dispatch to UConsoleFramebufferDevice
    only when the hardware signature matches. The check is fast (~3
    sysfs reads) and non-destructive (no fd opened, no mmap).

    Detection criteria (all must hold):
      - A framebuffer named 'vc4drmfb' is present in /proc/fb
      - Its virtual_size in sysfs is exactly UCONSOLE_PANEL_WIDTH x
        UCONSOLE_PANEL_HEIGHT (720 x 1280)
      - Its bits_per_pixel is exactly UCONSOLE_BPP (16)

    The same signature check is duplicated as shell in postinst.sh
    so install-time decisions (graphical target switch) can be made
    without invoking Python. Keep the two in sync if you ever extend
    the criteria.
    """
    idx = find_fbdev_index(UCONSOLE_FB_NAME, proc_fb=proc_fb)
    if idx is None:
        return False
    try:
        info = read_fb_info(idx, UCONSOLE_FB_NAME, sysfs_root=sysfs_root)
    except RuntimeError:
        return False
    return (
        info.width == UCONSOLE_PANEL_WIDTH
        and info.height == UCONSOLE_PANEL_HEIGHT
        and info.bpp == UCONSOLE_BPP
    )
