"""Phase 18 tests: userspace SPI driver for the ST7789V panel.

Strategy: mock SpiDev and DigitalOutputDevice entirely. Every test
either inspects the recorded SPI/GPIO transactions or exercises the
class's error paths. No real hardware is touched.

The fakes are intentionally minimal — just enough to satisfy the
``_SpiPort`` and ``_DigitalOut`` Protocols in display_spi.py.
"""

from __future__ import annotations

import io
import logging
from typing import Any, List, Tuple
from unittest.mock import patch

import pytest
from PIL import Image

from microjs8.ui.display_spi import (
    CMD_CASET,
    CMD_COLMOD,
    CMD_DISPOFF,
    CMD_DISPON,
    CMD_INVON,
    CMD_MADCTL,
    CMD_NORON,
    CMD_RAMWR,
    CMD_RASET,
    CMD_SLPOUT,
    CMD_SWRESET,
    COLMOD_RGB565,
    MADCTL_LANDSCAPE,
    PANEL_1_9_LANDSCAPE_HEIGHT,
    PANEL_1_9_LANDSCAPE_WIDTH,
    PANEL_1_9_LANDSCAPE_X_OFFSET,
    PANEL_1_9_LANDSCAPE_Y_OFFSET,
    SpiDisplayDevice,
    _rgb888_to_rgb565_be,
)


# ── Test fakes ──────────────────────────────────────────────────────


class FakeSpi:
    """Records every writebytes2 call. Mimics spidev.SpiDev's surface."""

    def __init__(self) -> None:
        self.transfers: List[bytes] = []
        self.max_speed_hz: int = 0
        self.mode: int = 0
        self.closed: bool = False

    def writebytes2(self, data) -> None:
        if self.closed:
            raise OSError("SPI port closed")
        # Materialize the input — accept bytes/bytearray/memoryview.
        self.transfers.append(bytes(data))

    def close(self) -> None:
        self.closed = True

    def total_bytes_written(self) -> int:
        return sum(len(t) for t in self.transfers)


class FakeGpio:
    """Records on/off transitions for a single GPIO line."""

    def __init__(self, name: str = "?") -> None:
        self.name = name
        self.transitions: List[str] = []  # "on" / "off" / "close"
        self.state: bool = False
        self.closed: bool = False

    def on(self) -> None:
        if self.closed:
            raise OSError(f"GPIO {self.name} closed")
        self.transitions.append("on")
        self.state = True

    def off(self) -> None:
        if self.closed:
            raise OSError(f"GPIO {self.name} closed")
        self.transitions.append("off")
        self.state = False

    def close(self) -> None:
        self.transitions.append("close")
        self.closed = True


class FakeSleep:
    """Records sleep durations. No actual blocking — tests run fast."""

    def __init__(self) -> None:
        self.calls: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


def _make_device(
    *, run_init: bool = True, **kwargs
) -> Tuple[SpiDisplayDevice, FakeSpi, FakeGpio, FakeGpio, FakeGpio, FakeSleep]:
    """Construct a SpiDisplayDevice with fake hardware. Returns
    the device plus all the fakes so the test can inspect them."""
    spi = FakeSpi()
    dc = FakeGpio("DC")
    rst = FakeGpio("RST")
    bl = FakeGpio("BL")
    sleep = FakeSleep()
    device = SpiDisplayDevice(
        spi=spi, dc=dc, rst=rst, bl=bl,
        run_init=run_init, sleep=sleep, **kwargs,
    )
    return device, spi, dc, rst, bl, sleep


def _decode_transfers(spi: FakeSpi, dc: FakeGpio) -> List[Tuple[str, bytes]]:
    """For tests that need to reason about command vs data bytes,
    walk the DC transitions interleaved with SPI transfers and
    label each transfer as ('cmd', bytes) or ('data', bytes).

    This is approximate — we can't perfectly reconstruct interleaving
    from the FakeGpio's flat list because DC transitions and
    writebytes2 calls happen in order on a single thread, so we
    rely on the fact that the test driver runs single-threaded and
    DC.on()/off() before each writebytes2 call determines the mode.

    Tests that need precise ordering should inspect spi.transfers
    and dc.transitions directly instead.
    """
    # Use the call order: DC state at the time of each writebytes2
    # is determined by which DC transition was most recent.
    labels: List[Tuple[str, bytes]] = []
    dc_state = False
    dc_iter = iter(dc.transitions)
    # Match: in the implementation, every cmd() call does
    #   dc.off(); writebytes2(cmd); [dc.on(); writebytes2(data)]
    # So DC transitions and writebytes2 calls alternate predictably.
    # We replay them in order.
    transfer_iter = iter(spi.transfers)
    for transition in dc.transitions:
        if transition == "close":
            break
        if transition == "off":
            dc_state = False
            try:
                payload = next(transfer_iter)
                labels.append(("cmd", payload))
            except StopIteration:
                pass
        elif transition == "on":
            dc_state = True
            try:
                payload = next(transfer_iter)
                labels.append(("data", payload))
            except StopIteration:
                pass
    return labels


