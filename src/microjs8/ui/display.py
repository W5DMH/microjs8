"""Linux framebuffer display backend and render-thread driver.

Phase 5 replaces the MiniJS8 SPI/Adafruit driver with the standard
Linux framebuffer interface that the CardputerZero's ST7789v3 panel
exposes. The render-thread architecture is unchanged — the asyncio
loop mutates ``UIState``, this thread polls the dirty flag, snapshots,
renders to a PIL image, and pushes the image to the panel.

The architecture is three layers:

  1. ``find_fbdev_index`` + ``read_fb_info``
     Pure functions that locate the ST7789 framebuffer by name in
     ``/proc/fb`` and read its size/stride/bpp from
     ``/sys/class/graphics/fb<N>/``. Both take an injectable root
     path so tests can point at a tmp directory.

  2. ``DisplayDevice``
     Owns the mmap'd framebuffer. ``show(image)`` converts a PIL RGB
     image to RGB565 bytes (via numpy for speed) and writes it
     stride-aware into the mmap. ``close()`` flushes and releases the
     mmap and the underlying fd.

     Constructed for tests by passing a writable target buffer and an
     ``FbInfo`` directly; constructed in production via the
     ``DisplayDevice.open()`` classmethod, which performs discovery
     and the actual ``mmap`` syscall.

  3. ``RenderThread``
     Unchanged from the prior MiniJS8 implementation. Waits on the
     UIState dirty flag, snapshots, calls ``screens.render``, and
     hands the image to ``DisplayDevice.show``. Rate-limited at
     ~30 fps so a flurry of state changes can't flood the bus.

Why the kernel framebuffer rather than driving the SPI bus
ourselves: the CardputerZero ships with the M5Stack Debian image's
``fb_st7789v`` device-tree overlay loaded, which gives us a fully
functional ``/dev/fb<N>`` node that handles SPI sequencing, panel
init, and (on most kernels) the byte-order swap from CPU LE to
panel BE. Reproducing that in userspace would be hundreds of lines
of fragile DT-coupled code; using the kernel driver is one
``mmap()`` and one slice assignment per frame.

The exact framebuffer index is discovered at runtime: when HDMI is
also enumerated by the kernel (DRM driver bound to ``/dev/dri/card0``
plus a ``/dev/fb0`` shadow), the ST7789v3 may show up as ``/dev/fb1``
or higher. ``find_fbdev_index`` is the single place that resolves
this.
"""

from __future__ import annotations

import logging
import mmap
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Optional, Protocol

from PIL import Image

from microjs8.ui import screens
from microjs8.ui.fonts import Fonts, load_fonts
from microjs8.ui.state import UIState

_log = logging.getLogger(__name__)


# Rate-limit redraws to ~30 FPS max. Even though we redraw on dirty
# events, a stream of mutations during the shutdown countdown could
# otherwise hammer the framebuffer. 33 ms between frames is generous.
_MIN_FRAME_INTERVAL_S: Final = 1.0 / 30.0

# Default kernel name we look for in /proc/fb. The M5Stack DT overlay
# names the ST7789v3 panel "fb_st7789v" — verified against the
# UserDemo's main.cpp, which uses the same string for discovery.
DEFAULT_FB_NAME: Final = "fb_st7789v"

# Standard sysfs root for graphics devices. Linux >= 2.6 has had this
# stable; the per-device subdirectory is "fb<N>".
DEFAULT_SYSFS_ROOT: Final = Path("/sys/class/graphics")

# Standard /proc file listing all registered framebuffer devices.
DEFAULT_PROC_FB: Final = Path("/proc/fb")


# ── Framebuffer discovery + introspection ────────────────────────────


