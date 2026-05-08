"""Tests for minijs8.store.db.MessageStore.

Use an on-disk temp DB rather than :memory: because we want to verify
that WAL mode actually creates journal files and that the schema
survives a connection close/reopen cycle.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from minijs8.protocol.types import (
    DecodedFrame,
    FrameKind,
    HeardStation,
    ParsedFrame,
)
from minijs8.store import MessageStore


@pytest.fixture
def store(tmp_path: Path):
    s = MessageStore(tmp_path / "messages.db")
    yield s
    s.close()


def _frame(text: str = "K8XYZ @HB EN82", snr: int = -10,
           received_at: float | None = None) -> DecodedFrame:
    return DecodedFrame(
        text=text, raw="abcdefghij12", snr_db=snr, frequency_hz=1500.0,
        dt_seconds=0.0, submode=0, quality=80, frame_type=0,
        utc_seconds_of_day=0,
        received_at=received_at if received_at is not None else time.time(),
    )


def _parsed(decoded: DecodedFrame, kind: FrameKind = FrameKind.HEARTBEAT,
            from_call: str | None = "K8XYZ", to_call: str | None = "@HB",
            grid: str | None = "EN82", body: str = "",
            is_for_us: bool = False) -> ParsedFrame:
    return ParsedFrame(
        decoded=decoded, kind=kind, from_call=from_call, to_call=to_call,
        grid=grid, body=body, is_for_us=is_for_us,
    )


def _station(callsign: str = "K8XYZ", snr: int = -10,
             grid: str | None = "EN82", last_heard: float | None = None
             ) -> HeardStation:
    return HeardStation(
        callsign=callsign, snr_db=snr, grid=grid, frequency_hz=1500.0,
        distance_mi=0.0, bearing_deg=0.0,
        last_heard=last_heard if last_heard is not None else time.time(),
    )


# ── Schema / connection ─────────────────────────────────────────────


def test_schema_creates_on_first_open(tmp_path: Path):
    s = MessageStore(tmp_path / "m.db")
    s.close()
    # A second open should find the schema already there and not error.
    s2 = MessageStore(tmp_path / "m.db")
    assert s2.recent_decodes() == []
    s2.close()


def test_wal_mode_active(tmp_path: Path, store):
    """WAL mode should create either a -wal sidecar or set the pragma."""
    rows = store._conn.execute("PRAGMA journal_mode").fetchall()
    assert rows[0][0].lower() == "wal"


# ── insert_decode + recent_decodes ──────────────────────────────────


def test_insert_and_retrieve_decode(store):
    p = _parsed(_frame("K8XYZ @HB EN82"))
    rid = store.insert_decode(p)
    assert rid > 0
    rows = store.recent_decodes()
    assert len(rows) == 1
    r = rows[0]
    assert r["text"] == "K8XYZ @HB EN82"
    assert r["from_call"] == "K8XYZ"
    assert r["kind"] == "HEARTBEAT"


def test_recent_decodes_order_newest_first(store):
    t = time.time()
    for i in range(3):
        store.insert_decode(_parsed(_frame(f"X{i} @HB EN82",
                                           received_at=t + i)))
    rows = store.recent_decodes()
    assert rows[0]["text"] == "X2 @HB EN82"
    assert rows[2]["text"] == "X0 @HB EN82"


def test_filter_by_kind(store):
    store.insert_decode(_parsed(_frame("HB1"), kind=FrameKind.HEARTBEAT))
    store.insert_decode(_parsed(_frame("CQ1"), kind=FrameKind.CQ))
    rows = store.recent_decodes(kind="CQ")
    assert len(rows) == 1
    assert rows[0]["text"] == "CQ1"


# ── Heard stations ──────────────────────────────────────────────────


def test_upsert_heard_first_time(store):
    s = _station("K8XYZ", snr=-10)
    store.upsert_heard_station(s)
    rows = store.heard_stations()
    assert len(rows) == 1
    assert rows[0].callsign == "K8XYZ"
    assert rows[0].snr_db == -10


def test_upsert_heard_updates_existing(store):
    """Second sighting must update last_heard + snr, keep the same row."""
    t1 = time.time()
    store.upsert_heard_station(_station("K8XYZ", snr=-15, last_heard=t1))
    store.upsert_heard_station(_station("K8XYZ", snr=-10, last_heard=t1 + 60))
    rows = store.heard_stations()
    assert len(rows) == 1   # still just one row
    assert rows[0].snr_db == -10
    assert rows[0].last_heard == pytest.approx(t1 + 60)


def test_heard_order_most_recent_first(store):
    t = time.time()
    store.upsert_heard_station(_station("OLD", last_heard=t - 3600))
    store.upsert_heard_station(_station("NEW", last_heard=t))
    store.upsert_heard_station(_station("MID", last_heard=t - 60))
    rows = store.heard_stations()
    assert [r.callsign for r in rows] == ["NEW", "MID", "OLD"]


def test_upsert_preserves_grid_when_missing(store):
    """A directed message has no grid; the upsert must keep the
    existing grid from a prior heartbeat."""
    t = time.time()
    store.upsert_heard_station(_station("K8XYZ", grid="EN82", last_heard=t))
    # Subsequent directed message with grid=None must NOT clobber EN82.
    store.upsert_heard_station(_station("K8XYZ", grid=None, last_heard=t + 10))
    rows = store.heard_stations()
    assert rows[0].grid == "EN82"


# ── directed_to_us ──────────────────────────────────────────────────


def test_directed_to_us(store):
    store.insert_decode(_parsed(
        _frame("K8XYZ: K1ABC HELLO"),
        kind=FrameKind.DIRECTED_MESSAGE, to_call="K1ABC",
        body="HELLO", is_for_us=True,
    ))
    store.insert_decode(_parsed(
        _frame("K8XYZ: VE3ABC HELLO"),
        kind=FrameKind.DIRECTED_MESSAGE, to_call="VE3ABC",
        body="HELLO", is_for_us=False,
    ))
    rows = store.directed_to_us("K1ABC")
    assert len(rows) == 1
    assert rows[0]["body"] == "HELLO"


def test_directed_to_us_uppercase_match(store):
    store.insert_decode(_parsed(
        _frame("K8XYZ: K1ABC HELLO"),
        kind=FrameKind.DIRECTED_MESSAGE, to_call="K1ABC",
        body="HELLO", is_for_us=True,
    ))
    # Pass a lowercase callsign — the store should still match.
    rows = store.directed_to_us("k1abc")
    assert len(rows) == 1


# ── Retention ───────────────────────────────────────────────────────


def test_prune_older_than(store):
    now = time.time()
    # Three rows: one yesterday, one a month ago, one today.
    store.insert_decode(_parsed(_frame("today", received_at=now)))
    store.insert_decode(_parsed(_frame("yesterday", received_at=now - 86400)))
    store.insert_decode(_parsed(_frame("month", received_at=now - 30 * 86400)))
    n = store.prune_older_than(retain_days=7)
    # Just the month-old one is more than 7 days old.
    assert n == 1
    rows = store.recent_decodes()
    texts = {r["text"] for r in rows}
    assert "month" not in texts
    assert "today" in texts


# ── Heard cap ───────────────────────────────────────────────────────


def test_heard_cap_drops_oldest_when_full(store):
    """When more than 200 callsigns are heard, the oldest gets dropped."""
    t = time.time()
    # Insert 205 stations. The cap is 200.
    for i in range(205):
        store.upsert_heard_station(_station(f"CALL{i:04d}", last_heard=t + i))
    rows = store.heard_stations(limit=300)
    assert len(rows) == 200
    callsigns = {r.callsign for r in rows}
    # CALL0000-CALL0004 should be dropped (oldest 5).
    for i in range(5):
        assert f"CALL{i:04d}" not in callsigns
    # CALL0204 (newest) should remain.
    assert "CALL0204" in callsigns