# ── RGB565 BE conversion ────────────────────────────────────────────


def test_rgb565_be_pure_red():
    """Pure red (255,0,0) → R=31, G=0, B=0 → 0xF800
    → big-endian bytes 0xF8, 0x00."""
    img = Image.new("RGB", (1, 1), (255, 0, 0))
    out = _rgb888_to_rgb565_be(img)
    assert out == b"\xF8\x00"


def test_rgb565_be_pure_green():
    """Pure green (0,255,0) → R=0, G=63, B=0 → 0x07E0
    → big-endian bytes 0x07, 0xE0."""
    img = Image.new("RGB", (1, 1), (0, 255, 0))
    out = _rgb888_to_rgb565_be(img)
    assert out == b"\x07\xE0"


def test_rgb565_be_pure_blue():
    """Pure blue (0,0,255) → R=0, G=0, B=31 → 0x001F
    → big-endian bytes 0x00, 0x1F."""
    img = Image.new("RGB", (1, 1), (0, 0, 255))
    out = _rgb888_to_rgb565_be(img)
    assert out == b"\x00\x1F"


def test_rgb565_be_size_for_full_panel():
    """A 320×170 image must produce exactly 320*170*2 = 108_800 bytes."""
    img = Image.new("RGB", (320, 170), (128, 64, 32))
    out = _rgb888_to_rgb565_be(img)
    assert len(out) == 320 * 170 * 2


def test_rgb565_be_converts_rgba_to_rgb():
    """RGBA input should be silently converted — the daemon's
    renderer occasionally produces RGBA buffers, and a bare TypeError
    would crash the render thread."""
    img = Image.new("RGBA", (1, 1), (255, 0, 0, 128))
    out = _rgb888_to_rgb565_be(img)
    assert out == b"\xF8\x00"


# ── Init sequence ───────────────────────────────────────────────────


def test_init_sequence_command_order():
    """Verify the init sequence sends commands in the documented order:
    SWRESET → SLPOUT → COLMOD → MADCTL → INVON → NORON → DISPON.
    Order matters — sending DISPON before SLPOUT is documented to
    leave the panel in an undefined state."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)

    # The init sequence cmd() calls produce alternating DC off/on
    # transitions and SPI writes. Pull out just the command bytes
    # (the bytes following a DC.off transition).
    cmds_sent = []
    dc_state = True  # start state doesn't matter, we look at transitions
    transfer_idx = 0
    for transition in dc.transitions:
        if transition == "off":
            # Next transfer is a command byte
            if transfer_idx < len(spi.transfers):
                cmds_sent.append(spi.transfers[transfer_idx][0])
                transfer_idx += 1
                dc_state = False
        elif transition == "on":
            # Next transfer is data (if any)
            if transfer_idx < len(spi.transfers):
                transfer_idx += 1
            dc_state = True

    expected_order = [
        CMD_SWRESET, CMD_SLPOUT, CMD_COLMOD, CMD_MADCTL,
        CMD_INVON, CMD_NORON, CMD_DISPON,
    ]
    # cmds_sent should start with this exact sequence (no show() yet).
    assert cmds_sent[:len(expected_order)] == expected_order


def test_init_sequence_pulses_reset():
    """Hardware reset must pulse RST: high→low→high with delays.
    Without the pulse, some ST7789V batches don't accept commands."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    # First three transitions on RST should be on/off/on.
    assert rst.transitions[:3] == ["on", "off", "on"]