@dataclass(frozen=True)
class FbInfo:
    """Information about a Linux framebuffer device.

    Populated by reading the kernel's sysfs attributes for the device.
    All fields are mandatory — a sane fbdev exposes all of them, and
    we'd rather refuse to start than silently use a default that
    might not match the panel.
    """

    index: int          # the N in /dev/fbN
    width: int          # xres (visible pixels per row)
    height: int         # yres (visible rows)
    bpp: int            # bits_per_pixel — must be 16 for our RGB565 path
    line_length: int    # bytes per row INCLUDING padding ("stride")
    name: str           # kernel name from /proc/fb (e.g. "fb_st7789v")

    @property
    def frame_bytes(self) -> int:
        """Total bytes the kernel mmaps for this device."""
        return self.line_length * self.height

    @property
    def visible_row_bytes(self) -> int:
        """Bytes of actual visible pixel data per row (excludes padding)."""
        return self.width * (self.bpp // 8)

    @property
    def has_padding(self) -> bool:
        """True iff line_length > visible_row_bytes — must blit row-by-row."""
        return self.line_length > self.visible_row_bytes


def find_fbdev_index(
    name: str = DEFAULT_FB_NAME,
    *,
    proc_fb: Path = DEFAULT_PROC_FB,
) -> Optional[int]:
    """Locate the framebuffer index for a device by kernel name.

    Reads ``/proc/fb``. Each line has the format::

        <index> <name>

    where ``<index>`` is a non-negative integer and ``<name>`` is the
    kernel-side framebuffer driver's identifier. We match ``name``
    exactly (case-sensitive, whitespace-trimmed) and return the
    index. Returns ``None`` if no match is found.

    Tests inject an alternate ``proc_fb`` path; production uses the
    default ``/proc/fb``.
    """
    try:
        text = proc_fb.read_text()
    except OSError as exc:
        _log.warning("cannot read %s: %s", proc_fb, exc)
        return None

    for line in text.splitlines():
        # The format is "<index> <name>" — whitespace-separated. We
        # split() with no args to be tolerant of multiple spaces or
        # tabs (kernel doesn't actually mix these, but defensive).
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        idx_str, fb_name = parts
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if fb_name.strip() == name:
            return idx
    return None


def read_fb_info(
    index: int,
    name: str,
    *,
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
) -> FbInfo:
    """Read size/stride/bpp from sysfs for the given fbdev index.

    The kernel exposes these under ``/sys/class/graphics/fb<N>/``:

      - ``virtual_size`` — "W,H" (e.g. "320,170")
      - ``stride``       — bytes per row including padding (e.g. "640")
      - ``bits_per_pixel`` — "16" for RGB565, "32" for ARGB, etc.

    All three are required. A missing or malformed file raises
    ``RuntimeError`` — silent defaults could cause us to write past
    the end of the mmap or misrender at the wrong stride, both of
    which produce visibly broken output that's hard to diagnose.

    The ``name`` argument is plumbed through so the returned ``FbInfo``
    is self-describing for the journal — the caller already had to
    look it up via ``find_fbdev_index`` to get the integer index.
    """
    fb_dir = sysfs_root / f"fb{index}"

    def _read_int(filename: str) -> int:
        path = fb_dir / filename
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"framebuffer fb{index}: cannot parse integer from {path}: {exc}"
            ) from exc

    # virtual_size is "W,H" — split on comma.
    vsize_path = fb_dir / "virtual_size"
    try:
        raw = vsize_path.read_text().strip()
        w_str, h_str = raw.split(",", 1)
        width = int(w_str)
        height = int(h_str)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"framebuffer fb{index}: cannot parse 'virtual_size' from {vsize_path}: {exc}"
        ) from exc

    line_length = _read_int("stride")
    bpp = _read_int("bits_per_pixel")

    return FbInfo(
        index=index,
        width=width,
        height=height,
        bpp=bpp,
        line_length=line_length,
        name=name,
    )


# ── RGB888 → RGB565 conversion ───────────────────────────────────────


