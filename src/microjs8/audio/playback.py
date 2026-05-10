"""Audio playback for transmit — JS8Call-equivalent callback architecture.

PortAudio is driven in **callback (pull) mode**, mirroring how
JS8Call's ``Modulator`` feeds samples to Qt's ``QAudioOutput`` via
``readData()``. The audio device pulls samples from us when it
needs them, at exactly the device clock rate. This is what eliminates
push-mode buffer-state uncertainty — the same problem that made our
on-air timing arrive 400-625 ms late and varied between frames.

JS8Call's per-frame lifecycle (from ``soundout.cpp::restart()`` and
``Modulator::start()``):

  1. Caller decides to TX a frame
  2. Modulator::start() configures m_silentFrames and arms the state
  3. SoundOutput::restart() calls QAudioOutput::start(modulator)
  4. QAudioOutput's audio thread calls Modulator::readData() until
     the modulator signals state=Idle (all symbols emitted)
  5. The stream auto-stops; PTT stays keyed for next frame in burst

We mirror this with PortAudio:

  1. Caller invokes play_frame(silence_samples, modulation_samples)
  2. We set internal state: silence-remaining + modulation buffer + cursor
  3. We call stream.start() — PortAudio's audio thread begins calling
     our callback at the device's natural rate
  4. The callback emits silence_samples zeros, then modulation samples
  5. When all modulation samples have been emitted, callback sets a
     "done" event
  6. play_frame() waits on the done event, then calls stream.stop()
  7. PTT stays keyed across multiple play_frame() calls in a burst

Per-frame stream.start()/stop() means we get a fresh, predictable
audio start every frame. No leftover buffer state from previous
frames. The ~ms-scale latency between stream.start() and "first DAC
sample emerges" applies CONSISTENTLY to every frame, so a single
per-radio offset (radios.py: ``tx_pipeline_latency_ms``) corrects for
the entire pipeline.

Why pull-mode (callback) over push-mode (write):
  - Push mode (sd.OutputStream.write) buffers indefinitely behind
    whatever's currently in the queue; we don't know when our data
    will actually leave the DAC
  - Pull mode lets the device clock drive sample emission, giving us
    sample-accurate timing relative to stream.start()
  - This matches JS8Call exactly — they use Qt's pull-mode, we use
    PortAudio's pull-mode

Sample rates: we resample 12 kHz (encoder output) → 48 kHz (USB sound
card rate). The 12→48 polyphase filter is unchanged from prior
versions; that part was solid. What changed is how the resampled
samples reach the device.

Threading model:

  - Main thread: calls play_frame(), blocks until frame done
  - PortAudio thread: calls our callback at real-time priority

The callback runs at real-time priority. Per the sounddevice docs,
**no allocations, no logging, no I/O, no Python features that might
trigger GC.** Numpy buffers used by the callback are pre-allocated
and assigned-to in place. The lock is held for microseconds,
protecting only state updates — never any heavy work.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

_log = logging.getLogger(__name__)

# ── Sample rates ────────────────────────────────────────────────────

# Source rate from the encoder (gfsk8 modulator emits at 12 kHz).
TX_SOURCE_RATE = 12_000

# Output rate to the USB sound card. The QDX (per its manual and per
# bench testing) is a 48 kHz stereo USB sound card. PortAudio uniformly
# demands 44.1 / 48 / 96 kHz on USB sound cards — 12 kHz is not a
# native rate. We resample 12 → 48 with a proper anti-aliasing filter
# (NOT sample-and-hold, which fails QDX audio detection).
TX_OUTPUT_RATE = 48_000

# Upsampling factor.
TX_UPSAMPLE_FACTOR = TX_OUTPUT_RATE // TX_SOURCE_RATE  # 4

# Output channel count. The QDX presents itself as a STEREO USB sound
# card and its audio-detect firmware only sees content on a real
# stereo stream — opening as 1-channel mono results in the QDX
# flashing its "PTT active, no audio" pattern even when we're writing
# samples successfully. We open as stereo and duplicate mono modulation
# into both L+R channels.
TX_OUTPUT_CHANNELS = 2

# PortAudio callback blocksize. Smaller = lower latency (callback
# fires more often, reacts to state changes faster) but higher CPU
# overhead. 1024 frames at 48 kHz is ~21 ms — small enough that
# sample-level accuracy of "when does modulation start within the
# block" is preserved (worst case, modulation starts 1 sample late
# if silence_samples lands mid-block, but we handle that case).
#
# Pi Zero 2W can comfortably handle ~50 callbacks/sec with our trivial
# callback work; this is well within budget.
_CALLBACK_BLOCKSIZE = 1024


# ── Polyphase resample filter (UNCHANGED from prior versions) ───────
# 12 kHz → 48 kHz upsampling with a windowed-sinc anti-aliasing
# low-pass. The filter is precomputed at module import — never
# changes — so each TX cycle just runs a fast convolution.
#
# Filter design:
#   - 257 taps (64 × upsample_factor + 1), long enough for clean
#     stop-band rejection
#   - Cutoff at original Nyquist (6 kHz, = 0.125 of new fs)
#   - Hamming window — good roll-off, manageable ringing
#   - DC-normalized so amplitude is preserved
#
# We built this in pure NumPy (rather than scipy.signal.resample_poly)
# to avoid pulling in scipy as a runtime dep on the Pi Zero 2W.

def _build_resample_filter(
    upsample_factor: int = TX_UPSAMPLE_FACTOR,
    n_taps: int = 257,
) -> np.ndarray:
    """Build a windowed-sinc low-pass filter for polyphase upsampling.

    Returns a float64 1-D filter kernel suitable for ``np.convolve``.
    """
    cutoff = 0.5 / upsample_factor   # normalized to new fs
    idx = np.arange(n_taps) - (n_taps - 1) / 2
    sinc = np.sinc(2.0 * cutoff * idx)
    window = np.hamming(n_taps)
    h = sinc * window
    h = h / h.sum()  # DC gain = 1
    return h.astype(np.float64)


# Precomputed at import time; never changes.
_RESAMPLE_FILTER = _build_resample_filter()


def resample_12k_to_48k(samples_12khz: np.ndarray) -> np.ndarray:
    """Resample 12 kHz int16 audio to 48 kHz int16 with proper AA filter.

    Public so transmit_frame() can resample modulation once and pass
    the 48 kHz buffer directly to play_frame() — no point resampling
    zeros for the silence prefix.

    Returns int16 mono at 48 kHz.
    """
    if not isinstance(samples_12khz, np.ndarray):
        raise PlaybackError(
            f"samples must be numpy array, got {type(samples_12khz).__name__}"
        )
    if samples_12khz.dtype != np.int16:
        raise PlaybackError(
            f"samples must be int16, got dtype={samples_12khz.dtype}"
        )

    target_len = len(samples_12khz) * TX_UPSAMPLE_FACTOR
    # Zero-stuff: place each input sample at every 4th output position.
    # Multiply by upsample_factor to compensate for energy spread
    # across the new samples. (The filter normalizes DC gain back to 1.)
    upsampled = np.zeros(target_len, dtype=np.float64)
    upsampled[::TX_UPSAMPLE_FACTOR] = (
        samples_12khz.astype(np.float64) * TX_UPSAMPLE_FACTOR
    )
    full = np.convolve(upsampled, _RESAMPLE_FILTER, mode="full")
    delay = (len(_RESAMPLE_FILTER) - 1) // 2
    filtered = full[delay : delay + target_len]
    return np.clip(filtered, -32767, 32767).astype(np.int16)


# ── Errors ──────────────────────────────────────────────────────────


class PlaybackError(Exception):
    """Raised when audio playback fails."""


# ── AudioPlayback (callback architecture) ──────────────────────────


class AudioPlayback:
    """Callback-driven output to the QDX's USB sound card.

    Lifecycle:
      1. ``start()`` — open the OutputStream in callback mode (does NOT
         begin playback; stream is opened-but-stopped)
      2. ``play_frame(silence_samples_48k, modulation_48k)`` — set
         per-frame state, stream.start(), block until callback signals
         done, stream.stop(). Per-frame: fresh stream start each time.
      3. ``stop()`` — close the stream (idempotent, called at shutdown)

    The keep-stream-open-but-start/stop-per-frame approach matches
    JS8Call's ``m_stream->restart(this)`` pattern: the audio device
    is opened once, but ``QAudioOutput::start()`` is called per
    transmission and naturally stops when the source signals Idle.

    Thread safety: ``play_frame()`` is called from the application
    thread (typically the TX worker). The callback runs on PortAudio's
    audio thread. State is protected by ``_state_lock`` — held only
    for microseconds, never during heavy work.
    """

    def __init__(self, device_index: int) -> None:
        self._device_index = device_index
        self._stream = None  # sounddevice.OutputStream, set in start()

        # Per-frame state, set by play_frame() before stream.start():
        self._state_lock = threading.Lock()
        self._silence_remaining: int = 0   # samples-of-zeros still to emit
        self._modulation: Optional[np.ndarray] = None  # int16 mono @ 48k
        self._modulation_cursor: int = 0
        self._frame_done = threading.Event()

        # Diagnostics: count of samples played since start().
        self._samples_played = 0

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Open the output stream in callback mode. Does NOT begin
        playback — stream is opened-but-stopped, ready for play_frame.

        Raises PlaybackError on device failure.
        """
        # Lazy import so host-side tests don't need PortAudio.
        import sounddevice as sd  # type: ignore[import-not-found]

        try:
            self._stream = sd.OutputStream(
                device=self._device_index,
                samplerate=TX_OUTPUT_RATE,
                channels=TX_OUTPUT_CHANNELS,
                dtype="int16",
                blocksize=_CALLBACK_BLOCKSIZE,
                callback=self._audio_callback,
            )
            # Note: we deliberately do NOT call stream.start() here.
            # The stream stays opened-but-stopped between bursts.
            # play_frame() starts/stops it per frame, matching
            # JS8Call's ``m_stream->restart(this)`` per-frame pattern.
        except Exception as exc:
            self._stream = None
            raise PlaybackError(
                f"could not open output stream on device "
                f"{self._device_index}: {exc}"
            ) from exc

        _log.info(
            "audio playback opened: device=%d, %d Hz %dch int16, "
            "blocksize=%d (callback mode)",
            self._device_index, TX_OUTPUT_RATE, TX_OUTPUT_CHANNELS,
            _CALLBACK_BLOCKSIZE,
        )

    def stop(self) -> None:
        """Close the stream. Idempotent. Safe to call any time.

        Used at daemon shutdown. Per-frame stop is handled inside
        play_frame() — callers don't need to invoke stop() between
        frames.
        """
        with self._state_lock:
            if self._stream is None:
                return
            stream = self._stream
            self._stream = None
        try:
            # If a callback is mid-execution, abort() returns faster
            # than stop() (which waits for the current callback to
            # complete) — preferred at shutdown so the daemon doesn't
            # hang.
            stream.abort()
            stream.close()
        except Exception:
            _log.exception("error closing playback stream")

    def is_open(self) -> bool:
        """True if the stream is open and ready for play_frame().

        Used by callers (and tests) to verify the lifecycle.
        """
        return self._stream is not None

    # ── Per-frame playback ──────────────────────────────────────────

    def play_frame(
        self,
        silence_samples_48k: int,
        modulation_48k: np.ndarray,
        timeout_s: float = 30.0,
    ) -> None:
        """Synchronously emit silence-prefix + modulation through stream.

        ``silence_samples_48k`` — number of zero samples to emit BEFORE
        the modulation. Computed by tx_backend.transmit_frame() using
        the JS8Call alignment formula. Always >= 0; if 0, modulation
        starts immediately.

        ``modulation_48k`` — int16 mono modulation samples at 48 kHz.
        Will be duplicated to L+R inside the callback. Caller is
        responsible for resampling 12 kHz → 48 kHz (use
        ``resample_12k_to_48k()``).

        ``timeout_s`` — fail-safe upper bound on how long play_frame
        will block. A normal frame (~13 s of audio) plays in ~13 s of
        wall clock. The default 30 s gives generous margin.

        Blocks until the callback has emitted all modulation samples,
        then stops the stream (so the next frame starts fresh).

        Raises PlaybackError if the stream isn't open, the modulation
        type is wrong, or the callback fails to signal done within
        ``timeout_s``.
        """
        if self._stream is None:
            raise PlaybackError("playback stream not open")
        if not isinstance(modulation_48k, np.ndarray):
            raise PlaybackError(
                f"modulation must be numpy array, "
                f"got {type(modulation_48k).__name__}"
            )
        if modulation_48k.dtype != np.int16:
            raise PlaybackError(
                f"modulation must be int16, got dtype={modulation_48k.dtype}"
            )
        if modulation_48k.ndim != 1:
            raise PlaybackError(
                f"modulation must be 1-D mono, "
                f"got shape={modulation_48k.shape}"
            )
        if silence_samples_48k < 0:
            raise PlaybackError(
                f"silence_samples_48k must be >= 0, "
                f"got {silence_samples_48k}"
            )

        # Set up per-frame state. Hold the lock just long enough to
        # set the four fields atomically — the callback may be poised
        # to read them as soon as we call stream.start().
        with self._state_lock:
            self._silence_remaining = silence_samples_48k
            self._modulation = modulation_48k
            self._modulation_cursor = 0
            self._frame_done.clear()

        # Fire the stream. The PortAudio audio thread starts calling
        # _audio_callback at the device clock rate. The callback emits
        # silence, then modulation, then sets _frame_done.
        try:
            self._stream.start()
        except Exception as exc:
            raise PlaybackError(f"stream.start() failed: {exc}") from exc

        try:
            # Wait for the callback to finish emitting all modulation.
            # A normal frame is ~13 s; we use a generous timeout.
            if not self._frame_done.wait(timeout=timeout_s):
                raise PlaybackError(
                    f"frame timed out after {timeout_s}s "
                    f"(callback didn't signal done)"
                )
        finally:
            # Always stop the stream so the next frame starts fresh.
            # stop() drains any in-progress callback (graceful) which
            # is what we want — better than abort() which would
            # discard the trailing modulation samples.
            try:
                self._stream.stop()
            except Exception:
                _log.exception("stream.stop() raised after play_frame")

        self._samples_played += silence_samples_48k + len(modulation_48k)

        _log.debug(
            "play_frame done: %d silence + %d modulation samples "
            "(%.2f s @ %d Hz)",
            silence_samples_48k, len(modulation_48k),
            (silence_samples_48k + len(modulation_48k)) / TX_OUTPUT_RATE,
            TX_OUTPUT_RATE,
        )

    def samples_played(self) -> int:
        """Total samples emitted since start() (for diagnostics)."""
        return self._samples_played

    # ── PortAudio callback ──────────────────────────────────────────

    def _audio_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info,  # PaTime struct; we don't use it
        status,     # CallbackFlags; logged on underflow
    ) -> None:
        """Called by PortAudio's audio thread when it needs samples.

        Real-time priority — keep this FAST. No allocations, no
        logging in the hot path, no Python features that may trigger
        GC pauses.

        ``outdata`` is shape ``(frames, TX_OUTPUT_CHANNELS)`` int16,
        owned by PortAudio. We must fill it completely.
        """
        # Status warnings are rare but useful when they do happen.
        # Logging IS technically not allowed in the callback per
        # sounddevice docs, but a single status warning per
        # underflow is far less harmful than NOT seeing it at all.
        if status:
            _log.warning("audio callback status: %s", status)

        # Acquire state lock. Held for microseconds — only field
        # reads/updates, no I/O.
        with self._state_lock:
            silence_remaining = self._silence_remaining
            modulation = self._modulation
            cursor = self._modulation_cursor

            # If no frame is armed (modulation is None) — silence.
            # This can happen between play_frame() calls when the
            # stream hasn't been stopped yet but the previous frame
            # has completed. Defensive: shouldn't occur in normal
            # flow because play_frame() stops the stream.
            if modulation is None:
                outdata[:] = 0
                return

            # Phase 1: emit silence prefix.
            i = 0
            if silence_remaining > 0:
                n_silence = min(frames, silence_remaining)
                outdata[i:i + n_silence, :] = 0
                self._silence_remaining = silence_remaining - n_silence
                i += n_silence

            # Phase 2: emit modulation samples.
            if i < frames:
                mod_remaining = len(modulation) - cursor
                n_mod = min(frames - i, mod_remaining)
                if n_mod > 0:
                    # Copy modulation into both stereo channels.
                    # Slice assignment to outdata is in-place — no
                    # new allocation. The QDX firmware needs audio
                    # on a real stereo stream.
                    outdata[i:i + n_mod, 0] = modulation[
                        cursor:cursor + n_mod
                    ]
                    outdata[i:i + n_mod, 1] = outdata[i:i + n_mod, 0]
                    self._modulation_cursor = cursor + n_mod
                    i += n_mod

            # Phase 3: trailing silence + done detection.
            # If we ran out of modulation mid-block, fill the rest
            # of the block with zeros and signal done.
            if i < frames:
                outdata[i:, :] = 0
                # Frame complete? (cursor advanced past end of buffer)
                if self._modulation_cursor >= len(modulation):
                    # Clear modulation reference so subsequent
                    # callbacks (if any fire before stream.stop()
                    # takes effect) emit only silence. _frame_done
                    # being set is what play_frame() actually waits on.
                    self._modulation = None
                    self._frame_done.set()
