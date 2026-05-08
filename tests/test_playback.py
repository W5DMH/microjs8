"""Tests for minijs8.audio.playback.AudioPlayback (callback mode).

Stub sounddevice so we can exercise lifecycle, the resample function,
the input-validation path, and the audio callback itself without
opening real PortAudio. The audio callback is normally invoked by
PortAudio's audio thread; in tests we capture the registered callable
and invoke it directly from the test thread to verify its behavior.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import numpy as np
import pytest

from minijs8.audio.playback import (
    AudioPlayback,
    PlaybackError,
    TX_OUTPUT_CHANNELS,
    TX_OUTPUT_RATE,
    TX_SOURCE_RATE,
    TX_UPSAMPLE_FACTOR,
    resample_12k_to_48k,
)


# ── Fake sounddevice ────────────────────────────────────────────────


@pytest.fixture
def fake_sd(monkeypatch):
    """Stub sounddevice with a recordable spy.

    The spy:
      - Captures construction kwargs so tests can verify channels=2,
        callback mode, blocksize, sample rate, etc.
      - Tracks start()/stop()/abort()/close() calls.
      - Captures the registered callback so tests can drive it
        directly to verify its silence/modulation emission behavior.
      - Optionally raises on open/start to exercise error paths.
    """
    state: dict[str, Any] = {
        "started_count": 0,
        "stopped_count": 0,
        "closed": False,
        "raise_on_open": None,
        "raise_on_start": None,
        "callback": None,
        "blocksize": None,
    }
    last_kwargs: dict = {}

    class _FakeStream:
        def __init__(self, **kwargs):
            last_kwargs.clear()
            last_kwargs.update(kwargs)
            self.kwargs = kwargs
            if state["raise_on_open"]:
                raise state["raise_on_open"]
            state["callback"] = kwargs.get("callback")
            state["blocksize"] = kwargs.get("blocksize")

        def start(self):
            if state["raise_on_start"]:
                raise state["raise_on_start"]
            state["started_count"] += 1

        def stop(self):
            state["stopped_count"] += 1

        def abort(self):
            pass

        def close(self):
            state["closed"] = True

    class _FakeSd:
        OutputStream = _FakeStream

    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSd)
    return {"state": state, "last_kwargs": last_kwargs}


# ── Constants ───────────────────────────────────────────────────────


def test_upsample_factor_is_4():
    assert TX_UPSAMPLE_FACTOR == 4
    assert TX_SOURCE_RATE * TX_UPSAMPLE_FACTOR == TX_OUTPUT_RATE


# ── Lifecycle ───────────────────────────────────────────────────────


def test_construct_does_not_open_device(fake_sd):
    """Construction is cheap; the device opens in start()."""
    AudioPlayback(device_index=99)
    assert fake_sd["state"]["started_count"] == 0


def test_start_opens_stream_in_callback_mode(fake_sd):
    """start() opens the stream in callback mode but does NOT call
    stream.start() — the stream stays opened-but-stopped between
    bursts. play_frame() does the per-frame start/stop dance."""
    p = AudioPlayback(device_index=99)
    p.start()
    # Stream was constructed (callback registered, kwargs captured).
    assert fake_sd["state"]["callback"] is not None, (
        "AudioPlayback.start() did NOT register a callback — "
        "we need callback mode for JS8Call-equivalent timing"
    )
    # Stream was NOT started — start happens per-frame in play_frame().
    assert fake_sd["state"]["started_count"] == 0
    assert p.is_open()


def test_start_stream_constructed_with_correct_format(fake_sd):
    """OutputStream must be channels=2 (QDX needs stereo to detect
    audio), 48 kHz, int16 — and a small blocksize for low-latency
    callback firing."""
    p = AudioPlayback(device_index=99)
    p.start()
    kw = fake_sd["last_kwargs"]
    assert kw["channels"] == 2
    assert kw["samplerate"] == TX_OUTPUT_RATE
    assert kw["dtype"] == "int16"
    # Blocksize should be small enough to react to state changes
    # within a few ms but big enough to amortize callback overhead.
    assert kw["blocksize"] in range(256, 4097)
    # callback must be a callable.
    assert callable(kw["callback"])


def test_start_failure_raises_PlaybackError(fake_sd):
    fake_sd["state"]["raise_on_open"] = OSError("no such device")
    p = AudioPlayback(device_index=99)
    with pytest.raises(PlaybackError, match="could not open"):
        p.start()
    assert not p.is_open()


def test_stop_idempotent(fake_sd):
    """Multiple stops are safe — used at daemon shutdown."""
    p = AudioPlayback(device_index=99)
    p.start()
    p.stop()
    p.stop()  # second call must not raise
    assert fake_sd["state"]["closed"] is True
    assert not p.is_open()


def test_stop_without_start_is_safe(fake_sd):
    """Calling stop on an unopened playback should be a no-op."""
    p = AudioPlayback(device_index=99)
    p.stop()  # never started, must not crash


# ── play_frame: input validation ────────────────────────────────────


def test_play_frame_without_start_raises(fake_sd):
    p = AudioPlayback(device_index=99)
    with pytest.raises(PlaybackError, match="not open"):
        p.play_frame(0, np.zeros(10, dtype=np.int16))


def test_play_frame_rejects_non_numpy_modulation(fake_sd):
    p = AudioPlayback(device_index=99)
    p.start()
    with pytest.raises(PlaybackError, match="numpy array"):
        p.play_frame(0, [1, 2, 3])  # type: ignore[arg-type]


def test_play_frame_rejects_wrong_dtype(fake_sd):
    p = AudioPlayback(device_index=99)
    p.start()
    with pytest.raises(PlaybackError, match="int16"):
        p.play_frame(0, np.zeros(10, dtype=np.float32))


def test_play_frame_rejects_2d_modulation(fake_sd):
    """Modulation must be 1-D mono — playback duplicates to L+R."""
    p = AudioPlayback(device_index=99)
    p.start()
    with pytest.raises(PlaybackError, match="1-D"):
        p.play_frame(0, np.zeros((10, 2), dtype=np.int16))


def test_play_frame_rejects_negative_silence(fake_sd):
    p = AudioPlayback(device_index=99)
    p.start()
    with pytest.raises(PlaybackError, match=">="):
        p.play_frame(-1, np.zeros(10, dtype=np.int16))


# ── play_frame: stream lifecycle ────────────────────────────────────


def _drive_callback_to_completion(fake_sd, frame_count_per_block=1024):
    """Helper: run the registered callback in a background thread
    until it signals frame done (or a safety cap is hit). Mirrors what
    PortAudio's audio thread would do — but synchronously, in the test.
    """
    callback = fake_sd["state"]["callback"]
    blocksize = fake_sd["state"]["blocksize"] or frame_count_per_block

    # Pre-allocated output buffer the callback fills.
    outdata = np.zeros((blocksize, 2), dtype=np.int16)

    # Up to 5000 callbacks (well over 1 minute of audio at 48kHz/1024).
    # The callback will signal _frame_done partway through; we just
    # need to keep firing until that happens.
    for _ in range(5000):
        # Each callback is independent. Reset outdata's pretend
        # "ownership" — PortAudio passes a fresh buffer each time but
        # we reuse the same one in the test.
        outdata[:] = 0
        callback(outdata, blocksize, None, 0)
        # The callback sets _frame_done when the modulation cursor
        # reaches the end. We can't see that internal state directly
        # here; the play_frame() caller sees it via _frame_done.wait().


def test_play_frame_starts_and_stops_stream(fake_sd):
    """play_frame() must call stream.start() at the beginning and
    stream.stop() at the end (per-frame lifecycle, like JS8Call).
    """
    p = AudioPlayback(device_index=99)
    p.start()
    # Drive the callback in a background thread so play_frame() can
    # observe _frame_done being set.
    drive_thread = threading.Thread(
        target=_drive_callback_to_completion, args=(fake_sd,)
    )
    drive_thread.daemon = True

    # Schedule the start once play_frame() arms the state.
    # Because play_frame blocks on the done event, we need to drive
    # the callback after stream.start() but before the wait times out.
    # In production this happens automatically (PortAudio does it).
    # In tests we monkey-patch stream.start() to spawn the driver.
    real_start = fake_sd["state"]["callback"]  # just to confirm available

    state = fake_sd["state"]

    def starting_side_effect():
        state["started_count"] += 1
        # Spawn the driver thread now — this simulates PortAudio
        # beginning to call the callback after start() returns.
        drive_thread.start()

    # Patch the stream.start to also drive the callback.
    # The fake_sd fixture's _FakeStream class is hidden inside the
    # closure, but we can reach the actual stream object via
    # AudioPlayback._stream.
    p._stream.start = starting_side_effect  # type: ignore[method-assign]

    p.play_frame(48, np.zeros(96, dtype=np.int16))  # tiny frame
    drive_thread.join(timeout=1.0)

    # We started the stream (the side-effect ran) AND we stopped it
    # (the play_frame() finally-block).
    assert state["started_count"] == 1, (
        f"expected 1 stream.start() call, got {state['started_count']}"
    )
    assert state["stopped_count"] == 1, (
        f"expected 1 stream.stop() call, got {state['stopped_count']}"
    )


def test_play_frame_timeout_raises(fake_sd, monkeypatch):
    """If the callback never fires (PortAudio hung, device wedged),
    play_frame should fail with a clear timeout message instead of
    blocking the daemon forever."""
    p = AudioPlayback(device_index=99)
    p.start()
    # Use a very short timeout to keep the test fast.
    with pytest.raises(PlaybackError, match="timed out"):
        p.play_frame(0, np.zeros(10, dtype=np.int16), timeout_s=0.05)


def test_play_frame_stream_start_failure(fake_sd):
    """If stream.start() itself fails, play_frame should wrap the
    exception in PlaybackError."""
    p = AudioPlayback(device_index=99)
    p.start()
    # Inject a start failure.
    fake_sd["state"]["raise_on_start"] = RuntimeError("audio device dead")
    with pytest.raises(PlaybackError, match="stream.start"):
        p.play_frame(0, np.zeros(10, dtype=np.int16))


# ── The audio callback itself ───────────────────────────────────────


def test_callback_emits_silence_then_modulation(fake_sd):
    """Drive the callback directly to verify it emits N silence
    samples then the modulation samples."""
    p = AudioPlayback(device_index=99)
    p.start()
    callback = fake_sd["state"]["callback"]

    # Arm the playback state via the public API: schedule silence + mod.
    # We can't call play_frame() because it blocks on the callback,
    # but we can reach into the state directly the same way play_frame
    # does. (The lock is defined inside the class.)
    silence_n = 10
    modulation = np.array([100, 200, 300, 400], dtype=np.int16)
    with p._state_lock:
        p._silence_remaining = silence_n
        p._modulation = modulation
        p._modulation_cursor = 0
        p._frame_done.clear()

    # Big block — bigger than silence + modulation total — so we see
    # the full sequence in one callback.
    block_frames = 32
    outdata = np.zeros((block_frames, 2), dtype=np.int16)
    callback(outdata, block_frames, None, 0)

    # First 10 frames should be silence (zeros).
    assert int(np.max(np.abs(outdata[:silence_n]))) == 0, (
        "leading samples should be silence"
    )
    # Next 4 frames should be the modulation values.
    np.testing.assert_array_equal(
        outdata[silence_n:silence_n + 4, 0],
        modulation,
    )
    # Both channels carry the same modulation (mono dup'd to L+R).
    np.testing.assert_array_equal(
        outdata[silence_n:silence_n + 4, 0],
        outdata[silence_n:silence_n + 4, 1],
    )
    # Trailing samples (after modulation runs out) should be silence.
    assert int(np.max(np.abs(outdata[silence_n + 4:]))) == 0
    # Frame done should be signaled.
    assert p._frame_done.is_set()


def test_callback_silence_spans_multiple_blocks(fake_sd):
    """If the silence prefix is longer than one callback block, it
    spans multiple blocks. Each block emits its share of silence."""
    p = AudioPlayback(device_index=99)
    p.start()
    callback = fake_sd["state"]["callback"]

    block_frames = 100
    silence_n = 250  # spans 3 blocks
    modulation = np.array([1, 2, 3], dtype=np.int16)
    with p._state_lock:
        p._silence_remaining = silence_n
        p._modulation = modulation
        p._modulation_cursor = 0
        p._frame_done.clear()

    # Block 1: 100 silence samples emitted
    outdata = np.zeros((block_frames, 2), dtype=np.int16)
    callback(outdata, block_frames, None, 0)
    assert int(np.max(np.abs(outdata))) == 0
    assert not p._frame_done.is_set()

    # Block 2: another 100 silence samples
    outdata[:] = 0
    callback(outdata, block_frames, None, 0)
    assert int(np.max(np.abs(outdata))) == 0
    assert not p._frame_done.is_set()

    # Block 3: 50 silence + 3 modulation + 47 trailing silence; done.
    outdata[:] = 0
    callback(outdata, block_frames, None, 0)
    assert int(np.max(np.abs(outdata[:50]))) == 0
    np.testing.assert_array_equal(outdata[50:53, 0], modulation)
    assert int(np.max(np.abs(outdata[53:]))) == 0
    assert p._frame_done.is_set()


def test_callback_modulation_spans_multiple_blocks(fake_sd):
    """If modulation is longer than one block, multiple callbacks
    advance the cursor through it."""
    p = AudioPlayback(device_index=99)
    p.start()
    callback = fake_sd["state"]["callback"]

    block_frames = 50
    modulation = np.arange(120, dtype=np.int16)  # 120 mod samples
    with p._state_lock:
        p._silence_remaining = 0  # no silence prefix
        p._modulation = modulation
        p._modulation_cursor = 0
        p._frame_done.clear()

    # Block 1: 50 mod samples (indices 0..49)
    outdata = np.zeros((block_frames, 2), dtype=np.int16)
    callback(outdata, block_frames, None, 0)
    np.testing.assert_array_equal(outdata[:, 0], modulation[0:50])
    assert not p._frame_done.is_set()

    # Block 2: next 50 mod samples (indices 50..99)
    outdata[:] = 0
    callback(outdata, block_frames, None, 0)
    np.testing.assert_array_equal(outdata[:, 0], modulation[50:100])
    assert not p._frame_done.is_set()

    # Block 3: last 20 mod samples + 30 trailing silence; done.
    outdata[:] = 0
    callback(outdata, block_frames, None, 0)
    np.testing.assert_array_equal(outdata[:20, 0], modulation[100:120])
    assert int(np.max(np.abs(outdata[20:]))) == 0
    assert p._frame_done.is_set()


def test_callback_with_no_armed_frame_emits_silence(fake_sd):
    """If the callback fires when no frame is armed (modulation=None),
    it should fill the block with zeros — defensive case for races
    between play_frame returning and stream.stop() taking effect."""
    p = AudioPlayback(device_index=99)
    p.start()
    callback = fake_sd["state"]["callback"]

    # No state armed — modulation stays None.
    outdata = np.full((50, 2), 999, dtype=np.int16)  # pre-fill with garbage
    callback(outdata, 50, None, 0)
    assert int(np.max(np.abs(outdata))) == 0


# ── resample_12k_to_48k ──────────────────────────────────────────────


def test_resample_changes_length_4x():
    """N input samples → 4N output samples."""
    src = np.arange(100, dtype=np.int16)
    out = resample_12k_to_48k(src)
    assert len(out) == 400
    assert out.dtype == np.int16


def test_resample_constant_input_preserves_dc():
    """A constant input should survive the polyphase filter at roughly
    the same amplitude (DC gain = 1)."""
    src = np.full(1000, 5000, dtype=np.int16)
    out = resample_12k_to_48k(src)
    # Skip transients; check steady-state mean.
    steady = out[500:3500]
    assert abs(float(np.mean(steady)) - 5000) < 100


def test_resample_rejects_non_array():
    with pytest.raises(PlaybackError, match="numpy array"):
        resample_12k_to_48k([1, 2, 3])  # type: ignore[arg-type]


def test_resample_rejects_wrong_dtype():
    with pytest.raises(PlaybackError, match="int16"):
        resample_12k_to_48k(np.zeros(10, dtype=np.float32))


# ── samples_played counter ──────────────────────────────────────────


def test_samples_played_starts_at_zero(fake_sd):
    p = AudioPlayback(device_index=99)
    assert p.samples_played() == 0
    p.start()
    assert p.samples_played() == 0
