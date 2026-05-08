"""Tests for minijs8.tx.tx_backend — RealTxBackend and FakeTxBackend.

Most of Step 6 will use FakeTxBackend; we verify both here. RealTxBackend
gets exercised against doubles for the encoder, playback, and CAT.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

import numpy as np
import pytest

from minijs8.cat.radios import QDX, RadioDef
from minijs8.modem.encoder import EncoderError
from minijs8.tx.tx_backend import (
    FakeTxBackend,
    RealTxBackend,
    TxResult,
)


# ── Fakes for RealTxBackend dependencies ─────────────────────────────


class _FakeCatService:
    """Minimal substitute for CatService."""

    def __init__(self, connected: bool = True,
                 ptt_on_returns: bool = True,
                 ptt_off_returns: bool = True) -> None:
        self.is_connected = connected
        self._ptt_on_returns = ptt_on_returns
        self._ptt_off_returns = ptt_off_returns
        self.events: list[str] = []

    def ptt_on(self) -> bool:
        self.events.append("ptt_on")
        return self._ptt_on_returns

    def ptt_off(self) -> bool:
        self.events.append("ptt_off")
        return self._ptt_off_returns

    def ptt_kick(self) -> None:
        # Don't append to events — kick is high-frequency during
        # multi-frame bursts; logging each one would just clutter
        # event-based assertions. Tests that care about kicks can
        # check kick_count instead.
        self.kick_count = getattr(self, "kick_count", 0) + 1


class _FakePlayback:
    """Test double mirroring the new ``AudioPlayback.play_frame`` API.

    Records each call's silence prefix and modulation buffer so tests
    can assert on alignment behavior. ``raise_on_play`` triggers a
    fault in play_frame() to exercise error paths.

    The legacy ``played`` list (just the modulation arrays) is retained
    for backwards compatibility with existing tests that don't care
    about the silence count — many tests only check "did SOMETHING get
    played and how many samples did it have."
    """

    def __init__(self, raise_on_play: Optional[Exception] = None) -> None:
        # Per-call records: list of (silence_samples_48k, modulation)
        # for tests that want to assert on the alignment.
        self.frames: list[tuple[int, np.ndarray]] = []
        # Legacy compatibility: the modulation array for each call.
        # Tests that just check "did we play something" use this.
        self.played: list[np.ndarray] = []
        self.raise_on_play = raise_on_play

    def play_frame(
        self,
        silence_samples_48k: int,
        modulation_48k: np.ndarray,
    ) -> None:
        if self.raise_on_play is not None:
            raise self.raise_on_play
        self.frames.append((silence_samples_48k, modulation_48k))
        self.played.append(modulation_48k)


class _FakeTxFrame:
    def __init__(self, frame_type: int, payload: str) -> None:
        self.frame_type = frame_type
        self.payload = payload


@pytest.fixture(autouse=True)
def deterministic_tx_time(monkeypatch):
    """Pin ``time.time()`` inside tx_backend to a known slot-aligned
    moment so tests of ``transmit()`` and ``transmit_audio()`` don't
    randomly fail when real wall-clock time happens to be past the
    500 ms slot-alignment target.

    Picked epoch = 1_700_000_000 because:

      * 1_700_000_000 % 15 == 0  → exactly on a slot boundary
      * sub-target (500 ms after slot start) is the perfect alignment
        moment, giving 500 ms of generated silence in the wall-clock
        alignment math.

    Tests that need to exercise the "too late" path can override this
    with their own monkeypatch of ``minijs8.tx.tx_backend.time.time``.
    """
    fixed = 1_700_000_010.1  # 100 ms into a slot (slot starts at xx10)
    # Patch only the time module reference inside tx_backend so other
    # uses of time.time elsewhere stay real.
    import minijs8.tx.tx_backend as txb

    original_time = txb.time.time
    monkeypatch.setattr(txb.time, "time", lambda: fixed)
    yield
    monkeypatch.setattr(txb.time, "time", original_time)


@pytest.fixture
def fake_gfsk8(monkeypatch):
    """Stub gfsk8 with the real pack/encode/submode_parms API surface.

    Tests can override how many frames pack() returns by mutating
    holder["pack_frames"] before invoking transmit(). Default is a
    single-frame heartbeat-shaped result.
    """
    holder: dict[str, object] = {
        "pack_frames": [_FakeTxFrame(frame_type=3, payload="3-C30787OkK8")],
        "modulate_calls": [],
    }

    class _FakeSubmode:
        def __init__(self, value: int):
            self.value = value

    class _FakeSubmodeParms:
        sample_rate = 12_000
        samples_per_symbol = 1920
        tone_spacing_hz = 6.25
        start_delay_ms = 500

    class _FakeGfsk8:
        Submode = _FakeSubmode
        RX_SAMPLE_RATE = 12_000

        @staticmethod
        def pack(mycall, mygrid, text, submode):
            return list(holder["pack_frames"])  # type: ignore[arg-type]

        @staticmethod
        def modulate(submode, frame_type, payload, audio_freq_hz):
            holder["modulate_calls"].append(  # type: ignore[union-attr]
                (frame_type, payload)
            )
            # 500 ms silence prefix + 13 s of moderate-amplitude audio
            silence = np.zeros(6000, dtype=np.float32)
            signal = np.full(151680, 0.5, dtype=np.float32)
            return np.concatenate([silence, signal])

        @staticmethod
        def encode(submode, frame_type, payload):
            return [i % 8 for i in range(79)]

        @staticmethod
        def submode_parms(submode):
            return _FakeSubmodeParms()

    monkeypatch.setitem(sys.modules, "gfsk8", _FakeGfsk8)
    # Attach the holder to the class so tests can mutate via either
    # the fixture return or the module reference.
    _FakeGfsk8._holder = holder  # type: ignore[attr-defined]
    return holder


def _ok_identity():
    """Identity factory returning a real callsign+grid."""
    return ("K1ABC", "FN42")


def _no_identity():
    """Identity factory returning None (unconfigured station)."""
    return None


def _raising_identity():
    raise RuntimeError("config disappeared")


# ── RealTxBackend happy path ─────────────────────────────────────────


def test_real_tx_full_cycle(fake_gfsk8):
    """Successful TX: encode → ptt_on → play → ptt_off → success TxResult."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    fast_radio = RadioDef(
        id="test", display_name="Test", hamlib_id=0, baud_rate=9600,
        ptt_method="CAT", description="", cat_required=True,
        ptt_on_delay_ms=0, ptt_off_delay_ms=0,
    )
    backend = RealTxBackend(cat, pb, fast_radio, identity_factory=_ok_identity)
    result = backend.transmit("K1ABC: @HB HEARTBEAT FN42")

    assert result.success
    assert cat.events == ["ptt_on", "ptt_off"]
    assert len(pb.played) == 1
    assert pb.played[0].dtype == np.int16


