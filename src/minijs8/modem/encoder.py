"""JS8 frame encoder — calls gfsk8 wrapper for full pack→modulate path.

The gfsk8 C++ library exposes three call sites:

  ``pack(mycall, mygrid, text, submode)``  → ``List[TxFrame]``
      High-level. Compresses human-readable text into one or more
      frames. Each TxFrame has ``.frame_type`` (int) and ``.payload``
      (12-char compressed string).

  ``encode(submode, frame_type, payload)``  → ``List[int]``
      Mid-level. Maps a payload to 79 8-FSK tone values [0..7].

  ``modulate(submode, frame_type, payload, audio_freq_hz)``
                                             → ``np.ndarray[float32]``
      High-level audio. Internally calls encode() and synthesizes
      phase-continuous 8-FSK at 12 kHz with the protocol's 500 ms
      leading silence.

We call ``pack()`` to get frames, then ``modulate()`` for each
frame's audio. We use modulate() rather than encode()+manual-synth
because the upstream C++ implementation is well-tested (see
test_loopback.cpp in the gfsk8 source) and produces audio that
JS8Call decoders accept — including the QDX firmware which has
been tuned against JS8Call's reference output.

History
-------

Earlier in Step 6, ``gfsk8.modulate()`` returned a buffer of all
zeros — not because the C++ implementation was broken, but because
the pybind11 wrapper had a buffer-lifetime bug: it created a numpy
array that VIEWED a local C++ vector's memory, and that vector was
freed when the binding lambda returned. We patched the wrapper to
copy the data into a numpy-owned buffer, rebuilt the wheel, and
modulate() now returns real audio. See gfsk8-modem-clean PR (or
the patch in build.sh).

In the interim we built a Python GFSK synthesizer to work around
the wrapper bug. That synthesizer produced math-correct GFSK that
the QDX firmware didn't recognize — the QDX was tuned and tested
against JS8Call's specific audio, not generic-correct GFSK. The
fix in this file restores the call to ``gfsk8.modulate()`` directly
and removes the DIY synth.

Multi-frame messages
--------------------

JS8 messages longer than what fits in one 12-char compressed frame
spill into multiple frames, designed for transmission in
consecutive 15-s slots. ``gfsk8.pack()`` handles this fragmentation
automatically — short messages produce one frame, longer messages
or bulletins to custom groups produce 2-3 frames.

We return a **list of audio buffers**, one per frame. The caller
(typically the scheduler) is responsible for transmitting them in
consecutive slots. Each buffer is a complete, self-contained ~13-s
TX including the protocol's 500 ms leading silence — no splicing or
concatenation needed at the audio layer.

For backwards compatibility (and cleaner single-frame paths in
tests + simple TX use), ``encode_message()`` always returns a
list, and ``encode_first_frame()`` returns just the first audio
buffer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

_log = logging.getLogger(__name__)


# JS8 codec constants. Mirror gfsk8::Submode enum values verified
# against the running module on the Pi:
#   Normal=0, Fast=1, Turbo=2, Slow=4, Ultra=8
SUBMODE_NORMAL = 0
SUBMODE_FAST = 1
SUBMODE_TURBO = 2
SUBMODE_SLOW = 4
SUBMODE_ULTRA = 8

# Sample rate of the audio that ``encode_message()`` returns.
#
# IMPORTANT: this is the OUTPUT rate (the device rate for the QDX /
# DigiRig sound cards). The gfsk8 wheel internally produces 12 kHz
# audio; we resample to 48 kHz inside ``encode_message()`` before
# returning. Doing the resample at encode time (NOT in transmit_frame's
# slot-aligned hot path) means the ~900 ms cost of the polyphase
# convolution on a Pi Zero 2W doesn't blow the slot-alignment budget.
# Slot-alignment failures were the original symptom — see
# ``transmit_frame()`` in ``tx_backend.py`` for the alignment math.
#
# Why 48 kHz: USB sound cards (both QDX and DigiRig CM108) only accept
# 44.1 / 48 / 96 kHz. PortAudio refuses to open at 12 kHz. JS8Call's
# Modulator generates samples directly at 48 kHz too. By resampling
# at encode time, ``transmit_frame()`` becomes a thin pass-through:
# strip silence prefix, hand modulation to playback.
TX_SAMPLE_RATE = 48_000

# The native rate the gfsk8 modulator produces. We resample to
# TX_SAMPLE_RATE before returning. Kept as a constant for the
# resample step + a sanity check on the wheel's output.
GFSK8_NATIVE_RATE = 12_000

# Audio center frequency in the radio's USB passband. 1500 Hz is the
# JS8Call default and is centered in a typical 3 kHz passband. The QDX
# was confirmed to be on 7.078 MHz USB with 3200 Hz passband — 1500 Hz
# audio puts us at 7.0795 MHz on-air, which is the JS8 conventional
# slot. Don't change without coordinating with the band convention.
DEFAULT_AUDIO_FREQ_HZ = 1500.0

# Output amplitude target as a fraction of int16 max. The QDX requires
# full-scale audio per its manual ("100%, no more, no less"). Going
# below this trips the radio's "audio too low" guard. Do not lower.
TX_LEVEL_FRAC = 1.0


# gfsk8.modulate() prepends 500 ms of silence to each frame's audio
# (it was designed for a JS8Call-style flow where PTT/silence are
# managed in-stream rather than via OS audio buffering).
#
# **Per-frame silence is managed by the TX backend now**, NOT by the
# encoder. The encoder hands tx_backend the full audio buffer
# (silence + modulation) at 48 kHz unchanged. tx_backend's
# ``transmit_frame()`` strips the protocol silence and re-prepends
# a freshly computed, wall-clock-aligned silence pad immediately
# before play_frame() so the modulation lands at slot+500ms regardless
# of when our scheduler managed to fire. This mirrors what JS8Call's
# Modulator::start() does (Modulator.cpp lines ~52-78) and is critical
# for slot-aligned multi-frame TX — each frame independently re-syncs
# to UTC.
#
# 500 ms × 48 kHz = 24000 samples. Defined as a module-level constant
# so tx_backend can import it for the strip step without tight
# coupling to an internal value.
SILENCE_PREFIX_SAMPLES = int(0.500 * TX_SAMPLE_RATE)  # 24000


class EncoderError(Exception):
    """Raised when frame encoding fails (validation, missing identity,
    gfsk8 module unavailable/failure)."""


def encode_message(
    message: str,
    *,
    callsign: str,
    grid: str,
    submode: int = SUBMODE_NORMAL,
    audio_freq_hz: float = DEFAULT_AUDIO_FREQ_HZ,
) -> list["np.ndarray"]:
    """Encode a JS8 message into a LIST of PCM int16 audio buffers
    at 12 kHz — one per frame.

    Short messages (heartbeats, no-body directed commands, simple
    @ALLCALL bulletins) pack to a single frame and the returned list
    has length 1. Longer messages or bulletins to custom groups pack
    to 2-3 frames; the caller is responsible for transmitting them in
    consecutive 15-s slots so receivers can stitch the message back
    together.

    Each buffer is a complete, self-contained ~13-s TX including the
    500 ms protocol silence prefix. They do NOT need to be
    concatenated at the audio layer — each is its own slot's worth.

    Parameters
    ----------
    message : str
        Human-readable JS8 message text (e.g.
        ``"K1ABC: @HB HEARTBEAT FN42"``). pack() handles the JS8
        grammar and fragmentation internally.
    callsign : str
        Operator's callsign — required by pack() for source
        attribution.
    grid : str
        Operator's Maidenhead grid (4 or 6 char) — required by pack().
    submode : int, optional
        SUBMODE_* constant. Default Normal.
    audio_freq_hz : float, optional
        Center frequency for the JS8 audio. Default 1500 Hz
        (JS8Call convention).

    Returns
    -------
    list[numpy.ndarray]
        One int16 audio buffer per frame, each at 12 kHz, each
        including its own 500 ms leading silence. Length 1 for
        single-frame messages, 2-3 for multi-frame.

    Raises
    ------
    EncoderError
        Invalid input, missing identity, or gfsk8 wrapper failure.
    """
    import numpy as np

    if not isinstance(message, str):
        raise EncoderError(
            f"message must be a string, got {type(message).__name__}"
        )
    if not message:
        raise EncoderError("message must not be empty")
    if not callsign or callsign == "N0CALL":
        raise EncoderError(
            f"encode_message requires a real callsign (got {callsign!r})"
        )
    if not grid:
        raise EncoderError("encode_message requires a grid (got empty)")
    if submode not in (SUBMODE_NORMAL, SUBMODE_FAST, SUBMODE_TURBO,
                       SUBMODE_SLOW, SUBMODE_ULTRA):
        raise EncoderError(f"unknown submode: {submode}")

    # Lazy import — keeps host tests independent of the gfsk8 .so.
    try:
        import gfsk8  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EncoderError(
            "gfsk8 module not available — install build.sh deps and "
            "the GFSK8 wheel into the venv"
        ) from exc

    submode_enum = gfsk8.Submode(submode)

    # 1) Pack the human-readable text into JS8 frames. pack() returns
    # 1 frame for short messages, 2-3 for longer ones. We modulate
    # each separately and return them all.
    try:
        frames = gfsk8.pack(callsign, grid, message, submode_enum)
    except Exception as exc:
        raise EncoderError(
            f"gfsk8.pack failed for {message!r}: {exc}"
        ) from exc

    if not frames:
        raise EncoderError(
            f"gfsk8.pack returned no frames for {message!r}"
        )

    _log.debug(
        "packed %r → %d frame(s): %s",
        message, len(frames),
        [(f.frame_type, f.payload) for f in frames],
    )

    # 2) Modulate each frame to its own 12 kHz int16 audio buffer,
    # then resample to 48 kHz. The polyphase resample takes ~700-900 ms
    # for one frame on a Pi Zero 2W; doing it here (during encode,
    # OUTSIDE any slot-aligned hot path) means transmit_frame() doesn't
    # see this latency and slot alignment stays clean.
    from minijs8.audio.playback import resample_12k_to_48k

    audio_buffers: list[np.ndarray] = []
    int16_max = 32767
    scale = TX_LEVEL_FRAC * int16_max

    for i, frame in enumerate(frames):
        try:
            audio_f32 = gfsk8.modulate(
                submode_enum, frame.frame_type, frame.payload, audio_freq_hz,
            )
        except Exception as exc:
            raise EncoderError(
                f"gfsk8.modulate failed for {message!r} frame {i} "
                f"(type={frame.frame_type} payload={frame.payload!r}): {exc}"
            ) from exc

        if audio_f32 is None or len(audio_f32) == 0:
            raise EncoderError(
                f"modulator produced no samples for {message!r} frame {i}"
            )

        # Sanity check: detect the symptom of the (now-fixed) pybind11
        # wrapper buffer-lifetime bug. If we ever find ourselves running
        # against an old/unfixed wheel, we want a clear failure mode
        # rather than silently sending dead air.
        if float(np.max(np.abs(audio_f32))) == 0.0:
            raise EncoderError(
                f"modulator returned silent audio for {message!r} "
                f"frame {i} — gfsk8 wheel may be the old broken "
                f"version. Rebuild and reinstall."
            )

        # Note: gfsk8.modulate() prepends ~500 ms of silence to each
        # frame's audio (see SILENCE_PREFIX_SAMPLES). We INTENTIONALLY
        # leave it intact here. The TX backend strips that protocol
        # silence and recomputes a fresh wall-clock-aligned silence
        # pad just before play_frame() — that's what gives us the
        # per-frame UTC alignment that JS8 receivers expect (modulation
        # lands at slot+500ms regardless of when our scheduler fires).
        # See ``transmit_frame()`` in ``tx_backend.py``.

        # 3) Convert float32 (range ±1.0) to int16. Scale to
        # TX_LEVEL_FRAC of full scale, clip defensively at ±32767.
        # The QDX requires full-scale audio.
        samples_12k = np.clip(
            audio_f32 * scale, -int16_max, int16_max,
        ).astype(np.int16)

        # 4) Resample 12 kHz → 48 kHz with proper anti-aliasing.
        # Uses the same polyphase filter the playback layer used to
        # apply, just moved upstream so it doesn't eat slot-alignment
        # budget.
        samples_48k = resample_12k_to_48k(samples_12k)
        audio_buffers.append(samples_48k)

    _log.debug(
        "encoded %r as %d frame(s); first frame: %d samples (%.2f s @ %d Hz) peak=%d",
        message, len(audio_buffers),
        len(audio_buffers[0]),
        len(audio_buffers[0]) / TX_SAMPLE_RATE,
        TX_SAMPLE_RATE,
        int(np.max(np.abs(audio_buffers[0]))),
    )
    return audio_buffers
