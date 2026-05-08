"""TX backend — composes encoding, audio playback, and CAT control.

This is the layer that turns "transmit this 12-character message" into
the actual sequence of operations:

  1. Encode the message (gfsk8.encode → int16 samples at 12 kHz)
  2. Assert PTT via CAT (rigctld T 1)
  3. Wait the radio's per-model lead time
  4. Stream the audio to the QDX (PortAudio output)
  5. Wait the radio's per-model tail time
  6. Release PTT via CAT (rigctld T 0)

Two implementations:

  ``RealTxBackend`` — invokes the actual encoder/playback/CAT.
  ``FakeTxBackend`` — records what would have been transmitted, with
                      configurable failure modes for tests.

Tests EXCLUSIVELY use FakeTxBackend; production code uses RealTxBackend.
This separation is the single most important design decision for Step 6
testing — it lets us exercise scheduler / queue / retry FSM without
ever opening a real audio device or rigctld socket.

Stuck-PTT defense
-----------------

Every code path that asserts PTT MUST release it before returning,
even on exception. We use try/finally throughout. The CatService also
runs a watchdog that forces release after _PTT_MAX_HOLD_S — so even
if our code panics in a way that bypasses finally, the radio doesn't
stay keyed forever.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import numpy as np

from minijs8.audio.playback import (
    TX_OUTPUT_RATE,
)
from minijs8.cat.radios import RadioDef
from minijs8.cat.service import CatService
from minijs8.modem.encoder import (
    EncoderError,
    SILENCE_PREFIX_SAMPLES,
    SUBMODE_NORMAL,
    encode_message,
)

_log = logging.getLogger(__name__)


# Identity provider: returns (callsign, grid) tuple, or None if the
# operator hasn't configured a callsign+grid yet. Called fresh at each
# TX time so config edits propagate without restarting the daemon.
IdentityFactory = Callable[[], Optional[tuple[str, str]]]


# JS8 normal-mode protocol parameters used by the per-frame
# wall-clock alignment in ``transmit_frame()``.
#
# Receivers expect the modulation in each frame to begin at exactly
# ``slot_start + TX_TARGET_DELAY_MS`` ms. JS8Call's Modulator.cpp
# uses the same value (``delay_ms = 500`` for normal mode).
#
# Alignment formula (mirrors JS8Call's ``Modulator::start()``):
#
#     mstr = ms_now % SLOT_MS                # ms into current 15s slot
#     samples_per_period = 48000 * delay_ms / 1000   # 24000 samples
#     samples_elapsed = mstr * 48               # samples already played
#     samples_into_period = samples_elapsed % samples_per_period
#     silence = samples_per_period - samples_into_period
#
# The key property: ``silence`` is ALWAYS in [0, samples_per_period).
# It can never be negative — if we wake at slot+600 ms, we just emit
# 400 ms of silence and modulation lands at slot+1000 ms (the next
# 500 ms boundary). JS8Call NEVER fails alignment; it just shifts to
# the next boundary. We match that.
#
# Per-frame wall-clock re-sync: each frame independently reads the
# wall clock and computes its own silence pad, so multi-frame
# messages don't accumulate drift across consecutive slots.
TX_TARGET_DELAY_MS = 500
JS8_SLOT_MS = 15_000  # normal-mode slot length in milliseconds


@dataclass(frozen=True)
class TxResult:
    """Outcome of a single transmit attempt.

    The scheduler / queue use this to decide retry behavior.
    """

    success: bool
    # Diagnostic message for log/UI. On failure, describes what went
    # wrong. On success, can be empty or include encoded-audio length.
    message: str = ""
    # Wall-clock time when the actual TX started (PTT asserted +
    # lead-in elapsed). None if we never got that far.
    tx_started_at: Optional[float] = None
    # Wall-clock time PTT was released. None on early failure.
    tx_finished_at: Optional[float] = None


class _PlaybackProto(Protocol):
    """Minimal interface the TxBackend needs from a playback impl.

    Mirrors ``minijs8.audio.playback.AudioPlayback``. Tests can swap
    in a fake without dragging the whole class hierarchy.

    The ``play_frame`` API decouples silence (just zeros — no point
    resampling) from modulation (already at 48 kHz, ready for the
    USB sound card). This matches the JS8Call architecture where
    the Modulator emits silence_frames of zeros then symbols, all
    via ``readData()`` callbacks from QAudioOutput.
    """

    def play_frame(
        self,
        silence_samples_48k: int,
        modulation_48k: np.ndarray,
    ) -> None: ...


class TxBackend(abc.ABC):
    """Abstract transmit interface.

    The fundamental unit of TX is a *burst*: one PTT cycle that
    contains one or more independently slot-aligned frames. This
    mirrors JS8Call's transmit model — MainWindow keys PTT, calls
    Modulator::start() once per frame across consecutive TR periods,
    drops PTT only after the last frame's fade-out completes.

    Three-step API for callers (typically the scheduler):

      1. ``start_burst()`` — keys PTT, blocks for ptt_on_delay so
         the radio's audio detector can settle, returns success.
      2. ``transmit_frame(audio)`` — does wall-clock-aligned silence
         padding, plays the audio buffer. Called once per frame, in
         the slot we want THIS frame to land in. Does NOT touch PTT.
         Uses JS8Call's round-up alignment formula so it never fails
         on timing — if we wake past slot+500 ms, we round up to the
         next 500 ms boundary instead of failing. Returns failure
         only on actual playback / device errors; PTT is left in its
         current state and the caller decides whether to call
         end_burst() to clean up or abort.
      3. ``end_burst()`` — blocks for ptt_off_delay so the radio's
         tail audio drains, then drops PTT.

    Convenience wrapper:

      ``transmit(message)`` — encode + start_burst + transmit_frame
      for each frame + end_burst, all back-to-back. Equivalent to
      the old single-call behavior. Used by tests and direct
      single-call sites; NOT used by the slot-aligned scheduler.

    Implementations must guarantee PTT is released before returning
    from any of these methods on exception. Use try/finally.
    """

    @abc.abstractmethod
    def transmit(self, message: str) -> TxResult:
        """Encode and TX a message in one call.

        Equivalent to: encode → start_burst → transmit_frame for each
        frame → end_burst. NOT slot-aligned for multi-frame messages
        — frames TX back-to-back in one slot. Used by tests and any
        caller that just wants "send this and tell me when it's
        done." For slot-aligned multi-frame, use the burst API
        directly via the scheduler.
        """

    @abc.abstractmethod
    def encode(self, message: str) -> "list[np.ndarray]":
        """Encode a message into a list of per-frame audio buffers.

        Does NOT touch PTT or audio. The scheduler uses this to
        pre-encode a multi-frame message so it can TX one frame per
        slot via ``transmit_frame()``.

        Raises EncoderError on encoding failure. Identity (callsign +
        grid) is resolved internally via the configured
        identity_factory.
        """

    @abc.abstractmethod
    def start_burst(self) -> TxResult:
        """Begin a TX burst: key PTT and let the radio settle.

        After this returns success, the radio is keyed and ready to
        accept audio. The caller is expected to call ``transmit_frame()``
        one or more times, then ``end_burst()`` to clean up.

        On failure (CAT disconnected, ptt_on() returned False), no
        PTT cleanup is needed — PTT was never successfully keyed.
        Result includes the failure reason.

        Calling start_burst() while already in a burst is a logic
        error and raises RuntimeError — the previous burst should
        have been ended first.
        """

    @abc.abstractmethod
    def transmit_frame(self, audio: "np.ndarray") -> TxResult:
        """TX one frame's audio with wall-clock-aligned silence pad.

        Must be called between ``start_burst()`` and ``end_burst()``.
        Strips the protocol's 500 ms silence prefix from ``audio``,
        computes how many ms we are into the current slot, prepends
        fresh silence so modulation lands at the next 500 ms boundary
        in the slot, then plays the result through the callback-mode
        playback layer.

        Uses JS8Call's round-up alignment formula (mirrors
        ``Modulator::start()``): if wall clock is past slot+500 ms,
        modulation lands at the next 500 ms boundary instead of
        failing. Returns failure only on actual playback / device
        errors, never on timing.

        Does NOT touch PTT. PTT remains keyed across frames so the
        radio sees one continuous TX from key-on to key-off.
        """

    @abc.abstractmethod
    def end_burst(self) -> None:
        """End the current TX burst: drain audio tail, drop PTT.

        Idempotent — safe to call when no burst is active (e.g. after
        start_burst() returned failure). Always tries to drop PTT.

        Blocks for ptt_off_delay (the radio's tail-time) before
        actually releasing PTT so the final audio samples make it
        through the radio's audio chain before keying off.
        """

    @abc.abstractmethod
    def set_burst_hold_seconds(self, seconds: float) -> None:
        """Tell the underlying CAT layer how long PTT may legitimately
        be held for the upcoming burst.

        Multi-frame bursts hold PTT continuously across multiple
        slots — for a 5-frame message, that's ~75 seconds. The CAT
        layer's PTT watchdog has a default cap (~20 s) that prevents
        runaway PTT from a stuck audio path. Without this hint, the
        watchdog would fire mid-burst and yank PTT off the air.

        The scheduler calls this BEFORE ``start_burst()`` with the
        burst-deadline value (n_frames × per_frame_budget + grace).
        The CAT layer's override is per-cycle: ``end_burst()`` (which
        calls ptt_off) clears it, so subsequent TX paths fall back
        to the default watchdog cap.

        Pass ``seconds <= 0`` to clear the override immediately.
        """


class RealTxBackend(TxBackend):
    """Production backend — real encoder, real playback, real CAT."""

    def __init__(
        self,
        cat: CatService,
        playback: _PlaybackProto,
        radio: RadioDef,
        identity_factory: "IdentityFactory",
        submode: int = SUBMODE_NORMAL,
    ) -> None:
        """
        identity_factory: zero-arg callable returning ``(callsign, grid)``
        at TX time. Lets us pick up the operator's most recent edit to
        either field without restarting the daemon — the beacon and
        scheduler use the same pattern. May raise if no identity is
        configured; we treat that as "skip this TX" via TxResult.
        """
        self._cat = cat
        self._playback = playback
        self._radio = radio
        self._identity_factory = identity_factory
        self._submode = submode
        # Burst lifecycle state. True between start_burst() and
        # end_burst(). Used to enforce ordering and idempotence.
        self._burst_active = False

    def encode(self, message: str) -> "list[np.ndarray]":
        """Encode a message into per-frame audio buffers.

        Resolves identity (callsign + grid) via the configured
        identity factory, then runs ``encode_message()`` from the
        modem layer. Pure CPU work — no PTT, no audio device, no
        CAT contact. Typically runs in <100ms for a 3-frame message.

        Raises EncoderError on any failure (missing identity,
        gfsk8.pack failure, modulator error, etc.). The caller is
        responsible for translating that into a TxResult.
        """
        try:
            identity = self._identity_factory()
        except Exception as exc:
            raise EncoderError(f"identity factory raised: {exc}") from exc
        if identity is None:
            raise EncoderError("identity not configured")
        callsign, grid = identity
        return encode_message(
            message,
            callsign=callsign,
            grid=grid,
            submode=self._submode,
        )

    # ── Burst lifecycle: start_burst → transmit_frame* → end_burst ──

    def start_burst(self) -> TxResult:
        """Begin a TX burst: key PTT and let the radio settle.

        After return, ``self._burst_active`` is True and the radio is
        keyed. The caller should pair this with ``end_burst()`` even
        if subsequent ``transmit_frame()`` calls fail — that's how
        we ensure PTT is dropped on every code path.
        """
        if self._burst_active:
            raise RuntimeError(
                "start_burst() called while already in a burst — "
                "previous burst was not ended"
            )

        if not self._cat.is_connected:
            return TxResult(
                success=False,
                message="CAT disconnected; cannot key PTT",
            )

        if not self._cat.ptt_on():
            return TxResult(
                success=False,
                message="ptt_on() failed; CAT may be down",
            )

        # Mark active immediately so end_burst() will release PTT
        # even if the settle-sleep raises (KeyboardInterrupt etc.).
        self._burst_active = True
        try:
            time.sleep(self._radio.ptt_on_delay_ms / 1000.0)
        except Exception as exc:
            _log.exception("ptt_on settle interrupted; ending burst")
            self.end_burst()
            return TxResult(
                success=False,
                message=f"ptt_on settle error: {exc}",
            )

        _log.info("burst started: PTT keyed, settled %d ms",
                  self._radio.ptt_on_delay_ms)
        return TxResult(success=True, message="burst started")

    def transmit_frame(self, audio: "np.ndarray") -> TxResult:
        """TX one frame's audio with wall-clock-aligned silence pad.

        Mirrors JS8Call's ``Modulator::start()`` (Modulator.cpp).

        Algorithm:
          1. Strip the protocol's 500 ms silence prefix from the
             encoder output (gfsk8.modulate() always puts it at
             sample 0; we replace it with our own wall-clock-aligned
             silence).
          2. Resample modulation 12 kHz → 48 kHz once.
          3. Read the wall clock as late as possible.
          4. Compute silence using JS8Call's round-up formula —
             modulation lands at the next 500 ms boundary in the
             slot. NEVER fails: if we're at slot+600 ms, we emit
             400 ms of silence and modulation lands at slot+1000 ms.
             This matches JS8Call's behavior — they never fail
             alignment, just shift to the next boundary.
          5. Subtract the radio's tx_pipeline_latency_ms to compensate
             for OS → ALSA → USB → radio audio chain latency. Without
             this, modulation arrives on-air late (typical pipeline
             on Pi+QDX: 400-625 ms).
          6. Hand silence_count + modulation_48k to the playback
             layer, which calls stream.start() in pull-mode. PortAudio
             requests samples via callback; the callback emits silence
             then modulation then signals done. This is the same
             pull-mode architecture JS8Call uses (Qt's QAudioOutput
             reads from Modulator::readData).

        Each frame independently re-syncs to UTC by reading the wall
        clock fresh, just like JS8Call's per-frame Modulator::start()
        calls. Multi-frame messages don't accumulate drift.

        Does NOT touch PTT — the burst keeps PTT keyed across all
        frames so the radio sees one continuous TX.
        """
        if not self._burst_active:
            raise RuntimeError(
                "transmit_frame() called outside a burst — "
                "start_burst() must be called first"
            )

        # Reset the PTT watchdog timer. Multi-frame bursts span
        # multiple slots (~45 s for a 3-frame message) — well past
        # the CAT-layer max-hold default. Without this kick, the
        # watchdog would force-release PTT mid-burst, silencing
        # later frames. The kick says "we're actively progressing";
        # if the burst actually hangs (play_frame blocks beyond its
        # internal timeout, thread dies), the watchdog still fires
        # eventually — exactly as designed.
        self._cat.ptt_kick()

        # Strip the protocol silence prefix (gfsk8 always puts 500 ms
        # at the start). The encoder has already resampled to 48 kHz,
        # so the prefix is at 48 kHz too (24000 samples = 500 ms).
        # Defensive: if the buffer is shorter than SILENCE_PREFIX_SAMPLES
        # — some hypothetical future submode — treat the whole buffer
        # as modulation.
        if len(audio) > SILENCE_PREFIX_SAMPLES:
            modulation_48k = audio[SILENCE_PREFIX_SAMPLES:]
        else:
            modulation_48k = audio

        # NB: no resample step here. The encoder produces 48 kHz audio
        # (see encoder.TX_SAMPLE_RATE). Doing the resample at encode
        # time keeps the slot-aligned hot path lean — the polyphase
        # filter takes ~700-900 ms on Pi Zero 2W, which would blow our
        # alignment budget if it ran here.

        # Read wall clock at the latest possible moment so silence
        # count reflects "what time it actually is now," not "what
        # time it was when we started this tick."
        ms_now = int(time.time() * 1000)
        mstr_ms = ms_now % JS8_SLOT_MS

        # JS8Call alignment formula (mirrors Modulator::start()):
        # round up to the next TX_TARGET_DELAY_MS boundary in the
        # slot. Always non-negative; never fails.
        #
        # samples_per_period = 48000 * 500 / 1000 = 24000 samples
        #                    = one 500 ms boundary worth of samples
        # samples_elapsed    = mstr in samples
        # samples_in_period  = where in the current 500 ms period we are
        # silence_samples    = samples remaining until next boundary
        samples_per_period = (
            TX_OUTPUT_RATE * TX_TARGET_DELAY_MS // 1000
        )
        samples_elapsed = mstr_ms * TX_OUTPUT_RATE // 1000
        samples_into_period = samples_elapsed % samples_per_period
        silence_samples_48k = samples_per_period - samples_into_period
        # Edge case: if we're EXACTLY on a 500 ms boundary, the formula
        # above gives a full period of silence (24000 samples / 500 ms).
        # That's correct — we'd be transmitting at the CURRENT boundary
        # otherwise, but mstr being exactly 0/500/1000/... is
        # vanishingly rare in practice and "wait one period" is the
        # safe behavior.

        # Compensate for the audio pipeline latency (OS → ALSA → USB →
        # radio). Subtract from silence so modulation actually arrives
        # on-air at the target boundary.
        latency_samples_48k = (
            self._radio.tx_pipeline_latency_ms * TX_OUTPUT_RATE // 1000
        )
        compensated_silence_48k = silence_samples_48k - latency_samples_48k

        # If pipeline latency exceeds the available silence, we'd need
        # NEGATIVE silence — impossible. Round up to the NEXT boundary
        # by adding a full period. Modulation lands one boundary later
        # but is still cleanly aligned.
        while compensated_silence_48k < 0:
            compensated_silence_48k += samples_per_period

        # Diagnostic log — useful for verifying alignment on-air.
        # Convert samples back to ms for readability.
        silence_ms = compensated_silence_48k * 1000 // TX_OUTPUT_RATE
        _log.info(
            "TX align: mstr=%d ms → silence=%d ms "
            "(%d samples @ 48k, latency_comp=%d ms) → "
            "modulation=%d samples (%.2f s)",
            mstr_ms, silence_ms, compensated_silence_48k,
            self._radio.tx_pipeline_latency_ms,
            len(modulation_48k), len(modulation_48k) / TX_OUTPUT_RATE,
        )

        # Hand to the pull-mode playback layer. Blocking — returns
        # when the callback has emitted all modulation samples.
        # PTT stays on; the burst manager handles PTT release.
        tx_started_at = time.time()
        try:
            self._playback.play_frame(
                compensated_silence_48k, modulation_48k,
            )
        except Exception as exc:
            _log.exception("playback raised during transmit_frame")
            return TxResult(
                success=False,
                message=f"playback error: {exc}",
                tx_started_at=tx_started_at,
            )
        tx_finished_at = time.time()

        return TxResult(
            success=True,
            message=(
                f"transmitted {compensated_silence_48k} silence + "
                f"{len(modulation_48k)} modulation samples"
            ),
            tx_started_at=tx_started_at,
            tx_finished_at=tx_finished_at,
        )

    def end_burst(self) -> None:
        """End the current TX burst: drain audio tail, drop PTT.

        Idempotent — safe to call when no burst is active. Always
        attempts ptt_off so a half-failed start_burst() (PTT keyed
        but settle interrupted) still cleans up.
        """
        if not self._burst_active:
            # Nothing to end. Cheap to be idempotent here.
            return

        # Drain the radio's audio tail. Even if ptt_off_delay's sleep
        # is interrupted, we MUST attempt ptt_off — the radio is
        # currently keyed and the operator wants it off.
        try:
            time.sleep(self._radio.ptt_off_delay_ms / 1000.0)
        except Exception:
            _log.exception("ptt_off_delay sleep raised; releasing PTT anyway")

        try:
            self._cat.ptt_off()
        except Exception:
            _log.exception("ptt_off raised in end_burst; PTT state uncertain")
        finally:
            self._burst_active = False
            _log.info("burst ended: PTT released")

    def set_burst_hold_seconds(self, seconds: float) -> None:
        """Forward the per-cycle PTT max-hold override to the CAT
        layer. See ``TxBackend.set_burst_hold_seconds`` docstring."""
        self._cat.set_ptt_max_hold(seconds)

    # ── Single-call wrapper for tests / direct callers ──────────────

    def transmit(self, message: str) -> TxResult:
        """Encode + start_burst + transmit_frame for each frame +
        end_burst, all in one call.

        Convenience wrapper. Plays all frames BACK-TO-BACK in one
        slot — NOT slot-aligned for multi-frame. Used by tests and
        direct callers that just want "send this and tell me when
        it's done." For slot-aligned multi-frame, use the burst API
        directly via the scheduler.
        """
        # 1) Encode. If encoding fails, the radio is never keyed.
        try:
            audio_frames = self.encode(message)
        except EncoderError as exc:
            _log.error("encode failed for %r: %s", message, exc)
            return TxResult(success=False, message=f"encode failed: {exc}")

        n_frames = len(audio_frames)
        total_samples = sum(len(a) for a in audio_frames)
        _log.info(
            "transmit: %r → %d frame(s), %d total samples",
            message, n_frames, total_samples,
        )

        # 2) Start the burst (key PTT).
        start_result = self.start_burst()
        if not start_result.success:
            return start_result

        # 3) TX each frame. On any failure, end the burst cleanly
        # and return the failure.
        first_started_at: Optional[float] = None
        last_finished_at: Optional[float] = None

        try:
            for i, samples in enumerate(audio_frames):
                # Re-check CAT between frames — multi-frame TX takes
                # 15-40 s and CAT could drop mid-message.
                if not self._cat.is_connected:
                    return TxResult(
                        success=False,
                        message=(
                            f"CAT disconnected during multi-frame TX "
                            f"(after frame {i} of {n_frames})"
                        ),
                        tx_started_at=first_started_at,
                    )
                result = self.transmit_frame(samples)
                if first_started_at is None:
                    first_started_at = result.tx_started_at
                if result.tx_finished_at is not None:
                    last_finished_at = result.tx_finished_at
                if not result.success:
                    return TxResult(
                        success=False,
                        message=(
                            f"frame {i + 1}/{n_frames} failed: "
                            f"{result.message}"
                        ),
                        tx_started_at=first_started_at,
                    )
        finally:
            # End the burst on every code path so PTT is released.
            self.end_burst()

        return TxResult(
            success=True,
            message=(
                f"transmitted {n_frames} frame(s), "
                f"{total_samples} total samples"
            ),
            tx_started_at=first_started_at,
            tx_finished_at=last_finished_at,
        )


@dataclass
class _RecordedTransmission:
    """One entry in FakeTxBackend's transmission log."""

    message: str
    timestamp: float