def test_real_tx_uses_radio_ptt_delays(fake_gfsk8):
    """ptt_on_delay and ptt_off_delay come from the radio definition."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    radio_with_delays = RadioDef(
        id="test", display_name="Test", hamlib_id=0, baud_rate=9600,
        ptt_method="CAT", description="", cat_required=True,
        ptt_on_delay_ms=50, ptt_off_delay_ms=30,
    )
    backend = RealTxBackend(
        cat, pb, radio_with_delays, identity_factory=_ok_identity,
    )
    t0 = time.monotonic()
    result = backend.transmit("HELLO")
    elapsed = time.monotonic() - t0

    assert result.success
    # Total time should be at least the sum of the two delays.
    assert elapsed >= 0.080


# ── RealTxBackend identity handling ─────────────────────────────────


def test_real_tx_no_identity_fails_clean(fake_gfsk8):
    """Unconfigured station — identity factory returns None.
    The TX must fail before keying the radio."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(cat, pb, QDX, identity_factory=_no_identity)
    result = backend.transmit("HELLO")

    assert not result.success
    assert "identity" in result.message.lower()
    # PTT was never touched.
    assert cat.events == []


def test_real_tx_identity_factory_raises(fake_gfsk8):
    """If identity provider raises, fail clean — never key radio."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, QDX, identity_factory=_raising_identity,
    )
    result = backend.transmit("HELLO")

    assert not result.success
    assert "identity" in result.message.lower()
    assert cat.events == []


# ── RealTxBackend failure paths ──────────────────────────────────────


def test_real_tx_encode_failure_no_ptt(fake_gfsk8, monkeypatch):
    """If encoding fails, PTT is NEVER asserted."""
    def bad_pack(*a, **kw):
        raise RuntimeError("pack went boom")
    # Replace pack on the stub module — the encoder calls pack first.
    monkeypatch.setattr(sys.modules["gfsk8"], "pack", bad_pack)

    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(cat, pb, QDX, identity_factory=_ok_identity)
    result = backend.transmit("HELLO")

    assert not result.success
    assert "encode" in result.message.lower()
    # PTT was never touched — defense in depth.
    assert cat.events == []


def test_real_tx_cat_disconnected_fails_cleanly(fake_gfsk8):
    """If CAT is down, refuse to transmit. Don't play audio without RF."""
    cat = _FakeCatService(connected=False)
    pb = _FakePlayback()
    backend = RealTxBackend(cat, pb, QDX, identity_factory=_ok_identity)
    result = backend.transmit("HELLO")

    assert not result.success
    assert "CAT" in result.message
    assert pb.played == []


