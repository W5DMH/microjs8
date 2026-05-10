"""Tests for the Phase 5 framebuffer display backend.

The DisplayDevice + discovery functions are exercised end-to-end on
host without a real ``/dev/fb*`` node:

  - ``find_fbdev_index`` and ``read_fb_info`` take an injectable root
    path; tests point them at ``tmp_path``.
  - ``DisplayDevice.__init__`` accepts a writable target buffer
    (``bytearray``) and a hand-built ``FbInfo``, so the show() path
    is exercised against ordinary memory.

What we cover:

  * ``find_fbdev_index`` matches a name, ignores other lines, returns
    None for no match, tolerates malformed lines without raising.
  * ``read_fb_info`` parses sysfs files correctly, raises with a
    useful message when files are missing or malformed.
  * ``_rgb888_to_rgb565`` produces the right bits for primary colours
    and grayscale, and emits 2 bytes per pixel in little-endian
    native order (the ARM/x86 fbdev expectation).
  * ``DisplayDevice.show`` blits a 320×170 image into a contiguous
    bytearray correctly.
  * ``DisplayDevice.show`` handles a stride > width*2 (line padding)
    by writing per-row and leaving the padding bytes untouched.
  * ``DisplayDevice`` rejects non-16bpp ``FbInfo`` at construction
    rather than corrupting the framebuffer.
  * ``DisplayDevice.show`` rejects an image with the wrong size
    rather than silently distorting.
  * ``DisplayDevice.close`` is idempotent and tolerant of fake
    targets that have no fd / mmap.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PIL import Image

from microjs8.ui import display
from microjs8.ui.display import (
    DisplayDevice,
    FbInfo,
    _rgb888_to_rgb565,
    find_fbdev_index,
    read_fb_info,
)


# ── /proc/fb discovery ───────────────────────────────────────────────


def test_find_fbdev_index_returns_index_for_match(tmp_path: Path):
    """Standard /proc/fb format: index, space, name, newline."""
    proc_fb = tmp_path / "fb"
    proc_fb.write_text("0 fb_st7789v\n1 fb_some_other\n")
    assert find_fbdev_index("fb_st7789v", proc_fb=proc_fb) == 0


def test_find_fbdev_index_when_st7789_is_not_fb0(tmp_path: Path):
    """When HDMI's DRM device claims fb0, the SPI panel may be at fb1+."""
    proc_fb = tmp_path / "fb"
    proc_fb.write_text("0 fb_drm_hdmi\n1 fb_st7789v\n")
    assert find_fbdev_index("fb_st7789v", proc_fb=proc_fb) == 1


def test_find_fbdev_index_returns_none_when_missing(tmp_path: Path):
    proc_fb = tmp_path / "fb"
    proc_fb.write_text("0 fb_drm_hdmi\n")
    assert find_fbdev_index("fb_st7789v", proc_fb=proc_fb) is None


def test_find_fbdev_index_returns_none_if_proc_fb_does_not_exist(tmp_path: Path):
    """Daemon must not crash when /proc/fb is missing — e.g. running on
    a build host with no graphics support, or in a stripped container."""
    missing = tmp_path / "no_such_file"
    assert find_fbdev_index("fb_st7789v", proc_fb=missing) is None


def test_find_fbdev_index_tolerates_malformed_lines(tmp_path: Path):
    """A blank line or a bare-text line in /proc/fb must not raise."""
    proc_fb = tmp_path / "fb"
    proc_fb.write_text(
        "\n"                 # blank line
        "garbage\n"          # no integer
        "0 fb_st7789v\n"     # the one we want
        "abc fb_other\n"     # invalid integer
    )
    assert find_fbdev_index("fb_st7789v", proc_fb=proc_fb) == 0


# ── sysfs read_fb_info ───────────────────────────────────────────────


def _write_sysfs_fb(root: Path, idx: int, *, w: int, h: int, bpp: int, stride: int) -> Path:
    """Build a fake /sys/class/graphics/fb<N>/ tree."""
    d = root / f"fb{idx}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "virtual_size").write_text(f"{w},{h}\n")
    (d / "stride").write_text(f"{stride}\n")
    (d / "bits_per_pixel").write_text(f"{bpp}\n")
    return d


def test_read_fb_info_parses_all_attributes(tmp_path: Path):
    _write_sysfs_fb(tmp_path, 0, w=320, h=170, bpp=16, stride=640)
    info = read_fb_info(0, "fb_st7789v", sysfs_root=tmp_path)
    assert info.index == 0
    assert info.width == 320
    assert info.height == 170
    assert info.bpp == 16
    assert info.line_length == 640
    assert info.name == "fb_st7789v"
    assert info.frame_bytes == 640 * 170
    assert info.visible_row_bytes == 320 * 2
    assert not info.has_padding