def _rgb888_to_rgb565(image: Image.Image) -> bytes:
    """Convert a PIL RGB image to RGB565 bytes in native endianness.

    aarch64 (and x86_64, the host we test on) are both little-endian.
    The Linux fbdev driver for ST7789v3 expects pixels in CPU-native
    16-bit byte order; the driver itself does any further byte-swap
    needed before issuing the SPI MOSI bytes.

    Bit layout for one 16-bit pixel:

        bit  15 14 13 12 11 | 10  9  8  7  6  5 |  4  3  2  1  0
              R  R  R  R  R |  G  G  G  G  G  G |  B  B  B  B  B

    We use numpy for the per-pixel arithmetic; pure-Python iteration
    over 54 400 pixels per frame would dominate the render budget.
    """
    # Lazy import — numpy is already a runtime dep but importing it at
    # module load time is wasted work for headless tests that never
    # call show().
    import numpy as np

    # Make sure we have RGB; PIL may hand us RGBA, P, etc.
    if image.mode != "RGB":
        image = image.convert("RGB")

    arr = np.asarray(image, dtype=np.uint8)        # H × W × 3
    # Promote to uint16 BEFORE shifting so the shift doesn't lose bits
    # on the top end of the green channel.
    r = arr[..., 0].astype(np.uint16) >> 3         # 5 bits
    g = arr[..., 1].astype(np.uint16) >> 2         # 6 bits
    b = arr[..., 2].astype(np.uint16) >> 3         # 5 bits
    rgb565 = (r << 11) | (g << 5) | b              # H × W uint16
    # tobytes() returns native byte order — LE on aarch64/x86_64,
    # which is what the fbdev driver wants.
    return rgb565.tobytes()


# ── DisplayDevice ────────────────────────────────────────────────────


class _Sliceable(Protocol):
    """Anything that supports slice-assignment of ``bytes``.

    In production this is an ``mmap.mmap``; in tests it's a
    ``bytearray``. The fbdev mmap and bytearray share enough of the
    ``MutableSequence`` slice interface that DisplayDevice can be
    fully exercised on host without ever touching ``/dev/fb*``.
    """

    def __setitem__(self, key: slice, value: bytes) -> None: ...


