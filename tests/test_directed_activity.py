"""Tests for the in-memory directed-activity log.

Coverage goals:

  - Append-and-snapshot round-trip works for both directions
  - Eviction kicks in at the configured cap (NOT at maxlen+1)
  - Snapshot is immutable (tuple, frozen entries)
  - Snapshot is independent of subsequent appends (no aliasing)
  - Thread safety: concurrent appends from multiple threads don't
    corrupt the buffer (best-effort smoke test, not a stress test)
  - clear() drops everything
  - Callsigns are uppercased on entry
  - Verbs are uppercased on entry
  - Inbound + SNR/freq are preserved; outbound has SNR/freq = None
"""

from __future__ import annotations

import threading

import pytest

from minijs8.activity import (
    DEFAULT_MAX_ENTRIES,
    Direction,
    DirectedActivityEntry,
    DirectedActivityLog,
)


# ── Construction ──────────────────────────────────────────────────


def test_default_constructor_uses_default_cap():
    log = DirectedActivityLog()
    assert log.max_entries == DEFAULT_MAX_ENTRIES


def test_custom_cap_is_honored():
    log = DirectedActivityLog(max_entries=5)
    assert log.max_entries == 5


def test_zero_or_negative_cap_raises():
    with pytest.raises(ValueError):
        DirectedActivityLog(max_entries=0)
    with pytest.raises(ValueError):
        DirectedActivityLog(max_entries=-1)


def test_empty_log_has_zero_length():
    log = DirectedActivityLog()
    assert len(log) == 0
    assert log.snapshot() == ()


# ── Inbound recording ─────────────────────────────────────────────


def test_record_in_appends_entry():
    log = DirectedActivityLog()
    entry = log.record_in(
        from_call="KD8PGB", verb="SNR?", snr_db=-10, freq_hz=1500.0,
        at_unix=1700000000.0,
    )
    assert isinstance(entry, DirectedActivityEntry)
    assert entry.direction == Direction.IN
    assert entry.other_call == "KD8PGB"
    assert entry.verb == "SNR?"
    assert entry.snr_db == -10
    assert entry.freq_hz == 1500.0
    assert entry.at_unix == 1700000000.0
    # Snapshot reflects the append
    snap = log.snapshot()
    assert len(snap) == 1
    assert snap[0] == entry


def test_record_in_uppercases_callsign_and_verb():
    log = DirectedActivityLog()
    entry = log.record_in(
        from_call="kd8pgb", verb="snr?", at_unix=0.0,
    )
    assert entry.other_call == "KD8PGB"
    assert entry.verb == "SNR?"


def test_record_in_with_body_preserves_case_in_body():
    """Body content (the message text) is opaque — we don't case-fold it."""
    log = DirectedActivityLog()
    entry = log.record_in(
        from_call="KD8PGB", verb="STATUS",
        body="On 14.078 with QRM", at_unix=0.0,
    )
    assert entry.body == "On 14.078 with QRM"


def test_record_in_uses_now_when_at_unix_omitted():
    """time.time() is called when at_unix is None — entry timestamp is
    a real wall-clock value (not 0, not None)."""
    log = DirectedActivityLog()
    entry = log.record_in(from_call="K1ABC", verb="GRID?")
    assert entry.at_unix > 0


# ── Outbound recording ────────────────────────────────────────────


def test_record_out_appends_with_no_snr_or_freq():
    log = DirectedActivityLog()
    entry = log.record_out(
        to_call="KD8PGB", verb="ACK", at_unix=1700000010.0,
    )
    assert entry.direction == Direction.OUT
    assert entry.other_call == "KD8PGB"
    assert entry.verb == "ACK"
    assert entry.snr_db is None
    assert entry.freq_hz is None


def test_record_out_uppercases_callsign_and_verb():
    log = DirectedActivityLog()
    entry = log.record_out(
        to_call="kc1wdo", verb="msg", at_unix=0.0,
    )
    assert entry.other_call == "KC1WDO"
    assert entry.verb == "MSG"


def test_record_out_preserves_body():
    log = DirectedActivityLog()
    entry = log.record_out(
        to_call="KC1WDO", verb="MSG", body="5 HELLO FROM N0XYZ",
        at_unix=0.0,
    )
    assert entry.body == "5 HELLO FROM N0XYZ"


# ── Eviction at cap ───────────────────────────────────────────────


def test_eviction_at_cap_drops_oldest():
    """When the buffer is full, appending evicts the OLDEST entry."""
    log = DirectedActivityLog(max_entries=3)
    log.record_in(from_call="A", verb="SNR?", at_unix=1.0)
    log.record_in(from_call="B", verb="SNR?", at_unix=2.0)
    log.record_in(from_call="C", verb="SNR?", at_unix=3.0)
    assert len(log) == 3

    # 4th append evicts A (oldest)
    log.record_in(from_call="D", verb="SNR?", at_unix=4.0)
    assert len(log) == 3
    snap = log.snapshot()
    assert [e.other_call for e in snap] == ["B", "C", "D"]


