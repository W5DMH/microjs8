"""Tests for microjs8.modem.encoder.

We don't need the real gfsk8 .so; we inject a stub via sys.modules so
tests can run on any host. The stub mimics the actual API surface
verified against the running module on the Pi:

    pack(mycall, mygrid, text, submode) -> List[TxFrame]
    modulate(submode, frame_type, payload, audio_freq_hz)
        -> numpy.ndarray[float32]
"""

from __future__ import annotations

import sys
from typing import Any

import numpy as np
import pytest

from microjs8.modem.encoder import (
    DEFAULT_AUDIO_FREQ_HZ,
    EncoderError,
    SUBMODE_FAST,
    SUBMODE_NORMAL,
    SUBMODE_SLOW,
    TX_LEVEL_FRAC,
    TX_SAMPLE_RATE,
    encode_message,
)


class _FakeTxFrame:
    """Mimics gfsk8.TxFrame's two-attribute interface."""

    def __init__(self, frame_type: int, payload: str) -> None:
        self.frame_type = frame_type
        self.payload = payload


@pytest.fixture
def fake_gfsk8(monkeypatch):
    """Stub the gfsk8 module so encode_message runs without the .so."""
    holder: dict[str, Any] = {
        "pack_frames": [_FakeTxFrame(frame_type=3, payload="3-C30787OkK8")],
        "pack_raise": None,
        # Default modulate output: 13.14s of float32 audio at moderate
        # amplitude. Real gfsk8 returns 157,680 samples (500ms silence
        # + 79 symbols × 1920 samples/symbol).
        "modulate_audio": np.concatenate([
            np.zeros(6000, dtype=np.float32),  # 500ms silence prefix
            np.full(151680, 0.5, dtype=np.float32),  # filler signal
        ]),
        "modulate_raise": None,
        "pack_calls": [],
        "modulate_calls": [],
    }

    class _FakeSubmode:
        def __init__(self, value: int):
            self.value = value
        def __eq__(self, other):
            return isinstance(other, _FakeSubmode) and self.value == other.value
        def __hash__(self):
            return hash(self.value)

    class _FakeGfsk8:
        Submode = _FakeSubmode
        RX_SAMPLE_RATE = 12_000

        @staticmethod
        def pack(mycall, mygrid, text, submode):
            holder["pack_calls"].append((mycall, mygrid, text, submode))
            if holder["pack_raise"] is not None:
                raise holder["pack_raise"]
            return holder["pack_frames"]

        @staticmethod
        def modulate(submode, frame_type, payload, audio_freq_hz):
            holder["modulate_calls"].append(
                (submode, frame_type, payload, audio_freq_hz)
            )
            if holder["modulate_raise"] is not None:
                raise holder["modulate_raise"]
            return holder["modulate_audio"]

    monkeypatch.setitem(sys.modules, "gfsk8", _FakeGfsk8)
    return holder


# ── Happy path ──────────────────────────────────────────────────────


def test_encode_returns_list_of_int16_buffers(fake_gfsk8):
    """encode_message always returns a list — length 1 for
    single-frame messages, more for multi-frame. Each entry is
    int16 audio."""
    audio_frames = encode_message(
        "K1ABC: @HB HEARTBEAT FN42",
        callsign="K1ABC", grid="FN42",
    )
    assert isinstance(audio_frames, list)
    assert len(audio_frames) == 1  # default fake_gfsk8 returns one frame
    samples = audio_frames[0]
    assert isinstance(samples, np.ndarray)
    assert samples.dtype == np.int16
    assert len(samples) > 0


def test_encode_passes_identity_to_pack(fake_gfsk8):
    encode_message(
        "K1ABC: @HB HEARTBEAT FN42",
        callsign="K1ABC", grid="FN42",
    )
    assert len(fake_gfsk8["pack_calls"]) == 1
    mycall, mygrid, text, _submode = fake_gfsk8["pack_calls"][0]
    assert mycall == "K1ABC"
    assert mygrid == "FN42"
    assert text == "K1ABC: @HB HEARTBEAT FN42"


def test_encode_default_submode_is_normal(fake_gfsk8):
    encode_message("HELLO", callsign="K1ABC", grid="FN42")
    _, _, _, submode = fake_gfsk8["pack_calls"][0]
    assert submode.value == SUBMODE_NORMAL


