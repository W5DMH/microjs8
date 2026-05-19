"""Tests for ``DirectedActivityLog.record_in_supersede``: replacement
of a prior single-frame entry with a richer multi-frame reassembled
entry, so the DIRECTED screen shows ONE row per wire message even
when the message arrived split across frames.

Bug history (W5DMH bench, May 2026): HEARTBEAT replies with the JS8Call
"MSG ID N" piggy-back tag arrived as two frames — the first frame
dispatched immediately as a single-frame heartbeat (body "SNR +04"),
then ~30 s later the reassembled multi-frame body emitted again as a
separate row ("SNR +04 MSG ID 61"). Operator saw two rows for one
wire message.

The supersede method consolidates these: when the multi-frame emit
fires, it finds the prior single-frame entry from the same station
within a recency window and REPLACES it in-place. Net result: one
row that gets richer when the continuation completes.
"""
from __future__ import annotations

import pytest

from microjs8.activity import (
    DEFAULT_MAX_ENTRIES,
    Direction,
    DirectedActivityLog,
)


# ── Happy path: prefix extension replaces in place ─────────────────


def test_supersede_replaces_when_new_extends_old():
    log = DirectedActivityLog()
    log.record_in(
        from_call="KD8PGB", verb="HEARTBEAT", body="SNR +04",
        snr_db=14, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="KD8PGB", verb="HEARTBEAT", body="SNR +04 MSG ID 61",
        snr_db=None, at_unix=1030.0,
    )
    snap = log.snapshot()
    assert len(snap) == 1, "supersede should not append a duplicate"
    e = snap[0]
    assert e.body == "SNR +04 MSG ID 61"
    # SNR preserved from original single-frame entry.
    assert e.snr_db == 14
    # Timestamp updated to the supersede call.
    assert e.at_unix == 1030.0


def test_supersede_with_exact_body_match_still_replaces():
    """body == prior.body is technically an 'extension' (startswith
    holds). Replace anyway; otherwise we get a duplicate row when a
    single-frame buffer times out with no continuations."""
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="SNR", body="-09",
        snr_db=10, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="SNR", body="-09",
        snr_db=None, at_unix=1020.0,
    )
    snap = log.snapshot()
    assert len(snap) == 1


# ── No-match fallback: append as new entry ────────────────────────


def test_supersede_falls_back_to_append_when_no_prior():
    """Empty log → supersede just appends as a fresh inbound entry."""
    log = DirectedActivityLog()
    e = log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04 MSG ID 61",
        snr_db=14, at_unix=1000.0,
    )
    snap = log.snapshot()
    assert len(snap) == 1
    assert snap[0] is e
    assert e.body == "SNR +04 MSG ID 61"


def test_supersede_does_not_match_different_callsign():
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=10, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="KD8GIJ", verb="HEARTBEAT", body="SNR +04 MSG ID 7",
        snr_db=None, at_unix=1020.0,
    )
    # Different from_call → no match → appended as a fresh row.
    snap = log.snapshot()
    assert len(snap) == 2


def test_supersede_does_not_match_different_verb():
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=10, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="INFO", body="SNR +04 MSG ID 7",
        snr_db=None, at_unix=1020.0,
    )
    snap = log.snapshot()
    assert len(snap) == 2


def test_supersede_does_not_match_when_new_body_not_a_prefix():
    """Body doesn't start with the prior body → no supersede.
    Conservative — protects against false consolidation of unrelated
    messages from the same station."""
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=10, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR -21 MSG ID 5",
        snr_db=None, at_unix=1020.0,
    )
    snap = log.snapshot()
    assert len(snap) == 2


def test_supersede_does_not_match_outbound_entries():
    """Only INBOUND prior entries are considered — never collapse an
    outbound row with an inbound one even if the body happens to
    match prefix-wise (different directions, different meanings)."""
    log = DirectedActivityLog()
    log.record_out(
        to_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04 MSG ID 1",
        snr_db=None, at_unix=1020.0,
    )
    snap = log.snapshot()
    assert len(snap) == 2
    assert snap[0].direction is Direction.OUT
    assert snap[1].direction is Direction.IN


# ── Recency window ─────────────────────────────────────────────────


def test_supersede_respects_default_recency_window():
    """Default recency is 60 s. A prior entry older than that should
    not be considered for replacement — it's stale, a fresh wire
    message would not be a continuation of it."""
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=10, at_unix=1000.0,
    )
    # 120 s later — outside the 60 s window.
    log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04 MSG ID 1",
        snr_db=None, at_unix=1120.0,
    )
    snap = log.snapshot()
    assert len(snap) == 2


def test_supersede_custom_recency_window():
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=10, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04 MSG ID 1",
        snr_db=None, at_unix=1120.0,
        recency_s=200.0,    # extended window — captures the prior entry
    )
    snap = log.snapshot()
    assert len(snap) == 1
    assert snap[0].body == "SNR +04 MSG ID 1"


# ── Metadata preservation ──────────────────────────────────────────


def test_supersede_preserves_snr_when_new_call_passes_none():
    """The reassembled emit doesn't have SNR (frame-level metadata
    lost during assembly), so it passes snr_db=None. The original
    single-frame entry's SNR must be preserved on the replacement."""
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=14, freq_hz=1500.0, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04 MSG ID 1",
        snr_db=None, freq_hz=None, at_unix=1010.0,
    )
    e = log.snapshot()[0]
    assert e.snr_db == 14
    assert e.freq_hz == 1500.0


def test_supersede_overrides_snr_when_new_call_provides_one():
    """If the new call does pass SNR, use it. (Less common — the
    multi-frame path typically can't compute a meaningful aggregate
    SNR, but we allow it.)"""
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=14, at_unix=1000.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04 MSG ID 1",
        snr_db=20, at_unix=1010.0,
    )
    e = log.snapshot()[0]
    assert e.snr_db == 20


# ── Multiple inbound from same station — supersede the LATEST ────


def test_supersede_targets_most_recent_prior_match():
    """If multiple prior inbound entries from the same station match,
    only the most-recent one is replaced. Older entries remain
    untouched."""
    log = DirectedActivityLog()
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR -05",
        snr_db=5, at_unix=1000.0,
    )
    log.record_in(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04",
        snr_db=14, at_unix=1010.0,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="HEARTBEAT", body="SNR +04 MSG ID 1",
        snr_db=None, at_unix=1020.0,
    )
    snap = log.snapshot()
    assert len(snap) == 2
    # Older entry preserved
    assert snap[0].body == "SNR -05"
    # Latest replaced with extension
    assert snap[1].body == "SNR +04 MSG ID 1"