def test_eviction_keeps_newest_order_intact():
    """Across many evictions, snapshot is still oldest→newest."""
    log = DirectedActivityLog(max_entries=2)
    for i in range(10):
        log.record_in(from_call=f"K{i}", verb="SNR?", at_unix=float(i))
    snap = log.snapshot()
    assert len(snap) == 2
    # K8 then K9 — last two of the 10
    assert [e.other_call for e in snap] == ["K8", "K9"]
    # And ordering is by at_unix ascending
    assert snap[0].at_unix < snap[1].at_unix


# ── Snapshot semantics ────────────────────────────────────────────


def test_snapshot_is_a_tuple():
    log = DirectedActivityLog()
    log.record_in(from_call="A", verb="SNR?", at_unix=0.0)
    snap = log.snapshot()
    assert isinstance(snap, tuple)


def test_snapshot_entries_are_frozen():
    """Frozen dataclass prevents mutation — important so consumers
    can't accidentally corrupt cached snapshots."""
    log = DirectedActivityLog()
    log.record_in(from_call="A", verb="SNR?", at_unix=0.0)
    entry = log.snapshot()[0]
    with pytest.raises(Exception):
        entry.verb = "WAT"  # type: ignore[misc]


def test_snapshot_independent_of_subsequent_appends():
    """Critical invariant: a snapshot taken at T0 must NOT change
    when more entries are appended at T1.

    Without this, the UI thread (rendering on its own cadence) could
    see the buffer grow under it during a single render pass — at
    best confusing, at worst an index-out-of-range crash."""
    log = DirectedActivityLog()
    log.record_in(from_call="A", verb="SNR?", at_unix=0.0)
    snap_before = log.snapshot()

    log.record_in(from_call="B", verb="SNR?", at_unix=1.0)
    log.record_in(from_call="C", verb="SNR?", at_unix=2.0)

    # The original snapshot is untouched.
    assert len(snap_before) == 1
    assert snap_before[0].other_call == "A"
    # And the new snapshot has the new entries.
    assert len(log.snapshot()) == 3


# ── Mixed inbound + outbound ──────────────────────────────────────


def test_mixed_in_out_preserves_arrival_order():
    """The directed log is chronological — IN and OUT interleave by
    actual arrival time, NOT segregated."""
    log = DirectedActivityLog()
    log.record_in(from_call="KD8PGB", verb="QUERY MSGS", at_unix=1.0)
    log.record_out(to_call="KD8PGB", verb="MSG", body="5", at_unix=2.0)
    log.record_in(from_call="KC1WDO", verb="SNR?", at_unix=3.0)
    log.record_out(to_call="KC1WDO", verb="SNR", body="-8", at_unix=4.0)

    snap = log.snapshot()
    assert len(snap) == 4
    assert [(e.direction, e.other_call) for e in snap] == [
        (Direction.IN,  "KD8PGB"),
        (Direction.OUT, "KD8PGB"),
        (Direction.IN,  "KC1WDO"),
        (Direction.OUT, "KC1WDO"),
    ]


# ── clear() ───────────────────────────────────────────────────────


def test_clear_drops_all_entries():
    log = DirectedActivityLog()
    log.record_in(from_call="A", verb="SNR?", at_unix=0.0)
    log.record_in(from_call="B", verb="SNR?", at_unix=1.0)
    assert len(log) == 2

    log.clear()
    assert len(log) == 0
    assert log.snapshot() == ()


def test_clear_then_record_starts_fresh():
    log = DirectedActivityLog()
    log.record_in(from_call="A", verb="SNR?", at_unix=0.0)
    log.clear()
    log.record_in(from_call="B", verb="SNR?", at_unix=1.0)
    snap = log.snapshot()
    assert len(snap) == 1
    assert snap[0].other_call == "B"


# ── Concurrency smoke test ────────────────────────────────────────


def test_concurrent_appends_do_not_lose_entries():
    """Smoke test: launch 4 threads each appending 50 entries; final
    count must be 200 with no corruption.

    This isn't a stress test — it's a sanity check that the lock is
    actually held around append. Without the lock, this test
    occasionally flakes (the deque's internal state goes inconsistent
    on concurrent append). With the lock, it's reliable."""
    log = DirectedActivityLog(max_entries=1000)

    def _worker(prefix: str) -> None:
        for i in range(50):
            log.record_in(
                from_call=f"{prefix}{i:02d}",
                verb="SNR?",
                at_unix=float(i),
            )

    threads = [
        threading.Thread(target=_worker, args=(p,))
        for p in ("A", "B", "C", "D")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(log) == 200, f"expected 200, got {len(log)}"
    # All entries are well-formed (no half-constructed objects)
    for entry in log.snapshot():
        assert entry.direction == Direction.IN
        assert entry.verb == "SNR?"
        assert entry.other_call.startswith(("A", "B", "C", "D"))