def test_real_tx_ptt_on_failure_fails_cleanly(fake_gfsk8):
    """If ptt_on returns False (rigctld error), don't play."""
    cat = _FakeCatService(connected=True, ptt_on_returns=False)
    pb = _FakePlayback()
    backend = RealTxBackend(cat, pb, QDX, identity_factory=_ok_identity)
    result = backend.transmit("HELLO")

    assert not result.success
    assert "ptt_on" in result.message
    assert pb.played == []


def test_real_tx_playback_error_releases_ptt(fake_gfsk8):
    """The critical safety property: if playback raises, PTT MUST be released."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback(raise_on_play=RuntimeError("USB unplugged"))
    fast_radio = RadioDef(
        id="test", display_name="Test", hamlib_id=0, baud_rate=9600,
        ptt_method="CAT", description="", cat_required=True,
        ptt_on_delay_ms=0, ptt_off_delay_ms=0,
    )
    backend = RealTxBackend(cat, pb, fast_radio, identity_factory=_ok_identity)
    result = backend.transmit("HELLO")

    assert not result.success
    # Even though playback failed, ptt_off was called.
    assert "ptt_on" in cat.events
    assert "ptt_off" in cat.events
    assert cat.events.index("ptt_off") > cat.events.index("ptt_on")


def test_real_tx_ptt_off_in_finally_even_on_unexpected_exception(fake_gfsk8):
    """Any exception during the keyed period must result in PTT release."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback(raise_on_play=ValueError("weird internal bug"))
    fast_radio = RadioDef(
        id="test", display_name="Test", hamlib_id=0, baud_rate=9600,
        ptt_method="CAT", description="", cat_required=True,
        ptt_on_delay_ms=0, ptt_off_delay_ms=0,
    )
    backend = RealTxBackend(cat, pb, fast_radio, identity_factory=_ok_identity)
    backend.transmit("HELLO")
    assert "ptt_off" in cat.events


# ── Multi-frame TX (Phase 1) ─────────────────────────────────────────