def test_encode_passes_other_submodes(fake_gfsk8):
    encode_message("HELLO", callsign="K1ABC", grid="FN42", submode=SUBMODE_FAST)
    _, _, _, submode = fake_gfsk8["pack_calls"][0]
    assert submode.value == SUBMODE_FAST


def test_encode_default_audio_frequency(fake_gfsk8):
    encode_message("HELLO", callsign="K1ABC", grid="FN42")
    _, _, _, audio_hz = fake_gfsk8["modulate_calls"][0]
    assert audio_hz == DEFAULT_AUDIO_FREQ_HZ


def test_encode_passes_audio_frequency(fake_gfsk8):
    encode_message(
        "HELLO", callsign="K1ABC", grid="FN42", audio_freq_hz=2000.0,
    )
    _, _, _, audio_hz = fake_gfsk8["modulate_calls"][0]
    assert audio_hz == 2000.0


def test_encode_passes_payload_to_modulate(fake_gfsk8):
    """Verify the pack→modulate handoff: payload from pack goes to modulate."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=3, payload="ABCDEF123456"),
    ]
    encode_message("HELLO", callsign="K1ABC", grid="FN42")
    assert len(fake_gfsk8["modulate_calls"]) == 1
    _submode, frame_type, payload, _hz = fake_gfsk8["modulate_calls"][0]
    assert frame_type == 3
    assert payload == "ABCDEF123456"


def test_encode_long_message_packs_via_pack(fake_gfsk8):
    """Long human-readable text is pack()'s job — we just hand it off."""
    long_msg = "K1ABC: K8XYZ HELLO FROM MICHIGAN HOPE TO HEAR YOU SOON"
    encode_message(long_msg, callsign="K1ABC", grid="FN42")
    _, _, text, _ = fake_gfsk8["pack_calls"][0]
    assert text == long_msg


# ── Multi-frame support ─────────────────────────────────────────────


def test_encode_multi_frame_returns_one_buffer_per_frame(fake_gfsk8):
    """When pack() returns N frames, encode_message returns N
    audio buffers, one per frame. This is the happy-path multi-frame
    test — the encoder no longer rejects multi-frame messages."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FRAME1ABCDEF"),
        _FakeTxFrame(frame_type=0, payload="FRAME2ABCDEF"),
        _FakeTxFrame(frame_type=2, payload="FRAME3++++++"),
    ]
    audio_frames = encode_message(
        "very long message", callsign="K1ABC", grid="FN42",
    )
    assert isinstance(audio_frames, list)
    assert len(audio_frames) == 3
    # Each is a non-empty int16 buffer.
    for i, samples in enumerate(audio_frames):
        assert isinstance(samples, np.ndarray), f"frame {i} not ndarray"
        assert samples.dtype == np.int16, f"frame {i} wrong dtype"
        assert len(samples) > 0, f"frame {i} empty"


def test_encode_multi_frame_passes_each_frame_to_modulate(fake_gfsk8):
    """Verify each frame's frame_type + payload is independently
    handed to modulate(). Critical because the JS8 protocol uses
    different frame_types for first/middle/last frames in a chain."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FRAME1ABCDEF"),
        _FakeTxFrame(frame_type=0, payload="FRAME2ABCDEF"),
        _FakeTxFrame(frame_type=2, payload="FRAME3++++++"),
    ]
    encode_message(
        "very long message", callsign="K1ABC", grid="FN42",
    )
    assert len(fake_gfsk8["modulate_calls"]) == 3
    # Each modulate call gets the corresponding frame's metadata.
    expected = [
        (1, "FRAME1ABCDEF"),
        (0, "FRAME2ABCDEF"),
        (2, "FRAME3++++++"),
    ]
    for (_submode, ft, payload, _hz), (exp_ft, exp_payload) in zip(
        fake_gfsk8["modulate_calls"], expected
    ):
        assert ft == exp_ft
        assert payload == exp_payload


