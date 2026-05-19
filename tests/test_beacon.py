"""Tests for microjs8.tx.beacon — HeartbeatBeacon and EmergencyBeacon.

These threads have time-driven semantics. We verify message format
and queue-interaction directly via ``_build_message()`` / ``_fire_one()``;
we don't actually run the thread loop except for one lifecycle test
that uses very short intervals.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

import pytest

from microjs8.tx.beacon import (
    EmergencyBeacon,
    HEARTBEAT_INTERVAL_S,
    HeartbeatBeacon,
)
from microjs8.tx.queue import OutboundKind, OutboundQueue


@pytest.fixture
def queue(tmp_path: Path):
    db = sqlite3.connect(
        str(tmp_path / "msg.db"),
        check_same_thread=False,
        isolation_level=None,
    )
    db.row_factory = sqlite3.Row
    yield OutboundQueue(db)
    db.close()


# ── HeartbeatBeacon ─────────────────────────────────────────────────


def test_heartbeat_message_format(queue):
    """The HB message must be the modern JS8Call format we observed
    on-air: '<call>: @HB HEARTBEAT <grid>'."""
    hb = HeartbeatBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42"),
    )
    msg = hb._build_message()
    assert msg == "K1ABC: @HB HEARTBEAT FN42"


def test_heartbeat_with_6char_grid(queue):
    hb = HeartbeatBeacon(
        queue, identity_factory=lambda: ("K1ABC", "FN42dj"),
    )
    assert hb._build_message() == "K1ABC: @HB HEARTBEAT FN42dj"


def test_heartbeat_skipped_when_callsign_unset(queue):
    """N0CALL → no HB. Operator hasn't configured yet."""
    hb = HeartbeatBeacon(
        queue, identity_factory=lambda: ("N0CALL", "FN42"),
    )
    assert hb._build_message() is None


def test_heartbeat_skipped_when_grid_empty(queue):
    hb = HeartbeatBeacon(
        queue, identity_factory=lambda: ("K1ABC", ""),
    )
    assert hb._build_message() is None


def test_heartbeat_skipped_when_identity_none(queue):
    hb = HeartbeatBeacon(queue, identity_factory=lambda: None)
    assert hb._build_message() is None


def test_heartbeat_fire_one_enqueues(queue):
    hb = HeartbeatBeacon(
        queue, identity_factory=lambda: ("K1ABC", "FN42"),
    )
    hb._fire_one()
    # Beacons enqueue for encoding (not directly to QUEUED). The
    # encode worker would normally pick this up; we just verify the
    # ENCODING-state row is there.
    msg = queue.pick_next_encoding()
    assert msg is not None
    assert msg.kind is OutboundKind.HEARTBEAT
    assert msg.text == "K1ABC: @HB HEARTBEAT FN42"
    assert hb.fire_count == 1


def test_heartbeat_fire_one_when_factory_returns_none(queue):
    """Factory returning None should not enqueue or increment counter."""
    hb = HeartbeatBeacon(
        queue, identity_factory=lambda: None,
    )
    hb._fire_one()
    assert queue.pick_next() is None
    assert hb.fire_count == 0


def test_heartbeat_when_queue_full_does_not_increment(queue):
    """If queue rejects the enqueue (full), fire_count stays put."""
    # Fill queue.
    from microjs8.tx.queue import QUEUE_DEPTH
    for i in range(QUEUE_DEPTH):
        queue.enqueue(f"M{i}", OutboundKind.DIRECTED, to_call="K1ABC")
    hb = HeartbeatBeacon(
        queue, identity_factory=lambda: ("K1ABC", "FN42"),
    )
    hb._fire_one()
    assert hb.fire_count == 0


def test_heartbeat_default_interval():
    """Default interval is 30 minutes per spec."""
    assert HEARTBEAT_INTERVAL_S == 30 * 60


def test_heartbeat_custom_interval(queue):
    """Override is supported (for tests / different operators)."""
    hb = HeartbeatBeacon(
        queue, identity_factory=lambda: ("K1ABC", "FN42"),
        interval_s=600,
    )
    # Random offset is included in the sleep computation; with 60 s
    # default offset, sleep is 600-660 s.
    sleep_s = hb._next_sleep_seconds()
    assert 600 <= sleep_s <= 660


def test_heartbeat_lifecycle_immediate_fire():
    """Starting the thread fires immediately (your locked answer)."""
    db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    queue = OutboundQueue(db)
    # Use a very long interval so the thread sleeps after the
    # immediate fire and we can stop it without racing.
    hb = HeartbeatBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42"),
        interval_s=3600,
        random_offset_s=0,
    )
    hb.start()
    # Wait for the immediate fire to land.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and hb.fire_count == 0:
        time.sleep(0.02)
    hb.stop()
    hb.join(timeout=1.0)
    assert hb.fire_count == 1
    db.close()


# ── EmergencyBeacon ─────────────────────────────────────────────────