def _fast_radio() -> RadioDef:
    """Helper: zero-delay radio definition for fast tests."""
    return RadioDef(
        id="test", display_name="Test", hamlib_id=0, baud_rate=9600,
        ptt_method="CAT", description="", cat_required=True,
        ptt_on_delay_ms=0, ptt_off_delay_ms=0,
    )


def test_real_tx_multi_frame_plays_each_frame(fake_gfsk8):
    """For an N-frame message, transmit() invokes playback.play_frame()
    N times — once per frame — but PTT is keyed ONCE for the whole
    burst, not once per frame.

    This is the JS8Call multi-frame contract: PTT stays keyed across
    all frames in a burst so the radio sees one continuous TX. The
    convenience ``transmit()`` wrapper still plays them back-to-back
    (not slot-aligned) — slot alignment is the scheduler's job — but
    the PTT lifecycle now matches what JS8Call does."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FIRST+++++++"),
        _FakeTxFrame(frame_type=0, payload="MIDDLE++++++"),
        _FakeTxFrame(frame_type=2, payload="LAST++++++++"),
    ]

    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    result = backend.transmit("KN4CRD MSG HELLO FROM EN83")

    assert result.success, result.message
    assert len(pb.played) == 3, "should play 3 frames"
    # ONE PTT cycle for the whole burst — not three.
    assert cat.events == ["ptt_on", "ptt_off"], (
        f"expected single PTT cycle for burst, got {cat.events!r}"
    )


def test_real_tx_multi_frame_passes_correct_frame_data_to_modulator(fake_gfsk8):
    """Each frame's distinct (frame_type, payload) pair must reach
    the modulator. Critical because JS8 protocol uses different
    frame_types for first/middle/last frames in a chain."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FIRST+++++++"),
        _FakeTxFrame(frame_type=2, payload="LAST++++++++"),
    ]

    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    backend.transmit("@ALLCALL TEST")

    calls = fake_gfsk8["modulate_calls"]
    assert len(calls) == 2
    assert calls[0] == (1, "FIRST+++++++")
    assert calls[1] == (2, "LAST++++++++")