def test_read_fb_info_detects_padding(tmp_path: Path):
    """If the kernel pads each row to a 256-byte alignment for some
    reason, line_length > width*2; FbInfo.has_padding must reflect it."""
    _write_sysfs_fb(tmp_path, 0, w=320, h=170, bpp=16, stride=768)
    info = read_fb_info(0, "fb_st7789v", sysfs_root=tmp_path)
    assert info.line_length == 768
    assert info.visible_row_bytes == 640
    assert info.has_padding


def test_read_fb_info_raises_on_missing_file(tmp_path: Path):
    """A missing 'stride' file is a kernel/DT misconfiguration we
    can't recover from. Raise loudly instead of guessing."""
    d = tmp_path / "fb0"
    d.mkdir()
    (d / "virtual_size").write_text("320,170\n")
    (d / "bits_per_pixel").write_text("16\n")
    # No 'stride' file — should raise.
    with pytest.raises(RuntimeError, match=r"stride"):
        read_fb_info(0, "fb_st7789v", sysfs_root=tmp_path)


def test_read_fb_info_raises_on_malformed_virtual_size(tmp_path: Path):
    d = tmp_path / "fb0"
    d.mkdir()
    (d / "virtual_size").write_text("not_a_size\n")
    (d / "stride").write_text("640\n")
    (d / "bits_per_pixel").write_text("16\n")
    with pytest.raises(RuntimeError, match=r"virtual_size"):
        read_fb_info(0, "fb_st7789v", sysfs_root=tmp_path)


# ── RGB565 conversion ────────────────────────────────────────────────


def _pixel_uint16(rgb565_bytes: bytes, x: int, y: int, *, width: int) -> int:
    """Decode the little-endian uint16 at pixel (x,y) from a packed buffer."""
    offset = (y * width + x) * 2
    lo, hi = rgb565_bytes[offset], rgb565_bytes[offset + 1]
    return lo | (hi << 8)


def test_rgb888_to_rgb565_pure_red():
    img = Image.new("RGB", (1, 1), (255, 0, 0))
    out = _rgb888_to_rgb565(img)
    # (255 >> 3) = 31 = 0b11111 → R5 << 11 = 0xF800
    assert _pixel_uint16(out, 0, 0, width=1) == 0xF800


def test_rgb888_to_rgb565_pure_green():
    img = Image.new("RGB", (1, 1), (0, 255, 0))
    out = _rgb888_to_rgb565(img)
    # (255 >> 2) = 63 = 0b111111 → G6 << 5 = 0x07E0
    assert _pixel_uint16(out, 0, 0, width=1) == 0x07E0


def test_rgb888_to_rgb565_pure_blue():
    img = Image.new("RGB", (1, 1), (0, 0, 255))
    out = _rgb888_to_rgb565(img)
    assert _pixel_uint16(out, 0, 0, width=1) == 0x001F


def test_rgb888_to_rgb565_pure_white():
    img = Image.new("RGB", (1, 1), (255, 255, 255))
    out = _rgb888_to_rgb565(img)
    assert _pixel_uint16(out, 0, 0, width=1) == 0xFFFF


def test_rgb888_to_rgb565_pure_black():
    img = Image.new("RGB", (1, 1), (0, 0, 0))
    out = _rgb888_to_rgb565(img)
    assert _pixel_uint16(out, 0, 0, width=1) == 0x0000


def test_rgb888_to_rgb565_byte_count_matches_pixel_count():
    """Output is exactly 2 bytes per pixel — proves no padding leaked."""
    img = Image.new("RGB", (320, 170), (12, 34, 56))
    out = _rgb888_to_rgb565(img)
    assert len(out) == 320 * 170 * 2


def test_rgb888_to_rgb565_endianness_is_little():
    """The fbdev driver expects native (LE) byte order on aarch64.
    For pixel value 0xF800 (pure red), bytes are [0x00, 0xF8].
    """
    img = Image.new("RGB", (1, 1), (255, 0, 0))
    out = _rgb888_to_rgb565(img)
    assert out == bytes([0x00, 0xF8])


def test_rgb888_to_rgb565_handles_rgba_input():
    """If somebody hands us RGBA (e.g. a screen renderer that used
    ImageDraw.rectangle with alpha), we convert before packing."""
    img = Image.new("RGBA", (1, 1), (255, 0, 0, 128))
    out = _rgb888_to_rgb565(img)
    assert _pixel_uint16(out, 0, 0, width=1) == 0xF800


# ── DisplayDevice ────────────────────────────────────────────────────