def test_init_colmod_payload_is_rgb565():
    """COLMOD must be 0x55 (RGB565). Wrong value either fails silently
    or produces visibly broken pixels."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    # Find the COLMOD command in spi.transfers — it's the byte
    # immediately preceding the COLMOD payload.
    for i, t in enumerate(spi.transfers):
        if len(t) == 1 and t[0] == CMD_COLMOD:
            payload = spi.transfers[i + 1]
            assert payload == bytes([COLMOD_RGB565])
            return
    pytest.fail("COLMOD command not sent during init")


def test_init_madctl_payload_is_landscape():
    """MADCTL must be 0x60 (MV=1, MX=1) for our landscape orientation."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    for i, t in enumerate(spi.transfers):
        if len(t) == 1 and t[0] == CMD_MADCTL:
            payload = spi.transfers[i + 1]
            assert payload == bytes([MADCTL_LANDSCAPE])
            return
    pytest.fail("MADCTL command not sent during init")


def test_init_turns_backlight_on_at_end():
    """Backlight should come on only at the end of init, so the
    operator never sees a momentary garbage frame during reset."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    # The backlight should be in the 'on' state at end of init.
    assert bl.state is True
    # And the very last transition should be 'on' (not on then off).
    assert bl.transitions[-1] == "on"


def test_init_includes_documented_delays():
    """The datasheet requires specific minimum delays between
    certain commands (SWRESET 120 ms, SLPOUT 500 ms, etc.). We use
    150/500 with margin. Verify the sleep budget is at least the
    documented minimum — without this delay, SLPOUT can be ignored."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    total = sum(sleep.calls)
    # Datasheet minimum is ~770 ms across the init. We use ~1300 ms
    # with margin. Either way, at least 700 ms total.
    assert total >= 0.7, f"init sleep budget too short: {total} s"


# ── show() pixel push ──────────────────────────────────────────────


def test_show_writes_caset_with_correct_offsets():
    """For the 1.9" panel in landscape we don't offset columns — but
    we DO offset rows by 35. CASET should encode (0, 319) and not
    apply any offset."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    initial_count = len(spi.transfers)

    img = Image.new("RGB", (320, 170), (0, 0, 0))
    device.show(img)

    # CASET is the first command after init. Look for it.
    for i in range(initial_count, len(spi.transfers)):
        if len(spi.transfers[i]) == 1 and spi.transfers[i][0] == CMD_CASET:
            payload = spi.transfers[i + 1]
            assert len(payload) == 4
            x_start = (payload[0] << 8) | payload[1]
            x_end   = (payload[2] << 8) | payload[3]
            assert x_start == 0
            assert x_end == 319, f"CASET x_end={x_end}, expected 319"
            return
    pytest.fail("CASET not sent during show()")


def test_show_writes_raset_with_35_row_offset():
    """For the 1.9" landscape panel the row window is 35..204 (35 +
    170 - 1 = 204). This is the critical offset — without it, the
    image renders shifted on the panel and the bottom rows are
    invisible."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    initial_count = len(spi.transfers)

    img = Image.new("RGB", (320, 170), (0, 0, 0))
    device.show(img)

    for i in range(initial_count, len(spi.transfers)):
        if len(spi.transfers[i]) == 1 and spi.transfers[i][0] == CMD_RASET:
            payload = spi.transfers[i + 1]
            assert len(payload) == 4
            y_start = (payload[0] << 8) | payload[1]
            y_end   = (payload[2] << 8) | payload[3]
            assert y_start == 35, f"RASET y_start={y_start}, expected 35"
            assert y_end == 204, f"RASET y_end={y_end}, expected 204"
            return
    pytest.fail("RASET not sent during show()")


def test_show_sends_ramwr_before_pixels():
    """RAMWR must come right before pixel data. Without it the panel
    interprets the pixel bytes as commands and goes into an undefined
    state."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    initial_count = len(spi.transfers)

    img = Image.new("RGB", (320, 170), (0, 0, 0))
    device.show(img)

    # Find RAMWR and confirm there's pixel data right after it
    # (in 'data' mode — DC.on()).
    for i in range(initial_count, len(spi.transfers)):
        if len(spi.transfers[i]) == 1 and spi.transfers[i][0] == CMD_RAMWR:
            # Everything after this should be pixel data.
            # Total pixel bytes = 320 * 170 * 2 = 108_800.
            total_pixel_bytes = sum(
                len(t) for t in spi.transfers[i + 1:]
            )
            assert total_pixel_bytes == 320 * 170 * 2
            return
    pytest.fail("RAMWR not sent during show()")


def test_show_total_pixel_byte_count():
    """The cumulative pixel data sent must equal width * height * 2.
    Off-by-one or stride-padding bugs would show up as
    mismatched totals."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    init_count = len(spi.transfers)

    img = Image.new("RGB", (320, 170), (100, 200, 50))
    device.show(img)

    # Sum all transfers after RAMWR. RAMWR is identified by being a
    # single byte equal to CMD_RAMWR (0x2C).
    pixels_started = False
    pixel_bytes = 0
    for t in spi.transfers[init_count:]:
        if pixels_started:
            pixel_bytes += len(t)
        elif len(t) == 1 and t[0] == CMD_RAMWR:
            pixels_started = True
    assert pixel_bytes == 320 * 170 * 2, (
        f"sent {pixel_bytes} pixel bytes, expected {320 * 170 * 2}"
    )


