"""Tests for the v0.0.17 uConsole framebuffer display backend.

Verifies:
  - UConsoleFramebufferDevice rejects non-uConsole framebuffer info
    (wrong size, wrong bpp) at construction time
  - show() rotates 90 degrees, scales 4x nearest, centers correctly,
    and writes RGB565 bytes to the right positions in the framebuffer
  - The mock-backed device exercises the full rotate+scale+center
    pipeline without needing real hardware
  - is_uconsole_present() correctly distinguishes uConsole signature
    from CardputerZero / generic Pi
  - Lifecycle (close) is idempotent and safe to call twice

ASCII-only policy enforced per the v0.0.14 paste-encoding incident.
The whole rotate-scale-center pipeline is tested against the
PI-2W-TEST-confirmed framebuffer geometry (vc4drmfb, 720x1280, 16bpp).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from microjs8.ui import theme
from microjs8.ui.display import FbInfo
from microjs8.ui.display_uconsole import (
    ROTATION_DEGREES,
    SCALE_FACTOR,
    UCONSOLE_BPP,
    UCONSOLE_FB_NAME,
    UCONSOLE_PANEL_HEIGHT,
    UCONSOLE_PANEL_WIDTH,
    UConsoleFramebufferDevice,
    is_uconsole_present,
)


# -- Helpers ----------------------------------------------------------


def _make_uconsole_fb_info() -> FbInfo:
    """A canonical uConsole FbInfo as observed on real hardware."""
    return FbInfo(
        index=0,
        width=UCONSOLE_PANEL_WIDTH,    # 720
        height=UCONSOLE_PANEL_HEIGHT,  # 1280
        bpp=UCONSOLE_BPP,              # 16
        line_length=720 * 2,           # 1440 bytes/row, no padding
        name=UCONSOLE_FB_NAME,
    )


def _make_test_device() -> tuple[UConsoleFramebufferDevice, bytearray]:
    """A device backed by a fresh bytearray. Returns (device, buffer)."""
    info = _make_uconsole_fb_info()
    buf = bytearray(info.frame_bytes)
    device = UConsoleFramebufferDevice(buf, info)
    return device, buf


# -- Constructor validation -------------------------------------------


class TestConstructorValidation:
    def test_accepts_uconsole_signature(self) -> None:
        # Smoke test: the canonical uConsole signature is accepted
        # without raising.
        device, _ = _make_test_device()
        assert device.info.width == UCONSOLE_PANEL_WIDTH
        assert device.info.height == UCONSOLE_PANEL_HEIGHT
        assert device.info.bpp == UCONSOLE_BPP

    def test_rejects_wrong_width(self) -> None:
        info = FbInfo(
            index=0, width=1024, height=1280, bpp=16,
            line_length=2048, name=UCONSOLE_FB_NAME,
        )
        with pytest.raises(ValueError, match="width"):
            UConsoleFramebufferDevice(bytearray(info.frame_bytes), info)

    def test_rejects_wrong_height(self) -> None:
        info = FbInfo(
            index=0, width=720, height=720, bpp=16,
            line_length=1440, name=UCONSOLE_FB_NAME,
        )
        with pytest.raises(ValueError, match="height"):
            UConsoleFramebufferDevice(bytearray(info.frame_bytes), info)

    def test_rejects_wrong_bpp(self) -> None:
        # 32-bit ARGB would be a desktop framebuffer; refuse.
        info = FbInfo(
            index=0, width=720, height=1280, bpp=32,
            line_length=2880, name=UCONSOLE_FB_NAME,
        )
        with pytest.raises(ValueError, match="bpp"):
            UConsoleFramebufferDevice(bytearray(info.frame_bytes), info)


# -- Geometry constants -----------------------------------------------


class TestGeometry:
    """Verify the compile-time geometry math matches expectations."""

    def test_source_dimensions_are_screen_constants(self) -> None:
        # If theme.SCREEN_W/H change, our scaling math should adapt.
        # This test pins the assumption that the source is the screen
        # so future refactors don't silently break the uConsole path.
        from microjs8.ui import display_uconsole as mod
        assert mod._SOURCE_W == theme.SCREEN_W
        assert mod._SOURCE_H == theme.SCREEN_H

    def test_scale_factor_is_four(self) -> None:
        # 4x is the chosen factor that gives an exact 1280px fit
        # on the long axis (320 source x 4 scale = 1280 panel).
        # If SCREEN_W or SCREEN_H changes, you'll likely also need to
        # revisit this. The post-scale dimensions are asserted below.
        assert SCALE_FACTOR == 4

    def test_scaled_dimensions_fit_panel(self) -> None:
        from microjs8.ui import display_uconsole as mod
        # After 90-degree rotation, source becomes (H x W) = (170 x 320).
        # After 4x scale: 680 x 1280.
        assert mod._SCALED_W == theme.SCREEN_H * SCALE_FACTOR
        assert mod._SCALED_H == theme.SCREEN_W * SCALE_FACTOR
        # Must fit within the panel.
        assert mod._SCALED_W <= UCONSOLE_PANEL_WIDTH
        assert mod._SCALED_H <= UCONSOLE_PANEL_HEIGHT

    def test_centering_offsets(self) -> None:
        from microjs8.ui import display_uconsole as mod
        # 20px horizontal margin (40 bytes), 0 vertical margin
        # for the default 320x170 source.
        assert mod._OFFSET_X == (UCONSOLE_PANEL_WIDTH - mod._SCALED_W) // 2
        assert mod._OFFSET_Y == (UCONSOLE_PANEL_HEIGHT - mod._SCALED_H) // 2
        # Specific values for the documented default config.
        if theme.SCREEN_W == 320 and theme.SCREEN_H == 170:
            assert mod._OFFSET_X == 20
            assert mod._OFFSET_Y == 0


# -- show() pipeline --------------------------------------------------


class TestShowPipeline:
    def test_rejects_wrong_input_size(self) -> None:
        device, _ = _make_test_device()
        bad = Image.new("RGB", (240, 240), (0, 0, 0))
        with pytest.raises(ValueError, match="image size"):
            device.show(bad)

    def test_accepts_correct_input_size(self) -> None:
        device, buf = _make_test_device()
        img = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), (255, 0, 0))
        # Should not raise.
        device.show(img)
        # And should have written non-zero data into the buffer.
        assert buf != bytearray(len(buf))

    def test_solid_color_fills_centered_region(self) -> None:
        # Paint the source entirely red. After rotation+scale, the
        # centered region of the framebuffer should be red (RGB565
        # 0xF800) and the margins should remain zero (black).
        device, buf = _make_test_device()
        red_src = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), (255, 0, 0))
        device.show(red_src)

        from microjs8.ui import display_uconsole as mod
        stride = device.info.line_length

        # Check a pixel inside the centered region (row 100, col 100
        # of the scaled image; offset by _OFFSET_X panel pixels and
        # _OFFSET_Y panel rows).
        row = 100 + mod._OFFSET_Y
        col = 100 + mod._OFFSET_X
        offset = row * stride + col * 2
        # RGB565 little-endian: 0xF800 -> bytes b'\x00\xf8'
        center_pixel = bytes(buf[offset : offset + 2])
        assert center_pixel == b"\x00\xf8", (
            f"expected red 0xF800 at centered position, got "
            f"{center_pixel.hex()}"
        )

    def test_solid_color_does_not_paint_margins(self) -> None:
        # The 20px margins on each side must stay black (zero) after
        # show(). This protects the centered-blit invariant.
        device, buf = _make_test_device()
        red_src = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), (255, 0, 0))
        device.show(red_src)

        from microjs8.ui import display_uconsole as mod
        stride = device.info.line_length

        # Check a pixel in the left margin (col 5 < _OFFSET_X = 20).
        row = 100
        col = 5
        offset = row * stride + col * 2
        left_margin = bytes(buf[offset : offset + 2])
        assert left_margin == b"\x00\x00", (
            f"expected black margin at col {col}, got {left_margin.hex()}"
        )

        # Check a pixel in the right margin (col 700 > _OFFSET_X +
        # _SCALED_W = 20 + 680 = 700).
        col = 710
        offset = row * stride + col * 2
        right_margin = bytes(buf[offset : offset + 2])
        assert right_margin == b"\x00\x00", (
            f"expected black margin at col {col}, got {right_margin.hex()}"
        )

    def test_show_is_repeatable(self) -> None:
        # Two consecutive show() calls must produce the same buffer
        # state -- previous frame's pixels must be fully overwritten
        # by the new frame's centered region.
        device, buf = _make_test_device()
        red = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), (255, 0, 0))
        green = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), (0, 255, 0))
        device.show(red)
        snapshot_after_red = bytes(buf)
        device.show(green)
        snapshot_after_green = bytes(buf)
        # Buffers must differ.
        assert snapshot_after_red != snapshot_after_green
        # Re-painting red should restore the first snapshot exactly.
        device.show(red)
        assert bytes(buf) == snapshot_after_red

    def test_rotation_direction_is_documented(self) -> None:
        # Pin the rotation direction so a future "fix" to PIL's
        # rotate semantics doesn't silently flip the panel.
        # v0.0.18: -90 (CW) is the hardware-verified value, replacing
        # the v0.0.17 default of +90 (CCW) which rendered upside-down
        # on the real uConsole panel mount orientation.
        assert ROTATION_DEGREES == -90


# -- Lifecycle --------------------------------------------------------


class TestLifecycle:
    def test_close_is_idempotent_when_no_fd(self) -> None:
        # The test constructor passes fd=None and mmap_obj=None.
        # close() must handle that gracefully.
        device, _ = _make_test_device()
        device.close()
        device.close()  # Second call must not raise.


# -- is_uconsole_present() --------------------------------------------


class TestIsUConsolePresent:
    def _write_sysfs(
        self, sysfs: Path, index: int, name: str,
        width: int, height: int, bpp: int, stride: int,
    ) -> None:
        fb_dir = sysfs / f"fb{index}"
        fb_dir.mkdir(parents=True, exist_ok=True)
        (fb_dir / "virtual_size").write_text(f"{width},{height}")
        (fb_dir / "stride").write_text(str(stride))
        (fb_dir / "bits_per_pixel").write_text(str(bpp))

    def test_detects_uconsole_signature(self, tmp_path: Path) -> None:
        proc_fb = tmp_path / "proc_fb"
        proc_fb.write_text("0 vc4drmfb\n")
        sysfs = tmp_path / "sys_class_graphics"
        self._write_sysfs(sysfs, 0, "vc4drmfb", 720, 1280, 16, 1440)
        assert is_uconsole_present(proc_fb=proc_fb, sysfs_root=sysfs) is True

    def test_rejects_cardputerzero_signature(self, tmp_path: Path) -> None:
        # CardputerZero uses fb_st7789v, not vc4drmfb -- no match.
        proc_fb = tmp_path / "proc_fb"
        proc_fb.write_text("0 fb_st7789v\n")
        sysfs = tmp_path / "sys_class_graphics"
        self._write_sysfs(sysfs, 0, "fb_st7789v", 320, 170, 16, 640)
        assert is_uconsole_present(proc_fb=proc_fb, sysfs_root=sysfs) is False

    def test_rejects_wrong_dimensions(self, tmp_path: Path) -> None:
        # vc4drmfb but with a 1920x1080 desktop panel -- not uConsole.
        proc_fb = tmp_path / "proc_fb"
        proc_fb.write_text("0 vc4drmfb\n")
        sysfs = tmp_path / "sys_class_graphics"
        self._write_sysfs(sysfs, 0, "vc4drmfb", 1920, 1080, 16, 3840)
        assert is_uconsole_present(proc_fb=proc_fb, sysfs_root=sysfs) is False

    def test_rejects_wrong_bpp(self, tmp_path: Path) -> None:
        # vc4drmfb with correct dimensions but 32 bpp -- not the
        # uConsole's RGB565 path.
        proc_fb = tmp_path / "proc_fb"
        proc_fb.write_text("0 vc4drmfb\n")
        sysfs = tmp_path / "sys_class_graphics"
        self._write_sysfs(sysfs, 0, "vc4drmfb", 720, 1280, 32, 2880)
        assert is_uconsole_present(proc_fb=proc_fb, sysfs_root=sysfs) is False

    def test_returns_false_when_no_framebuffer(self, tmp_path: Path) -> None:
        # /proc/fb missing entirely.
        proc_fb = tmp_path / "proc_fb_missing"
        sysfs = tmp_path / "sys_class_graphics"
        assert is_uconsole_present(proc_fb=proc_fb, sysfs_root=sysfs) is False

    def test_returns_false_on_malformed_sysfs(self, tmp_path: Path) -> None:
        proc_fb = tmp_path / "proc_fb"
        proc_fb.write_text("0 vc4drmfb\n")
        sysfs = tmp_path / "sys_class_graphics"
        # fb0 directory exists but virtual_size is garbage.
        fb_dir = sysfs / "fb0"
        fb_dir.mkdir(parents=True)
        (fb_dir / "virtual_size").write_text("not-a-size")
        (fb_dir / "stride").write_text("1440")
        (fb_dir / "bits_per_pixel").write_text("16")
        # read_fb_info raises RuntimeError; is_uconsole_present must
        # catch that and return False rather than propagate.
        assert is_uconsole_present(proc_fb=proc_fb, sysfs_root=sysfs) is False


# -- ASCII source policy -----------------------------------------------


class TestSourceFileIsAscii:
    """Per v0.0.14 paste-encoding incident: code stays pure ASCII."""

    def test_backend_source_is_ascii(self) -> None:
        import microjs8.ui.display_uconsole as mod
        raw = open(mod.__file__, "rb").read()
        non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        assert not non_ascii, (
            f"display_uconsole.py contains {len(non_ascii)} non-ASCII "
            f"bytes (first at offset {non_ascii[0][0]} = "
            f"0x{non_ascii[0][1]:02x}); source must be pure ASCII "
            "per the v0.0.14 paste-encoding policy"
        )