def _make_info(*, width: int = 320, height: int = 170,
               bpp: int = 16, stride: int | None = None) -> FbInfo:
    if stride is None:
        stride = width * (bpp // 8)
    return FbInfo(
        index=0,
        width=width,
        height=height,
        bpp=bpp,
        line_length=stride,
        name="fb_st7789v",
    )


def test_displaydevice_rejects_non_16_bpp():
    """Constructor refuses bpp != 16 — protects against silently
    misrendering into a 32-bit buffer at half the expected stride."""
    target = bytearray(320 * 170 * 4)
    info = _make_info(bpp=32, stride=320 * 4)
    with pytest.raises(ValueError, match=r"bpp=16"):
        DisplayDevice(target, info)


def test_displaydevice_show_writes_contiguous_buffer():
    """When line_length == width*2 (no padding), show() writes one
    contiguous block. Verify by sampling pixel (0,0) and (319,169)."""
    info = _make_info()
    target = bytearray(info.frame_bytes)
    dev = DisplayDevice(target, info)

    img = Image.new("RGB", (320, 170), (255, 0, 0))   # pure red
    dev.show(img)

    # Every pixel should be little-endian 0xF800.
    assert _pixel_uint16(bytes(target), 0, 0, width=320) == 0xF800
    assert _pixel_uint16(bytes(target), 319, 169, width=320) == 0xF800


def test_displaydevice_show_handles_stride_padding():
    """When line_length > width*2, show() must blit row-by-row
    with the destination offset incremented by stride. The padding
    bytes between rows must remain untouched (they were 0xFF in our
    pre-painted buffer; if the stride math is wrong they'd be
    overwritten with pixel data)."""
    info = _make_info(stride=768)         # 128 padding bytes per row
    target = bytearray(b"\xFF" * info.frame_bytes)   # pre-paint with sentinel
    dev = DisplayDevice(target, info)

    img = Image.new("RGB", (320, 170), (0, 0, 255))   # pure blue
    dev.show(img)

    # First row: visible bytes are RGB565 blue (0x001F → bytes 1F 00).
    # Expect [0x1F, 0x00] at offset 0, then [0x1F, 0x00] for each
    # subsequent visible pixel. After byte 640 (visible_row_bytes),
    # the next 128 bytes are padding and must still be 0xFF.
    assert target[0:2] == b"\x1F\x00"
    assert target[638:640] == b"\x1F\x00"
    assert target[640:768] == b"\xFF" * 128, (
        "stride padding bytes were overwritten — show() is not "
        "stride-aware"
    )
    # Second row's visible region starts at byte 768 (the stride)
    assert target[768:770] == b"\x1F\x00"


def test_displaydevice_show_rejects_size_mismatch():
    """Wrong-size image should raise rather than distort."""
    info = _make_info()
    target = bytearray(info.frame_bytes)
    dev = DisplayDevice(target, info)

    wrong_size = Image.new("RGB", (240, 240), (0, 0, 0))
    with pytest.raises(ValueError, match=r"image size"):
        dev.show(wrong_size)


def test_displaydevice_close_is_idempotent_on_fake_target():
    """Tests pass a bytearray (no fd, no mmap). close() must be a
    no-op and must be safe to call repeatedly."""
    info = _make_info()
    dev = DisplayDevice(bytearray(info.frame_bytes), info)
    dev.close()
    dev.close()    # second call must not raise


def test_displaydevice_open_raises_when_no_matching_fb(tmp_path: Path):
    """Production constructor: an empty /proc/fb means no panel,
    raise RuntimeError so the daemon's best-effort wrapper can log
    and continue headless."""
    proc_fb = tmp_path / "fb"
    proc_fb.write_text("")
    with pytest.raises(RuntimeError, match=r"fb_st7789v"):
        DisplayDevice.open(proc_fb=proc_fb, sysfs_root=tmp_path)


# ── End-to-end: render a real screen into the buffer ─────────────────


def test_displaydevice_can_consume_a_real_rendered_screen():
    """Round-trip: render a HOME screen to a PIL image, push it
    through DisplayDevice into a bytearray, and verify the buffer
    contains non-zero pixels (i.e. something actually painted).
    Catches silent breakage where show() runs without raising but
    produces an empty/black frame."""
    from microjs8.ui import screens
    from microjs8.ui.fonts import load_fonts
    from microjs8.ui.state import Screen, UIState

    state = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    state.set_screen(Screen.HOME)
    snap = state.snapshot()

    fonts = load_fonts()
    img = screens.render(snap, fonts)

    info = _make_info()
    target = bytearray(info.frame_bytes)
    dev = DisplayDevice(target, info)
    dev.show(img)

    # At least some pixels must be non-black after rendering HOME
    # (header bar, callsign text, etc.). Count non-zero bytes.
    nonzero = sum(1 for b in target if b != 0)
    assert nonzero > 100, (
        f"only {nonzero} non-zero bytes in framebuffer — show() may have "
        f"failed to write the rendered image"
    )