def test_real_tx_multi_frame_cat_drops_mid_message(fake_gfsk8):
    """If CAT drops between frames within a burst, transmit() returns
    failure with a frame-index in the message, and end_burst() still
    runs to attempt PTT release.

    With the new burst contract, PTT is keyed once at the start.
    Between frames, transmit() re-checks ``cat.is_connected``; if it's
    gone, we abort the loop. The finally-block calls end_burst()
    which attempts ptt_off — even if CAT can't actually deliver the
    command, we tried."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FIRST+++++++"),
        _FakeTxFrame(frame_type=0, payload="MIDDLE++++++"),
        _FakeTxFrame(frame_type=2, payload="LAST++++++++"),
    ]

    # CatService that disconnects after the first frame's playback.
    # We hook into the playback (not ptt_off, since ptt_off only runs
    # at end_burst now) so the disconnect happens between frame 1 and
    # frame 2, just like the original test's intent.
    class _DroppingPlayback(_FakePlayback):
        def __init__(self, cat):
            super().__init__()
            self._cat = cat

        def play_frame(self, silence_samples_48k, modulation_48k):
            super().play_frame(silence_samples_48k, modulation_48k)
            if len(self.played) >= 1:
                self._cat.is_connected = False

    cat = _FakeCatService(connected=True)
    pb = _DroppingPlayback(cat)
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    result = backend.transmit("multi-frame body that drops")

    assert not result.success
    assert "CAT disconnected" in result.message
    # Frame 0 played; the inter-frame check caught the disconnect
    # before frame 1 could play.
    assert len(pb.played) == 1
    # ptt_on was keyed once (at start_burst). ptt_off ran in
    # end_burst's finally — exactly one of each.
    assert cat.events.count("ptt_on") == 1
    assert cat.events.count("ptt_off") == 1


def test_real_tx_multi_frame_playback_failure_releases_ptt(fake_gfsk8):
    """If playback throws on a middle frame, PTT is released, and
    the failure result identifies which frame failed."""
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FIRST+++++++"),
        _FakeTxFrame(frame_type=0, payload="MIDDLE++++++"),
        _FakeTxFrame(frame_type=2, payload="LAST++++++++"),
    ]

    play_count = 0
    original_play_frame = _FakePlayback.play_frame

    class _MidFailPlayback(_FakePlayback):
        def play_frame(self, silence_samples_48k, modulation_48k):
            nonlocal play_count
            play_count += 1
            if play_count == 2:
                raise RuntimeError("audio device error")
            original_play_frame(self, silence_samples_48k, modulation_48k)

    cat = _FakeCatService(connected=True)
    pb = _MidFailPlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    result = backend.transmit("multi-frame body that fails mid-tx")

    assert not result.success
    assert "frame 2/3" in result.message
    # PTT was released even though the second play raised.
    assert cat.events.count("ptt_off") == cat.events.count("ptt_on")


# ── FakeTxBackend ────────────────────────────────────────────────────


def test_fake_records_transmissions():
    backend = FakeTxBackend()
    backend.transmit("HELLO 1")
    backend.transmit("HELLO 2")
    assert [t.message for t in backend.transmissions] == ["HELLO 1", "HELLO 2"]


def test_fake_returns_default_success():
    backend = FakeTxBackend()
    result = backend.transmit("HELLO")
    assert result.success


def test_fake_can_simulate_failure():
    backend = FakeTxBackend(
        return_value=TxResult(success=False, message="simulated failure")
    )
    result = backend.transmit("HELLO")
    assert not result.success
    assert result.message == "simulated failure"


def test_fake_can_raise():
    backend = FakeTxBackend(raise_on=RuntimeError("simulated crash"))
    with pytest.raises(RuntimeError, match="simulated crash"):
        backend.transmit("HELLO")


def test_fake_delay_is_observable():
    """The delay parameter lets scheduler tests verify time ordering."""
    backend = FakeTxBackend(delay=0.1)
    t0 = time.monotonic()
    backend.transmit("HELLO")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.1


# ── Wall-clock-aligned silence padding (Step 1) ──────────────────────


def _make_real_backend(fake_gfsk8) -> RealTxBackend:
    """Helper for alignment tests — minimal RealTxBackend with stubs."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    fast_radio = RadioDef(
        id="test", display_name="Test", hamlib_id=0, baud_rate=9600,
        ptt_method="CAT", description="", cat_required=True,
        ptt_on_delay_ms=0, ptt_off_delay_ms=0,
    )
    return RealTxBackend(cat, pb, fast_radio, identity_factory=_ok_identity)


def test_align_strips_protocol_silence_then_pads_to_500ms(
    fake_gfsk8, monkeypatch,
):
    """At slot+100ms, alignment should produce 400ms of fresh silence
    + 12.64s of modulation @ 48kHz (the protocol silence is stripped,
    fresh silence is computed using JS8Call's round-up formula).
    """
    import minijs8.tx.tx_backend as txb
    # 100 ms into a slot — target silence is 400 ms.
    monkeypatch.setattr(txb.time, "time", lambda: 1_700_000_010.1)

    backend = _make_real_backend(fake_gfsk8)
    result = backend.transmit("@HB HEARTBEAT FN42")
    assert result.success, result.message

    # The playback received: silence_samples_48k = 19200 (400 ms @ 48k)
    # and modulation_48k = 151680 * 4 = 606720 samples (12.64 s @ 48k).
    pb = backend._playback  # type: ignore[attr-defined]
    assert len(pb.frames) == 1
    silence_samples_48k, modulation_48k = pb.frames[0]
    # Expected 400 ms of silence at 48 kHz = 19200 samples.
    assert silence_samples_48k == 19200, (
        f"expected 19200 silence samples (400 ms @ 48k), "
        f"got {silence_samples_48k}"
    )
    # Modulation: 151680 samples @ 12 kHz × 4 = 606720 samples @ 48 kHz.
    assert len(modulation_48k) == 606720, (
        f"expected 606720 modulation samples (12.64 s @ 48k), "
        f"got {len(modulation_48k)}"
    )
    # Modulation array is the modulation only (no silence prefix).
    assert modulation_48k.dtype == np.int16