class FakeTxBackend(TxBackend):
    """Test double — records transmissions, never touches hardware.

    Configurable behavior:
      ``return_value``   — fixed TxResult to return from transmit()
                           and transmit_frame()
      ``raise_on``       — raise this exception instead of returning
      ``delay``          — sleep this many seconds inside transmit()
                           (simulates real TX duration in scheduler tests)
      ``frames_per_msg`` — how many frames encode() should return for
                           any given message (default 1, simulating
                           short messages). Tests that exercise multi-
                           frame paths set this to 2 or 3.
      ``start_burst_fails`` — if True, start_burst() returns failure
                           (simulates CAT/PTT failure)

    Inspect:
      ``transmissions``  — list of _RecordedTransmission (whole-message
                           transmits)
      ``encoded_for``    — list of message strings encode() was called
                           for
      ``audio_played``   — list of audio buffers transmit_frame()
                           received
      ``burst_events``   — ordered list of "start"/"end" strings
                           recording burst lifecycle calls (lets tests
                           verify PTT-stays-keyed semantics)
    """

    def __init__(
        self,
        return_value: Optional[TxResult] = None,
        raise_on: Optional[Exception] = None,
        delay: float = 0.0,
        frames_per_msg: int = 1,
        start_burst_fails: bool = False,
    ) -> None:
        self._return_value = return_value
        self._raise_on = raise_on
        self._delay = delay
        self._frames_per_msg = frames_per_msg
        self._start_burst_fails = start_burst_fails
        self.transmissions: list[_RecordedTransmission] = []
        self.encoded_for: list[str] = []
        self.audio_played: list[np.ndarray] = []
        self.burst_events: list[str] = []
        self.burst_hold_seconds_calls: list[float] = []
        self._burst_active = False

    def encode(self, message: str) -> "list[np.ndarray]":
        self.encoded_for.append(message)
        # Synthetic per-frame audio. Length matches a real Normal-mode
        # frame so any sample-count assertions in tests get the right
        # answer (157,680 samples = 13.14s @ 12kHz).
        frame_samples = 157_680
        return [
            np.zeros(frame_samples, dtype=np.int16)
            for _ in range(self._frames_per_msg)
        ]

    def start_burst(self) -> TxResult:
        if self._burst_active:
            raise RuntimeError(
                "FakeTxBackend.start_burst() called while already in "
                "a burst (test bug)"
            )
        self.burst_events.append("start")
        if self._start_burst_fails:
            return TxResult(success=False, message="fake start_burst failure")
        self._burst_active = True
        return TxResult(success=True, message="fake burst started")

    def transmit_frame(self, audio: "np.ndarray") -> TxResult:
        if not self._burst_active:
            raise RuntimeError(
                "FakeTxBackend.transmit_frame() called outside a burst "
                "(test bug — start_burst() must come first)"
            )
        self.audio_played.append(audio)
        if self._delay > 0:
            time.sleep(self._delay)
        if self._raise_on is not None:
            raise self._raise_on
        if self._return_value is not None:
            return self._return_value
        return TxResult(
            success=True,
            message=f"fake-transmitted {len(audio)} samples",
            tx_started_at=time.time(),
            tx_finished_at=time.time(),
        )

    def end_burst(self) -> None:
        if not self._burst_active:
            return
        self.burst_events.append("end")
        self._burst_active = False

    def set_burst_hold_seconds(self, seconds: float) -> None:
        """Records each call so tests can verify the scheduler is
        telling us the right thing before start_burst()."""
        self.burst_hold_seconds_calls.append(seconds)

    def transmit(self, message: str) -> TxResult:
        self.transmissions.append(
            _RecordedTransmission(message=message, timestamp=time.time())
        )
        if self._delay > 0:
            time.sleep(self._delay)
        if self._raise_on is not None:
            raise self._raise_on
        if self._return_value is not None:
            return self._return_value
        return TxResult(
            success=True,
            message=f"fake-transmitted {message!r}",
            tx_started_at=time.time(),
            tx_finished_at=time.time(),
        )