def test_show_chunks_large_transfers():
    """A 108 KB frame must be split into ≤4096-byte chunks so that
    older spidev buffers (default 4096) don't refuse the write."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    init_count = len(spi.transfers)

    img = Image.new("RGB", (320, 170), (0, 0, 0))
    device.show(img)

    # All pixel transfers (after RAMWR) must be ≤ chunk size.
    pixels_started = False
    for t in spi.transfers[init_count:]:
        if pixels_started:
            assert len(t) <= SpiDisplayDevice._SPI_CHUNK, (
                f"pixel chunk too large: {len(t)} > {SpiDisplayDevice._SPI_CHUNK}"
            )
        elif len(t) == 1 and t[0] == CMD_RAMWR:
            pixels_started = True


def test_show_rejects_mismatched_image_size():
    """A 100×100 image fed to a 320×170 device should raise rather
    than silently render garbage."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    bad_img = Image.new("RGB", (100, 100), (0, 0, 0))
    with pytest.raises(ValueError, match="doesn't match panel size"):
        device.show(bad_img)


def test_show_after_close_raises():
    """show() after close() must raise, not silently no-op — a render
    thread that's been told to stop should not still be pushing
    pixels."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    device.close()
    img = Image.new("RGB", (320, 170), (0, 0, 0))
    with pytest.raises(RuntimeError, match="after close"):
        device.show(img)


# ── close() ─────────────────────────────────────────────────────────


def test_close_blanks_panel_and_turns_off_backlight():
    """close() should leave the panel dark — display off, backlight
    off. Otherwise the last frame stays visible after the daemon
    stops, which is confusing."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)

    # Find init-time DISPOFF count (should be 0)
    init_dispoff_count = sum(
        1 for t in spi.transfers if len(t) == 1 and t[0] == CMD_DISPOFF
    )

    device.close()

    # After close, DISPOFF should have been sent once.
    dispoff_count = sum(
        1 for t in spi.transfers if len(t) == 1 and t[0] == CMD_DISPOFF
    )
    assert dispoff_count == init_dispoff_count + 1

    # Backlight should be off
    assert bl.state is False


def test_close_is_idempotent():
    """Calling close() twice must not raise — the daemon's cleanup
    paths sometimes redundantly close devices, and double-close
    should be safe."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    device.close()
    device.close()  # must not raise
    assert spi.closed is True


def test_close_releases_all_resources():
    """SPI, DC, RST, BL must all be closed."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)
    device.close()
    assert spi.closed
    assert dc.closed
    assert rst.closed
    assert bl.closed


def test_close_swallows_resource_errors():
    """If one of the close() calls raises (e.g. SPI bus already gone
    during a crash), the remaining closes must still run. Otherwise
    a partial cleanup leaks GPIO handles."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)

    # Make SPI close raise — we should still close the GPIO pins.
    original_close = spi.close
    def bad_close():
        original_close()
        raise OSError("simulated SPI close failure")
    spi.close = bad_close

    device.close()  # must not raise
    # GPIO pins should still have been closed
    assert dc.closed
    assert rst.closed
    assert bl.closed


# ── Backlight on/off ────────────────────────────────────────────────


def test_backlight_on_off_methods():
    """The Fn+B / Ctrl+B handler needs explicit backlight control
    methods on the device, mirroring the Backlight class's surface."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=True)

    # After init, backlight is on.
    assert bl.state is True

    device.backlight_off()
    assert bl.state is False

    device.backlight_on()
    assert bl.state is True


# ── Construction defaults + validation ──────────────────────────────