def test_emergency_message_format_with_gps(queue):
    """Emergency message convention with GPS fix: position is
    transmitted as ``+lat -lon`` decimal degrees, the most useful
    payload for a rescuer reading the SOS off the air."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42", 42.3601, -71.0589),
    )
    msg = eb._build_message()
    assert msg == "K1ABC: @ALLCALL SOS +42.3601 -71.0589"


def test_emergency_falls_back_to_grid_when_no_gps(queue):
    """When GPS lat/lon is unavailable but a Maidenhead grid was
    pre-configured, fall back to grid — better than nothing."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42", None, None),
    )
    msg = eb._build_message()
    assert msg == "K1ABC: @ALLCALL SOS FN42"


def test_emergency_uses_n0call_when_unconfigured(queue):
    """In emergency-bypass mode, an unconfigured station may still
    transmit. This is intentional per Step 6 design — the whole point
    of the bypass is letting an unconfigured operator call for help."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("N0CALL", "FN42", 42.3601, -71.0589),
    )
    assert eb._build_message() == "N0CALL: @ALLCALL SOS +42.3601 -71.0589"


def test_emergency_requires_some_location(queue):
    """Without ANY location (no GPS, no configured grid) the SOS is
    unactionable — refuse to transmit."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "", None, None),
    )
    assert eb._build_message() is None


def test_emergency_factory_none_returns_none(queue):
    eb = EmergencyBeacon(queue, identity_factory=lambda: None)
    assert eb._build_message() is None


def test_emergency_fire_one_enqueues_as_allcall(queue):
    """Emergency uses ALLCALL kind, not HEARTBEAT."""
    eb = EmergencyBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42", 42.3601, -71.0589),
    )
    eb._fire_one()
    # Beacons enqueue for encoding — see pick_next_encoding.
    msg = queue.pick_next_encoding()
    assert msg is not None
    assert msg.kind is OutboundKind.ALLCALL
    assert "SOS" in msg.text
    # And the position is decimal degrees, not a grid.
    assert "+42.3601" in msg.text
    assert "-71.0589" in msg.text


def test_emergency_default_interval_is_3_min():
    """Per operator spec (May 2026): emergency beacon TXes every 3 min.

    Originally 5 minutes; the operator picked 3 as the sweet spot for
    'ample time for a response but not wasting time' (Q2 in the
    emergency-beacon design conversation). Pin the constant so a
    refactor doesn't quietly speed it up or slow it down.
    """
    from microjs8.tx.beacon import EMERGENCY_BEACON_INTERVAL_S
    assert EMERGENCY_BEACON_INTERVAL_S == 3 * 60


# ── Single-shot mode (HbMode.SINGLE backing) ────────────────────────


def test_single_shot_beacon_fires_once_and_exits(queue):
    """``single_shot=True`` makes the beacon fire one HB and then
    exit cleanly. The thread must not loop."""
    import threading

    hb = HeartbeatBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42"),
        interval_s=0.05,
        single_shot=True,
    )
    hb.start()
    hb.join(timeout=2.0)
    assert not hb.is_alive(), "single-shot beacon did not exit"
    assert hb.fire_count == 1


def test_single_shot_beacon_invokes_on_complete(queue):
    """``on_complete`` must be called from the beacon thread after
    the single shot completes, so app.py can flip mode → OFF."""
    completed = []
    hb = HeartbeatBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42"),
        interval_s=0.05,
        single_shot=True,
        on_complete=lambda: completed.append(True),
    )
    hb.start()
    hb.join(timeout=2.0)
    assert completed == [True]


def test_single_shot_beacon_on_complete_exception_doesnt_break(queue):
    """A misbehaving on_complete must not corrupt the beacon thread's
    exit (the thread is exiting either way; we just need a clean log)."""
    def boom() -> None:
        raise RuntimeError("test")

    hb = HeartbeatBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42"),
        interval_s=0.05,
        single_shot=True,
        on_complete=boom,
    )
    hb.start()
    hb.join(timeout=2.0)
    assert not hb.is_alive()
    # The HB still fired before on_complete raised.
    assert hb.fire_count == 1


def test_repeating_beacon_does_not_invoke_on_complete(queue):
    """on_complete is single-shot-specific. Repeating beacons run
    indefinitely so the field never fires."""
    completed = []
    hb = HeartbeatBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42"),
        interval_s=10.0,    # long — we'll stop it before the next tick
        single_shot=False,
        on_complete=lambda: completed.append(True),
    )
    hb.start()
    # Wait for the immediate-on-start fire to complete.
    import time
    for _ in range(20):
        if hb.fire_count >= 1:
            break
        time.sleep(0.05)
    assert hb.fire_count >= 1
    hb.stop()
    hb.join(timeout=2.0)
    assert completed == []


def test_heartbeat_custom_interval(queue):
    """Confirms the interval_s param actually flows through (the
    app uses 20*60 for TWENTY_MIN and 60*60 for ONE_HR)."""
    hb = HeartbeatBeacon(
        queue,
        identity_factory=lambda: ("K1ABC", "FN42"),
        interval_s=1234.0,
    )
    assert hb._interval_s == 1234.0
