"""Tests for the audio decimation logic.

The capture thread receives 48 kHz int16 from sounddevice and writes
12 kHz int16 into a ring buffer via ``[::4]`` indexing. We don't open
real PortAudio in tests; instead we exercise the ring-write path
directly with synthetic input.
"""

from __future__ import annotations

import numpy as np
import pytest

from minijs8.audio.capture import (
    AudioCapture,
    CAPTURE_DECIMATION,
    CAPTURE_SAMPLE_RATE,
    RX_BUFFER_SIZE,
    RX_SAMPLE_RATE,
)


def test_decimation_ratio_correct():
    """48 kHz capture / 12 kHz output = 4:1 decimation."""
    assert CAPTURE_SAMPLE_RATE == 48_000
    assert RX_SAMPLE_RATE == 12_000
    assert CAPTURE_DECIMATION == 4


def test_buffer_size_60_seconds_at_12khz():
    assert RX_BUFFER_SIZE == 60 * RX_SAMPLE_RATE


def test_decimation_keeps_every_fourth_sample():
    """Verify the [::4] decimation in isolation, exactly as the
    capture callback does it."""
    src = np.arange(48, dtype=np.int16)
    decimated = src[::CAPTURE_DECIMATION]
    assert len(decimated) == 12
    np.testing.assert_array_equal(decimated, [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44])


def test_capture_constructed_does_not_open_device():
    """Construction should not touch hardware. Open is in start()."""
    cap = AudioCapture(device_index=99)
    # No sounddevice calls happened, no PortAudio init. snapshot()
    # returns the freshly-allocated zero buffer.
    snap = cap.snapshot()
    assert len(snap) == RX_BUFFER_SIZE
    np.testing.assert_array_equal(snap, np.zeros(RX_BUFFER_SIZE, dtype=np.int16))


def test_ring_buffer_write_advances_pos():
    """Manually invoke the internal write path; verify wrap behavior."""
    cap = AudioCapture(device_index=99)
    # Write 100 samples — should land at positions 0-99 after decimation.
    # We bypass the callback (no sounddevice) and call _write_into_ring
    # directly with already-decimated audio.
    cap._write_into_ring(np.arange(100, dtype=np.int16))
    assert cap._write_pos == 100
    snap = cap.snapshot()
    # Snapshot rolls so newest is at the END. The first 100 we wrote
    # are the newest, so they're at the tail.
    np.testing.assert_array_equal(snap[-100:], np.arange(100, dtype=np.int16))


def test_ring_buffer_wraps_at_boundary():
    """A write that crosses the buffer end must wrap correctly."""
    cap = AudioCapture(device_index=99)
    # Set position to 5 samples before the end.
    cap._write_pos = RX_BUFFER_SIZE - 5
    # Write 10 samples — first 5 fill to end, next 5 wrap to start.
    cap._write_into_ring(np.arange(10, dtype=np.int16))
    assert cap._write_pos == 5
    # Underlying buffer: end has 0..4, start has 5..9.
    assert cap._buffer[RX_BUFFER_SIZE - 1] == 4
    assert cap._buffer[0] == 5
    assert cap._buffer[4] == 9


def test_snapshot_oldest_first():
    """Snapshot must return audio in chronological order."""
    cap = AudioCapture(device_index=99)
    # Fill the ring with a value that depends on insertion order.
    cap._write_into_ring(np.full(100, 1, dtype=np.int16))
    cap._write_into_ring(np.full(100, 2, dtype=np.int16))
    snap = cap.snapshot()
    # First 100 of the writes (the older ones, value=1) should appear
    # BEFORE the newer ones (value=2) in the snapshot.
    # The buffer is mostly zeros plus 200 written samples at the tail.
    assert snap[-200:-100].tolist() == [1] * 100
    assert snap[-100:].tolist() == [2] * 100