def test_align_full_500ms_silence_at_slot_boundary(
    fake_gfsk8, monkeypatch,
):
    """At exactly slot start (mstr=0), JS8Call's round-up formula
    emits a full 500 ms period (24000 samples @ 48k) of silence."""
    import minijs8.tx.tx_backend as txb
    # Exactly at slot boundary.
    monkeypatch.setattr(txb.time, "time", lambda: 1_700_000_010.0)

    backend = _make_real_backend(fake_gfsk8)
    result = backend.transmit("@HB HEARTBEAT FN42")
    assert result.success

    pb = backend._playback  # type: ignore[attr-defined]
    silence_samples_48k, modulation_48k = pb.frames[0]
    # At mstr=0, samples_into_period = 0 →
    # silence = samples_per_period - 0 = 24000.
    assert silence_samples_48k == 24000
    assert len(modulation_48k) == 606720


def test_align_past_target_rounds_up_to_next_boundary(
    fake_gfsk8, monkeypatch,
):
    """When wall-clock is past slot+500 ms, JS8Call's algorithm rounds
    UP to the NEXT 500 ms boundary instead of failing.

    At mstr=600ms, we're 100ms past the 500ms boundary. Modulation
    should land at the next boundary (slot+1000ms), so we emit 400 ms
    of silence (= 19200 samples @ 48 kHz)."""
    import minijs8.tx.tx_backend as txb
    # 600 ms into a slot — past the 500 ms target.
    monkeypatch.setattr(txb.time, "time", lambda: 1_700_000_010.6)

    backend = _make_real_backend(fake_gfsk8)
    result = backend.transmit("@HB HEARTBEAT FN42")
    assert result.success, (
        f"JS8Call's algorithm rounds up; should never fail. "
        f"got: {result.message}"
    )

    pb = backend._playback  # type: ignore[attr-defined]
    silence_samples_48k, _ = pb.frames[0]
    # mstr=600 → samples_elapsed = 28800 → samples_into_period = 4800
    # → silence = 24000 - 4800 = 19200 (= 400 ms @ 48k).
    # Modulation lands at slot+1000ms (the next 500ms boundary).
    assert silence_samples_48k == 19200, (
        f"expected 19200 silence samples (round up to next 500ms "
        f"boundary), got {silence_samples_48k}"
    )


def test_align_past_target_keeps_ptt_keyed_normally(
    fake_gfsk8, monkeypatch,
):
    """When wall-clock is past target, JS8Call's round-up formula
    means we still TX successfully (just one boundary later), and
    the burst lifecycle is normal — one ptt_on, one ptt_off."""
    import minijs8.tx.tx_backend as txb
    monkeypatch.setattr(txb.time, "time", lambda: 1_700_000_010.6)

    backend = _make_real_backend(fake_gfsk8)
    result = backend.transmit("@HB HEARTBEAT FN42")
    assert result.success

    cat = backend._cat  # type: ignore[attr-defined]
    # PTT was keyed once and released once — normal burst lifecycle.
    assert cat.events == ["ptt_on", "ptt_off"], (
        f"expected one PTT cycle, got {cat.events!r}"
    )


