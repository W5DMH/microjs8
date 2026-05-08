"""Regression tests for per-radio TX latency calibration.

These values are EMPIRICALLY MEASURED on real hardware against an
NTP-disciplined reference receiver running JS8Call. They are NOT
guesses — each non-zero value here represents bench-tested
calibration that another operator (or a future reviewer) might be
tempted to "tidy up" without realizing what it does on-air.

The point of this file: if any of these values change, a test fails
loudly. The PR can still proceed if the operator re-measured against
a different reference path (different host, different cable, different
DigiRig firmware revision) — they just have to update the test AND
add their measurement methodology to the comment in radios.py.

Why it lives in its own file rather than test_radios.py: there is no
test_radios.py — most radio-property assertions live in test_ptt_factory
or test_rigctld_launcher. Calibration is its own concern (RF timing,
not PTT routing or rigctld arguments), so it gets its own file.
"""

from __future__ import annotations

from minijs8.cat.radios import (
    DIGIRIG_RTS_ONLY,
    QDX,
    XIEGU_G90_DIGIRIG,
    known_radio_ids,
    get_radio,
)


# ── Per-radio latency calibration ──────────────────────────────────


def test_g90_tx_pipeline_latency_calibrated_to_100ms():
    """Xiegu G90 + DigiRig was measured at +99-125 ms late on a
    reference station (NTP-disciplined laptop) over a 5-min window.
    100 ms compensation centers our TX on the slot boundary.

    See radios.py:XIEGU_G90_DIGIRIG for derivation comment.
    """
    assert XIEGU_G90_DIGIRIG.tx_pipeline_latency_ms == 100, (
        "G90 latency was empirically calibrated to 100 ms — "
        "if you want to change this, re-measure on a reference "
        "station and update the comment block in radios.py"
    )


def test_qdx_tx_pipeline_latency_calibrated_to_200ms():
    """QRP Labs QDX was measured at +180-214 ms late on a reference
    station (NTP-disciplined laptop) over a 5-min window. 200 ms
    compensation centers our TX on the slot boundary.

    Note: QDX needs ~2x the G90's compensation despite having no
    external sound card. Theory: QDX's internal DSP buffers a couple
    of FFT frames before the modulator. See radios.py:QDX for
    derivation comment.
    """
    assert QDX.tx_pipeline_latency_ms == 200, (
        "QDX latency was empirically calibrated to 200 ms — "
        "if you want to change this, re-measure on a reference "
        "station and update the comment block in radios.py"
    )


def test_digirig_rts_only_tx_pipeline_latency_calibrated_to_90ms():
    """digirig-rts-only is a chassis profile used with any RTS-only
    radio (FM walkies, uSDX, TRX-DUO, etc.). Calibrated against a
    generic FM walkie on the W5DMH bench: DT was 84-94 ms, mean
    ~89 ms. 90 ms compensation centers TX on the slot boundary.

    The DigiRig CM108 sound card is the dominant latency component,
    which is why this is within 10 ms of the G90's 100 ms (also
    CM108-based). Operators with different walkies should see
    similar DT values — no re-tuning typically needed. See
    radios.py:DIGIRIG_RTS_ONLY for derivation comment.
    """
    assert DIGIRIG_RTS_ONLY.tx_pipeline_latency_ms == 90, (
        "digirig-rts-only latency was empirically calibrated to "
        "90 ms — if you want to change this, re-measure on a "
        "reference station and update the comment block in radios.py"
    )


# ── Sanity invariants (should hold for ALL radios) ─────────────────


def test_all_radios_have_nonneg_latency():
    """Negative latency values would cause the alignment math in
    tx_backend.transmit_frame() to ADD silence rather than subtract,
    which is meaningless. Any future radio entry must have >= 0.
    """
    for radio_id in known_radio_ids():
        radio = get_radio(radio_id)
        assert radio.tx_pipeline_latency_ms >= 0, (
            f"{radio_id} has negative tx_pipeline_latency_ms "
            f"({radio.tx_pipeline_latency_ms}) — that's nonsensical"
        )


def test_all_radios_have_reasonable_latency_bound():
    """Pipeline latency on USB sound cards on Linux is typically
    50-300 ms. A value over 500 ms suggests a unit error (e.g.
    accidentally entering microseconds or seconds). Guard against
    that without being too tight: 600 ms upper bound leaves
    generous room for unusual hardware.
    """
    for radio_id in known_radio_ids():
        radio = get_radio(radio_id)
        assert radio.tx_pipeline_latency_ms <= 600, (
            f"{radio_id} has tx_pipeline_latency_ms="
            f"{radio.tx_pipeline_latency_ms} ms — over 600 ms is "
            f"suspicious; check for unit confusion (s vs ms)"
        )


def test_latency_compensation_field_is_int():
    """The transmit_frame math does ``samples = ms * SAMPLE_RATE //
    1000`` — integer-truncation behavior. A float would compile but
    silently lose precision somewhere. Keep it int.
    """
    for radio_id in known_radio_ids():
        radio = get_radio(radio_id)
        assert isinstance(radio.tx_pipeline_latency_ms, int), (
            f"{radio_id} tx_pipeline_latency_ms is "
            f"{type(radio.tx_pipeline_latency_ms).__name__}, must be int"
        )