def test_default_construction_uses_1_9_inch_geometry():
    """Default kwargs should match the Waveshare 1.9" 170×320 panel —
    the bare-Pi target hardware. New panel variants will pass
    different kwargs."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=False)
    assert device.width == PANEL_1_9_LANDSCAPE_WIDTH == 320
    assert device.height == PANEL_1_9_LANDSCAPE_HEIGHT == 170


def test_construction_rejects_invalid_dimensions():
    """Zero or negative dimensions should raise — catching a typo
    early beats writing 0 bytes to the panel forever."""
    spi = FakeSpi()
    dc = FakeGpio("DC"); rst = FakeGpio("RST"); bl = FakeGpio("BL")
    with pytest.raises(ValueError, match="dimensions must be positive"):
        SpiDisplayDevice(
            spi=spi, dc=dc, rst=rst, bl=bl,
            width=0, height=170, run_init=False, sleep=FakeSleep(),
        )
    with pytest.raises(ValueError, match="dimensions must be positive"):
        SpiDisplayDevice(
            spi=spi, dc=dc, rst=rst, bl=bl,
            width=320, height=-1, run_init=False, sleep=FakeSleep(),
        )


def test_run_init_false_skips_init():
    """Tests sometimes want to construct without running init (e.g.
    to verify show() in isolation). run_init=False supports that."""
    device, spi, dc, rst, bl, sleep = _make_device(run_init=False)
    # No transfers should have been recorded.
    assert spi.transfers == []
    # No RST pulse either.
    assert rst.transitions == []


# ── open_display() factory (display.py) ─────────────────────────────


def test_open_display_prefers_fbdev_when_available(tmp_path, monkeypatch):
    """If /proc/fb lists 'fb_st7789v', open_display must return a
    DisplayDevice — NOT the SPI fallback. The CardputerZero path
    must keep working."""
    from microjs8.ui.display import open_display
    from microjs8.ui import display as display_mod

    # Fake /proc/fb that lists fb_st7789v at index 1.
    proc_fb = tmp_path / "proc_fb"
    proc_fb.write_text("0 vc4drmfb\n1 fb_st7789v\n")

    # Fake sysfs with the required attrs.
    sysfs = tmp_path / "graphics"
    sysfs.mkdir()
    fb1_dir = sysfs / "fb1"
    fb1_dir.mkdir()
    (fb1_dir / "virtual_size").write_text("320,170\n")
    (fb1_dir / "stride").write_text("640\n")
    (fb1_dir / "bits_per_pixel").write_text("16\n")

    # Patch DisplayDevice.open to return a Mock with the .info shape
    # that the log statement inside open_display() reads.
    from unittest.mock import MagicMock
    fake_device = MagicMock()
    fake_device.info.index = 1
    fake_device.info.name = "fb_st7789v"
    with patch.object(
        display_mod.DisplayDevice, "open", return_value=fake_device,
    ) as mock_open:
        result = open_display(proc_fb=proc_fb, sysfs_root=sysfs)
        assert result is fake_device
        mock_open.assert_called_once()


def test_open_display_falls_back_to_spi(tmp_path, monkeypatch):
    """When fbdev discovery fails AND /dev/spidev0.0 exists,
    open_display should construct an SpiDisplayDevice."""
    from microjs8.ui.display import open_display

    # Empty /proc/fb (no st7789v)
    proc_fb = tmp_path / "proc_fb"
    proc_fb.write_text("0 vc4drmfb\n")

    sysfs = tmp_path / "graphics"; sysfs.mkdir()

    spidev_path = tmp_path / "spidev0.0"
    spidev_path.write_text("")  # exists as a regular file is fine for the check

    # Patch SpiDisplayDevice.open to return a sentinel
    sentinel = object()
    with patch(
        "microjs8.ui.display_spi.SpiDisplayDevice.open",
        return_value=sentinel,
    ) as mock_spi_open:
        result = open_display(
            proc_fb=proc_fb, sysfs_root=sysfs,
            spi_device_path=spidev_path,
        )
        assert result is sentinel
        mock_spi_open.assert_called_once()


def test_open_display_raises_when_nothing_available(tmp_path):
    """If neither fbdev nor /dev/spidev0.0 exists, open_display
    should raise with a helpful message pointing the operator at
    the resolution steps."""
    from microjs8.ui.display import open_display

    proc_fb = tmp_path / "proc_fb"
    proc_fb.write_text("0 vc4drmfb\n")
    sysfs = tmp_path / "graphics"; sysfs.mkdir()
    nonexistent_spi = tmp_path / "nope"

    with pytest.raises(RuntimeError) as exc_info:
        open_display(
            proc_fb=proc_fb, sysfs_root=sysfs,
            spi_device_path=nonexistent_spi,
        )
    # Error message should mention both options so the operator
    # knows what to fix.
    msg = str(exc_info.value)
    assert "microjs8-enable-display" in msg or "SPI" in msg