class DisplayDevice:
    """Owns the mmap'd framebuffer and converts frames into pixel writes.

    The class is constructed in two ways:

      - Production: ``DisplayDevice.open()`` discovers the
        framebuffer in ``/proc/fb``, reads its sysfs attributes,
        opens the device, and ``mmap()``s it. The returned device
        owns both the file descriptor and the mmap; ``close()``
        releases both.

      - Tests: the constructor accepts any ``_Sliceable`` (typically a
        ``bytearray``) plus an ``FbInfo``. No file descriptor is
        opened, so the test does not need a real fbdev node.
    """

    def __init__(
        self,
        target: _Sliceable,
        info: FbInfo,
        *,
        fd: Optional[int] = None,
        mmap_obj: Optional[mmap.mmap] = None,
    ) -> None:
        if info.bpp != 16:
            # We only handle RGB565. Refuse to start rather than
            # silently misrender into a 32-bit buffer.
            raise ValueError(
                f"DisplayDevice supports bpp=16 (RGB565); got bpp={info.bpp} "
                f"on fb{info.index} ({info.name})"
            )
        self._target = target
        self._info = info
        self._fd = fd
        self._mmap = mmap_obj

    # ── Production constructor ──────────────────────────────────────

    @classmethod
    def open(
        cls,
        name: str = DEFAULT_FB_NAME,
        *,
        proc_fb: Path = DEFAULT_PROC_FB,
        sysfs_root: Path = DEFAULT_SYSFS_ROOT,
    ) -> "DisplayDevice":
        """Open the kernel framebuffer matching ``name`` and return a device.

        Raises ``RuntimeError`` if discovery or sysfs read fails, and
        ``OSError`` for the underlying ``open(2)`` / ``mmap(2)`` calls.
        """
        idx = find_fbdev_index(name, proc_fb=proc_fb)
        if idx is None:
            raise RuntimeError(
                f"no framebuffer named {name!r} found in {proc_fb}"
            )
        info = read_fb_info(idx, name, sysfs_root=sysfs_root)
        path = f"/dev/fb{idx}"
        # Open the framebuffer for read/write. We need write to push
        # frames; reading is harmless and lets us future-proof for
        # any partial-update or readback functionality.
        fd = os.open(path, os.O_RDWR)
        try:
            m = mmap.mmap(fd, info.frame_bytes, mmap.MAP_SHARED, mmap.PROT_WRITE)
        except Exception:
            os.close(fd)
            raise
        # Paint a clean black frame so we don't show whatever was in
        # framebuffer memory at boot — uninitialised mmaps are zero
        # but a previously-running app may have left content.
        blank = bytes(info.frame_bytes)
        m[:] = blank
        _log.info(
            "display initialised (fb%d %s, %dx%d @ %dbpp, line=%d)",
            info.index, info.name, info.width, info.height,
            info.bpp, info.line_length,
        )
        return cls(m, info, fd=fd, mmap_obj=m)

    # ── Frame push ──────────────────────────────────────────────────

    def show(self, image: Image.Image) -> None:
        """Push a frame. Image must match the panel's exact size.

        We don't auto-resize — ``screens.render`` is responsible for
        producing exactly the right dimensions, and a size mismatch
        is a programming error worth surfacing in tests rather than
        silently distorting the on-screen result.
        """
        if image.size != (self._info.width, self._info.height):
            raise ValueError(
                f"image size {image.size} does not match panel "
                f"{self._info.width}x{self._info.height}"
            )
        rgb565 = _rgb888_to_rgb565(image)
        if self._info.has_padding:
            # Stride-aware row-by-row write. Each visible row is
            # (width*2) bytes; the framebuffer's line_length adds
            # alignment padding after each row that we leave zeroed.
            row_bytes = self._info.visible_row_bytes
            stride = self._info.line_length
            for row in range(self._info.height):
                src_off = row * row_bytes
                dst_off = row * stride
                self._target[dst_off : dst_off + row_bytes] = rgb565[
                    src_off : src_off + row_bytes
                ]
        else:
            # Contiguous: width*2 == line_length. Single block write.
            self._target[: len(rgb565)] = rgb565

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        """Best-effort teardown — flush and release mmap + fd if owned.

        Safe to call multiple times. Errors are logged at WARNING and
        suppressed so a teardown failure can't take the daemon's
        graceful-shutdown path with it.
        """
        if self._mmap is not None:
            try:
                self._mmap.flush()
            except (BufferError, OSError, ValueError) as exc:
                _log.warning("framebuffer flush failed: %s", exc)
            try:
                self._mmap.close()
            except (BufferError, OSError, ValueError) as exc:
                _log.warning("framebuffer mmap close failed: %s", exc)
            self._mmap = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError as exc:
                _log.warning("framebuffer fd close failed: %s", exc)
            self._fd = None

    # ── Read-only diagnostic accessors ──────────────────────────────

    @property
    def info(self) -> FbInfo:
        return self._info


# ── Render thread ────────────────────────────────────────────────────


class RenderThread(threading.Thread):
    """Render loop. Owns framebuffer writes for the lifetime of the program.

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
                    # framebuffer write itself failing — bad mmap,
                    # device unplugged at runtime, etc. We log and
                    # keep looping.
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
    """Test double that records frames instead of pushing them to fbdev.

    Used by host-side tests so the unit suite never needs a real
    framebuffer node, and has been the harness pattern from MiniJS8
    forward.
    """

    def __init__(self) -> None:
        self.frames: list[Image.Image] = []

    def show(self, image: Image.Image) -> None:
        self.frames.append(image)

    def close(self) -> None:
        pass
