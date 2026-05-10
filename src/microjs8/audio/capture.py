"""USB audio capture for JS8 reception.

Mirrors the proven Step-0 prototype (``js8_stream.py``):

  - Open the QDX's USB sound card at 48 000 Hz mono int16.
  - sounddevice fires its callback in ~100 ms chunks (4800 samples).
  - In the callback, decimate 4:1 (`audio[::4]`) to 12 000 Hz —
    JS8's required input rate. Aliasing is harmless because JS8
    audio is well below 6 kHz Nyquist.
  - Write the decimated audio into a 60-second numpy ring buffer
    (720 000 samples) under a threading.Lock.
  - The decode thread reads a snapshot of the buffer once per
    15-second JS8 slot boundary.

Why not 12 kHz capture: USB audio devices uniformly refuse non-standard
rates (44.1k, 48k, 96k are universal; 12k is not). PortAudio falls
back to 48 kHz anyway, so we just admit it and decimate ourselves.

The capture thread fails loudly if the device disappears or refuses
the requested format — no silent fallback to reduced rates. JS8
decodes are unforgiving: garbage in == no decodes.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

_log = logging.getLogger(__name__)

# JS8 protocol constants (mirror gfsk8.RX_SAMPLE_RATE / SAMPLE_SIZE)
RX_SAMPLE_RATE = 12_000
RX_BUFFER_SECONDS = 60
RX_BUFFER_SIZE = RX_SAMPLE_RATE * RX_BUFFER_SECONDS  # 720_000

# Capture rate we open the device at.
CAPTURE_SAMPLE_RATE = 48_000
CAPTURE_DECIMATION = CAPTURE_SAMPLE_RATE // RX_SAMPLE_RATE  # 4

# sounddevice block size (frames per callback) at the capture rate.
# ~100 ms blocks; sounddevice handles the actual scheduling, but a
# block size hint reduces callback overhead.
_CAPTURE_BLOCKSIZE = 4800  # 100 ms @ 48 kHz


class AudioCapture:
    """Captures audio from a USB sound card into a 12 kHz ring buffer.

    Construct with the sounddevice device index. Call ``start()`` to
    open the stream; the capture runs on PortAudio's internal thread
    (not one of ours). Call ``snapshot()`` from any thread to get a
    read-only copy of the most recent 60 s of decimated audio.
    """

    def __init__(self, device_index: int) -> None:
        self._device_index = device_index
        # Ring buffer: 60 s of int16 at 12 kHz, lock-protected so the
        # capture callback can write while the decode thread reads.
        self._buffer = np.zeros(RX_BUFFER_SIZE, dtype=np.int16)
        self._write_pos = 0
        self._lock = threading.Lock()
        self._stream = None  # sounddevice.InputStream once started
        self._samples_seen = 0  # rolling counter for diagnostics

    def start(self) -> None:
        """Open the sounddevice stream. Raises if device refuses 48 kHz."""
        # Lazy import so host-side tests don't need sounddevice/PortAudio.
        import sounddevice as sd  # type: ignore[import-not-found]

        self._stream = sd.InputStream(
            device=self._device_index,
            samplerate=CAPTURE_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=_CAPTURE_BLOCKSIZE,
            callback=self._on_audio,
        )
        try:
            self._stream.start()
        except Exception:
            self._stream = None
            raise
        _log.info(
            "audio capture started: device=%d, %d Hz mono int16, %d-sample blocks",
            self._device_index, CAPTURE_SAMPLE_RATE, _CAPTURE_BLOCKSIZE,
        )

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                _log.exception("error closing audio stream")
            self._stream = None

    # ── Callback (PortAudio thread) ──────────────────────────────────

    def _on_audio(self, indata, frames, _time_info, status) -> None:
        """sounddevice callback. Runs on PortAudio's internal thread.

        ``indata`` is a (frames, channels) numpy array of int16 at
        48 kHz. We flatten to mono if needed and decimate 4:1.

        IMPORTANT: This must NEVER block. Any raise here corrupts the
        audio stream silently. We catch and log everything.
        """
        try:
            if status:
                # Underrun / overrun — log at debug level since they
                # can happen briefly during USB hot-plug or system
                # contention. Real glitches will show up as missed
                # decodes, not as crashes here.
                _log.debug("audio callback status: %s", status)

            # Flatten to mono (the device is opened with channels=1
            # so this should already be 1-D, but defensive).
            if indata.ndim > 1:
                samples = indata[:, 0]
            else:
                samples = indata
            # Decimate 4:1. Aliasing is harmless because JS8 audio is
            # below 6 kHz, well below the 6 kHz Nyquist of the
            # decimated rate.
            decimated = samples[::CAPTURE_DECIMATION]
            self._write_into_ring(decimated)
        except Exception:
            _log.exception("audio callback raised; dropped this block")

    def _write_into_ring(self, samples: np.ndarray) -> None:
        n = len(samples)
        if n == 0:
            return
        with self._lock:
            end = self._write_pos + n
            if end <= RX_BUFFER_SIZE:
                self._buffer[self._write_pos:end] = samples
            else:
                # Wrap.
                first = RX_BUFFER_SIZE - self._write_pos
                self._buffer[self._write_pos:] = samples[:first]
                self._buffer[: n - first] = samples[first:]
            self._write_pos = (self._write_pos + n) % RX_BUFFER_SIZE
            self._samples_seen += n

    # ── Read API (any thread) ────────────────────────────────────────

    def snapshot(self) -> np.ndarray:
        """Return a read-only copy of the current 60 s ring buffer.

        The buffer is returned in chronological order (oldest sample
        first, newest last) — so consumers can index it like a normal
        time series.
        """
        with self._lock:
            # Roll the ring so the oldest sample is at index 0.
            return np.roll(self._buffer, -self._write_pos).copy()

    def samples_seen(self) -> int:
        """Total samples received since start (for diagnostics)."""
        return self._samples_seen