def test_align_multi_frame_each_frame_aligns_independently(
    fake_gfsk8, monkeypatch,
):
    """A multi-frame message produces correctly-aligned audio on
    EVERY frame. Each frame independently re-syncs to the wall clock
    via the JS8Call alignment formula.

    We use a fixed time (the autouse fixture pins it) so each frame
    aligns the same way; what we check is that BOTH frames received
    the alignment treatment, not just the first.
    """
    fake_gfsk8["pack_frames"] = [
        _FakeTxFrame(frame_type=1, payload="FRAME1++++++"),
        _FakeTxFrame(frame_type=2, payload="FRAME2++++++"),
    ]
    backend = _make_real_backend(fake_gfsk8)
    result = backend.transmit("multi-frame body")
    assert result.success, result.message

    pb = backend._playback  # type: ignore[attr-defined]
    assert len(pb.frames) == 2

    # The autouse fixture pins time at slot+100ms (mstr=100), so each
    # frame should produce 400 ms (19200 samples @ 48k) of silence
    # plus 606720 samples (12.64 s @ 48k) of modulation.
    expected_silence = 19200
    expected_mod = 606720
    for i, (silence_samples, modulation) in enumerate(pb.frames):
        assert silence_samples == expected_silence, (
            f"frame {i}: expected {expected_silence} silence samples, "
            f"got {silence_samples}"
        )
        assert len(modulation) == expected_mod, (
            f"frame {i}: expected {expected_mod} modulation samples, "
            f"got {len(modulation)}"
        )


# ── Burst lifecycle (start_burst / transmit_frame / end_burst) ───────


def test_burst_start_keys_ptt_and_settles(fake_gfsk8):
    """start_burst() should: key PTT, sleep ptt_on_delay_ms, return
    success. End state: burst is active (transmit_frame can be
    called)."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    result = backend.start_burst()
    try:
        assert result.success, result.message
        assert cat.events == ["ptt_on"]
        assert backend._burst_active is True
    finally:
        backend.end_burst()


def test_burst_end_releases_ptt(fake_gfsk8):
    """end_burst() should release PTT and clear active flag."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    backend.start_burst()
    backend.end_burst()
    assert cat.events == ["ptt_on", "ptt_off"]
    assert backend._burst_active is False


def test_burst_end_is_idempotent(fake_gfsk8):
    """Calling end_burst() multiple times is safe — second call is
    a no-op. Important for finally-block usage."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    backend.start_burst()
    backend.end_burst()
    backend.end_burst()  # second call should be a no-op
    # Still just one ptt_off — not two.
    assert cat.events == ["ptt_on", "ptt_off"]


def test_burst_end_when_never_started_is_safe(fake_gfsk8):
    """end_burst() on an inactive backend doesn't touch PTT.
    Defensive: caller might call end_burst() in a finally even when
    start_burst() failed."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    backend.end_burst()  # never started
    assert cat.events == [], "end_burst on inactive should not touch PTT"


def test_burst_double_start_raises(fake_gfsk8):
    """Calling start_burst() while already in a burst is a logic
    error — caller is supposed to end the previous one first."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    backend.start_burst()
    try:
        with pytest.raises(RuntimeError, match="already in a burst"):
            backend.start_burst()
    finally:
        backend.end_burst()


def test_burst_start_cat_disconnected_fails(fake_gfsk8):
    """start_burst() with CAT down returns failure cleanly. PTT was
    never keyed, no cleanup needed."""
    cat = _FakeCatService(connected=False)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    result = backend.start_burst()
    assert not result.success
    assert "CAT disconnected" in result.message
    assert backend._burst_active is False


def test_burst_start_ptt_on_failure(fake_gfsk8):
    """If ptt_on() returns False, start_burst() returns failure and
    leaves burst inactive."""
    class _PttRefusingCat(_FakeCatService):
        def ptt_on(self) -> bool:
            self.events.append("ptt_on")
            return False  # PTT failed to key

    cat = _PttRefusingCat(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    result = backend.start_burst()
    assert not result.success
    assert "ptt_on() failed" in result.message
    assert backend._burst_active is False


def test_transmit_frame_outside_burst_raises(fake_gfsk8):
    """transmit_frame() requires an active burst — raises if not."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    audio = np.zeros(157_680, dtype=np.int16)
    with pytest.raises(RuntimeError, match="outside a burst"):
        backend.transmit_frame(audio)