def test_encode_two_frame_message(fake_gfsk8):
    """The 2-frame case (e.g. @ALLCALL bulletins, QUERY MSG <id>)."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FIRST+++++++"),
        _FakeTxFrame(frame_type=2, payload="LAST++++++++"),
    ]
    audio_frames = encode_message(
        "@ALLCALL TEST", callsign="K1ABC", grid="FN42",
    )
    assert len(audio_frames) == 2


def test_encode_multi_frame_modulate_failure_includes_frame_index(fake_gfsk8):
    """If modulate() fails on a specific frame, the error message
    should make it clear which frame so debugging is easy."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FRAME1++++++"),
        _FakeTxFrame(frame_type=2, payload="FRAME2++++++"),
    ]
    fake_gfsk8["modulate_raise"] = RuntimeError("modulate boom")
    with pytest.raises(EncoderError, match="frame 0"):
        encode_message("long msg", callsign="K1ABC", grid="FN42")


def test_encode_empty_pack_result_raises(fake_gfsk8):
    fake_gfsk8["pack_frames"] = []
    with pytest.raises(EncoderError, match="no frames"):
        encode_message("HELLO", callsign="K1ABC", grid="FN42")


# ── Validation ──────────────────────────────────────────────────────


def test_encode_rejects_non_string():
    with pytest.raises(EncoderError, match="must be a string"):
        encode_message(12345, callsign="K1ABC", grid="FN42")  # type: ignore


def test_encode_rejects_empty_message():
    with pytest.raises(EncoderError, match="must not be empty"):
        encode_message("", callsign="K1ABC", grid="FN42")


def test_encode_rejects_n0call(fake_gfsk8):
    with pytest.raises(EncoderError, match="callsign"):
        encode_message("HELLO", callsign="N0CALL", grid="FN42")


def test_encode_rejects_empty_callsign(fake_gfsk8):
    with pytest.raises(EncoderError, match="callsign"):
        encode_message("HELLO", callsign="", grid="FN42")


def test_encode_rejects_empty_grid(fake_gfsk8):
    with pytest.raises(EncoderError, match="grid"):
        encode_message("HELLO", callsign="K1ABC", grid="")


def test_encode_rejects_unknown_submode(fake_gfsk8):
    with pytest.raises(EncoderError, match="unknown submode"):
        encode_message("HELLO", callsign="K1ABC", grid="FN42", submode=99)


# ── gfsk8 import / runtime errors ───────────────────────────────────


def test_encode_raises_when_gfsk8_unavailable(monkeypatch):
    """If the .so isn't installed, encoder must raise EncoderError."""
    monkeypatch.setitem(sys.modules, "gfsk8", None)
    with pytest.raises(EncoderError, match="gfsk8 module not available"):
        encode_message("HELLO", callsign="K1ABC", grid="FN42")


def test_encode_wraps_pack_exceptions(fake_gfsk8):
    fake_gfsk8["pack_raise"] = RuntimeError("pack went boom")
    with pytest.raises(EncoderError, match="gfsk8.pack failed"):
        encode_message("HELLO", callsign="K1ABC", grid="FN42")


def test_encode_wraps_modulate_exceptions(fake_gfsk8):
    fake_gfsk8["modulate_raise"] = RuntimeError("modulate went boom")
    with pytest.raises(EncoderError, match="gfsk8.modulate failed"):
        encode_message("HELLO", callsign="K1ABC", grid="FN42")


def test_encode_raises_on_silent_audio(fake_gfsk8):
    """Detects the (now-fixed) pybind11 wrapper buffer-lifetime bug."""
    fake_gfsk8["modulate_audio"] = np.zeros(157680, dtype=np.float32)
    with pytest.raises(EncoderError, match="silent audio"):
        encode_message("HELLO", callsign="K1ABC", grid="FN42")


def test_encode_raises_on_empty_audio(fake_gfsk8):
    fake_gfsk8["modulate_audio"] = np.zeros(0, dtype=np.float32)
    with pytest.raises(EncoderError, match="no samples"):
        encode_message("HELLO", callsign="K1ABC", grid="FN42")


# ── Audio level scaling ─────────────────────────────────────────────


def test_encode_scales_to_target_amplitude(fake_gfsk8):
    """Output peak should hit ~TX_LEVEL_FRAC * int16 max."""
    fake_gfsk8["modulate_audio"] = np.full(1000, 1.0, dtype=np.float32)
    audio_frames = encode_message("HELLO", callsign="K1ABC", grid="FN42")
    peak = int(np.max(np.abs(audio_frames[0])))
    expected = TX_LEVEL_FRAC * 32767
    assert abs(peak - expected) < 5


