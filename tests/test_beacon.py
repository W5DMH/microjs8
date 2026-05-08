"""Tests for minijs8.tx.beacon — HeartbeatBeacon and EmergencyBeacon.

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

from minijs8.tx.beacon import (
    EmergencyBeacon,
    HEARTBEAT_INTERVAL_S,
    HeartbeatBeacon,
)
from minijs8.tx.queue import OutboundKind, OutboundQueue


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
    from minijs8.tx.queue import QUEUE_DEPTH
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


def test_emergency_message_format(queue):
    """Emergency message convention: '<call>: @ALLCALL SOS <grid>'."""
    eb = EmergencyBeacon(
        queue, identity_factory=lambda: ("K1ABC", "FN42"),
    )
    msg = eb._build_message()
    assert msg == "K1ABC: @ALLCALL SOS FN42"


def test_emergency_uses_n0call_when_unconfigured(queue):
    """In emergency-bypass mode, an unconfigured station may still
    transmit. This is intentional per Step 6 design — the whole point
    of the bypass is letting an unconfigured operator call for help."""
    eb = EmergencyBeacon(
        queue, identity_factory=lambda: ("N0CALL", "FN42"),
    )
    assert eb._build_message() == "N0CALL: @ALLCALL SOS FN42"


def test_emergency_requires_grid(queue):
    """Without a grid, the message can't be useful — skip."""
    eb = EmergencyBeacon(
        queue, identity_factory=lambda: ("K1ABC", ""),
    )
    assert eb._build_message() is None


def test_emergency_factory_none_returns_none(queue):
    eb = EmergencyBeacon(queue, identity_factory=lambda: None)
    assert eb._build_message() is None


def test_emergency_fire_one_enqueues_as_allcall(queue):
    """Emergency uses ALLCALL kind, not HEARTBEAT."""
    eb = EmergencyBeacon(
        queue, identity_factory=lambda: ("K1ABC", "FN42"),
    )
    eb._fire_one()
    # Beacons enqueue for encoding — see pick_next_encoding.
    msg = queue.pick_next_encoding()
    assert msg is not None
    assert msg.kind is OutboundKind.ALLCALL
    assert "SOS" in msg.text


def test_emergency_default_interval_is_5_min():
    from minijs8.tx.beacon import EMERGENCY_BEACON_INTERVAL_S
    assert EMERGENCY_BEACON_INTERVAL_S == 5 * 60