def test_transmit_frame_does_not_touch_ptt(fake_gfsk8):
    """Within a burst, transmit_frame() must NOT touch PTT — that's
    the whole point of the burst contract: PTT stays keyed across
    frames."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    backend.start_burst()
    cat.events.clear()  # erase the ptt_on from start_burst
    try:
        audio = np.zeros(157_680, dtype=np.int16)
        backend.transmit_frame(audio)
        # transmit_frame should not have touched PTT at all.
        assert cat.events == [], (
            f"transmit_frame must not touch PTT; got {cat.events!r}"
        )
    finally:
        backend.end_burst()


def test_burst_three_frames_one_ptt_cycle(fake_gfsk8):
    """Full burst lifecycle: 3 frames TX'd, exactly ONE ptt_on +
    ONE ptt_off across all of them.

    This is the load-bearing test for the JS8Call multi-frame
    contract — PTT stays keyed for the whole burst."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    audio = np.zeros(157_680, dtype=np.int16)
    backend.start_burst()
    try:
        for _ in range(3):
            r = backend.transmit_frame(audio)
            assert r.success
    finally:
        backend.end_burst()

    assert len(pb.played) == 3
    assert cat.events == ["ptt_on", "ptt_off"], (
        f"expected one PTT cycle for 3-frame burst, got {cat.events!r}"
    )


def test_transmit_frame_kicks_ptt_watchdog(fake_gfsk8):
    """transmit_frame() must kick the PTT watchdog at the start of
    each frame, so multi-frame bursts (39+ s) don't trip the 20 s
    watchdog mid-burst.

    Without the kick, the watchdog would force-release PTT after 20 s
    and frames 2 and 3 of a 3-frame burst would TX with PTT off — no
    actual RF transmitted, but our logs would still say 'sent'."""
    cat = _FakeCatService(connected=True)
    pb = _FakePlayback()
    backend = RealTxBackend(
        cat, pb, _fast_radio(), identity_factory=_ok_identity,
    )
    audio = np.zeros(157_680, dtype=np.int16)
    backend.start_burst()
    try:
        for _ in range(3):
            backend.transmit_frame(audio)
    finally:
        backend.end_burst()

    # Each transmit_frame call should have kicked the watchdog once.
    assert cat.kick_count == 3, (
        f"expected 3 watchdog kicks for 3-frame burst, got {cat.kick_count}"
    )


def test_fake_burst_lifecycle():
    """FakeTxBackend tracks burst events for scheduler tests."""
    fake = FakeTxBackend()
    fake.start_burst()
    audio = np.zeros(157_680, dtype=np.int16)
    fake.transmit_frame(audio)
    fake.transmit_frame(audio)
    fake.end_burst()
    assert fake.burst_events == ["start", "end"]
    assert len(fake.audio_played) == 2


def test_fake_transmit_frame_outside_burst_raises():
    """FakeTxBackend mirrors RealTxBackend's contract — calling
    transmit_frame outside a burst is a test bug."""
    fake = FakeTxBackend()
    audio = np.zeros(157_680, dtype=np.int16)
    with pytest.raises(RuntimeError, match="outside a burst"):
        fake.transmit_frame(audio)


def test_fake_burst_can_simulate_failure():
    """FakeTxBackend exposes start_burst_fails for testing scheduler
    failure paths."""
    fake = FakeTxBackend(start_burst_fails=True)
    result = fake.start_burst()
    assert not result.success
    # When start fails, _burst_active stays False.
    audio = np.zeros(157_680, dtype=np.int16)
    with pytest.raises(RuntimeError, match="outside a burst"):
        fake.transmit_frame(audio)