def test_encode_does_not_clip(fake_gfsk8):
    """Even with overflow input, output stays within int16 range."""
    fake_gfsk8["modulate_audio"] = np.full(1000, 2.0, dtype=np.float32)
    audio_frames = encode_message("HELLO", callsign="K1ABC", grid="FN42")
    assert int(np.max(audio_frames[0])) <= 32767
    assert int(np.min(audio_frames[0])) >= -32767


def test_encode_preserves_silence_prefix_unconditionally(fake_gfsk8):
    """The encoder leaves the 500 ms silence prefix intact on EVERY
    frame regardless of frame index. Per-frame wall-clock alignment
    is now handled by ``transmit_frame()`` in ``tx_backend.py`` —
    the encoder's job is just to produce the modulation buffer
    with its standard prefix, then resample 12 kHz → 48 kHz before
    returning so transmit_frame doesn't have to.

    Encoder input rate (gfsk8 native) is 12 kHz; output rate is 48 kHz
    after the polyphase resample, so the buffer is 4× larger.
    """
    from microjs8.modem.encoder import SILENCE_PREFIX_SAMPLES
    # gfsk8 native 12 kHz: 500 ms silence = 6000 samples,
    # 12.64 s modulation = 151680 samples.
    silence_samples_12k = 6000
    mod_samples_12k = 151680
    audio = np.concatenate([
        np.zeros(silence_samples_12k, dtype=np.float32),
        np.full(mod_samples_12k, 0.5, dtype=np.float32),
    ])
    fake_gfsk8["modulate_audio"] = audio
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FRAME1++++++"),
        _FakeTxFrame(frame_type=0, payload="FRAME2++++++"),
        _FakeTxFrame(frame_type=2, payload="FRAME3++++++"),
    ]
    audio_frames = encode_message(
        "longer message", callsign="K1ABC", grid="FN42",
    )
    assert len(audio_frames) == 3
    # 4× upsample factor — output is 48 kHz.
    expected_total_48k = (silence_samples_12k + mod_samples_12k) * 4
    expected_silence_48k = silence_samples_12k * 4   # = SILENCE_PREFIX_SAMPLES
    assert expected_silence_48k == SILENCE_PREFIX_SAMPLES, (
        f"SILENCE_PREFIX_SAMPLES should be {expected_silence_48k} "
        f"(500 ms @ 48 kHz); got {SILENCE_PREFIX_SAMPLES}"
    )
    for i, out in enumerate(audio_frames):
        assert len(out) == expected_total_48k, (
            f"frame {i} should be {expected_total_48k} samples "
            f"(silence + mod, resampled to 48 kHz); got {len(out)}"
        )
        # First sample should be silent (silence prefix is intact
        # after resampling — zero in zero out).
        assert int(np.abs(out[0])) == 0, (
            f"frame {i}'s first sample should be silent — encoder "
            f"must not strip the silence prefix"
        )


def test_encode_passes_short_buffers_through_unchanged(fake_gfsk8):
    """If gfsk8.modulate() returns audio shorter than expected (some
    hypothetical future submode), the encoder still resamples it to
    48 kHz. The output length is N×4 where N is the input length.
    tx_backend handles short-buffer cases too."""
    short_audio = np.full(1000, 0.5, dtype=np.float32)  # 1000 samples in
    fake_gfsk8["modulate_audio"] = short_audio
    audio_frames = encode_message("HELLO", callsign="K1ABC", grid="FN42")
    # 1000 in × 4 upsample = 4000 out
    assert len(audio_frames[0]) == 4000


# ── Constants ───────────────────────────────────────────────────────


def test_tx_sample_rate_constant():
    """TX_SAMPLE_RATE is the OUTPUT rate of encode_message — 48 kHz,
    matching the device rate of all supported sound cards (QDX builtin,
    DigiRig CM108, etc.). Internal resample 12 → 48 happens inside
    encode_message."""
    assert TX_SAMPLE_RATE == 48_000


def test_tx_level_frac_at_full_scale():
    """QDX requires full-scale audio per its manual."""
    assert TX_LEVEL_FRAC == 1.0
