"""Tests for microjs8.tx.queue.OutboundQueue.

Covers state transitions, FIFO order, queue depth cap, ACK matching,
and timeout sweep — all the behavior the scheduler depends on.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from microjs8.tx.queue import (
    ACK_TIMEOUT_S,
    MAX_ATTEMPTS,
    OutboundKind,
    OutboundQueue,
    OutboundState,
    QUEUE_DEPTH,
)


@pytest.fixture
def conn(tmp_path: Path):
    """Real sqlite3 connection on a temp file. Mirrors the runtime
    pattern (MessageStore opens with row_factory=sqlite3.Row)."""
    db = sqlite3.connect(
        str(tmp_path / "msg.db"),
        check_same_thread=False,
        isolation_level=None,
    )
    db.row_factory = sqlite3.Row
    db.executescript(
        "PRAGMA journal_mode=WAL;"
        "PRAGMA synchronous=NORMAL;"
    )
    yield db
    db.close()


@pytest.fixture
def queue(conn):
    return OutboundQueue(conn)


# ── Basic enqueue / pick ─────────────────────────────────────────────


def test_enqueue_returns_id(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    assert msg_id is not None
    assert msg_id > 0


def test_pick_next_returns_queued(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    msg = queue.pick_next()
    assert msg is not None
    assert msg.id == msg_id
    assert msg.text == "HELLO"
    assert msg.to_call == "K1ABC"
    assert msg.state == OutboundState.QUEUED
    assert msg.attempts == 0


def test_pick_next_returns_none_when_empty(queue):
    assert queue.pick_next() is None


def test_pick_next_fifo_order(queue):
    """Oldest enqueued should come out first."""
    id1 = queue.enqueue("FIRST", OutboundKind.DIRECTED, to_call="K1ABC")
    time.sleep(0.01)
    id2 = queue.enqueue("SECOND", OutboundKind.DIRECTED, to_call="K1ABC")
    msg = queue.pick_next()
    assert msg.id == id1


def test_pick_next_skips_non_queued(queue):
    id1 = queue.enqueue("FIRST", OutboundKind.DIRECTED, to_call="K1ABC")
    id2 = queue.enqueue("SECOND", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(id1)
    queue.mark_wait_ack(id1)
    # Now pick_next should jump to id2.
    msg = queue.pick_next()
    assert msg.id == id2


# ── Queue depth cap ──────────────────────────────────────────────────


def test_enqueue_caps_at_QUEUE_DEPTH(queue):
    """Once we have QUEUE_DEPTH active rows, further enqueues fail."""
    ids = []
    for i in range(QUEUE_DEPTH):
        ids.append(queue.enqueue(f"M{i}", OutboundKind.DIRECTED, to_call="K1ABC"))
    # All should have succeeded.
    assert all(i is not None for i in ids)
    # Next one is rejected.
    assert queue.enqueue("BLOCKED", OutboundKind.DIRECTED, to_call="K1ABC") is None


def test_delivered_messages_dont_count_toward_cap(queue):
    """Once a message is DELIVERED, the cap should free up."""
    for i in range(QUEUE_DEPTH):
        msg_id = queue.enqueue(f"M{i}", OutboundKind.DIRECTED, to_call="K1ABC")
        queue.mark_delivered(msg_id)
    # All terminal — we should be able to enqueue more.
    assert queue.enqueue("AFTER", OutboundKind.DIRECTED, to_call="K1ABC") is not None


def test_abandoned_messages_dont_count_toward_cap(queue):
    for i in range(QUEUE_DEPTH):
        msg_id = queue.enqueue(f"M{i}", OutboundKind.DIRECTED, to_call="K1ABC")
        queue.mark_abandoned(msg_id, error="test")
    assert queue.enqueue("AFTER", OutboundKind.DIRECTED, to_call="K1ABC") is not None


# ── State transitions ───────────────────────────────────────────────


def test_mark_sending_increments_attempts(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.SENDING
    assert msg.attempts == 1
    assert msg.last_tx_at is not None


def test_mark_sending_sets_last_tx_at(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    before = time.time()
    queue.mark_sending(msg_id)
    after = time.time()
    msg = queue.get(msg_id)
    assert before <= msg.last_tx_at <= after


def test_mark_wait_ack_transitions(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.WAIT_ACK


def test_mark_delivered_transitions(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)
    queue.mark_delivered(msg_id)
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.DELIVERED


def test_mark_retry_keeps_attempts(queue):
    """Retry should NOT decrement attempts (mark_sending bumped it)."""
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    assert queue.get(msg_id).attempts == 1
    queue.mark_retry(msg_id, error="fake failure")
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.QUEUED
    assert msg.attempts == 1
    assert msg.error == "fake failure"


def test_mark_abandoned_records_error(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_abandoned(msg_id, error="3 failed attempts")
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.ABANDONED
    assert msg.error == "3 failed attempts"


# ── ACK matching ────────────────────────────────────────────────────


def test_record_ack_matches_wait_ack_row(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)

    matched_id = queue.record_ack("K1ABC")
    assert matched_id == msg_id
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.DELIVERED


def test_record_ack_case_insensitive(queue):
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)
    assert queue.record_ack("k1abc") == msg_id


def test_record_ack_no_match_returns_none(queue):
    """ACK for a callsign we never sent to should return None."""
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)
    assert queue.record_ack("VE3ABC") is None
    # Original message still WAIT_ACK.
    assert queue.get(msg_id).state == OutboundState.WAIT_ACK


def test_record_ack_no_match_when_no_wait_ack_rows(queue):
    """ACK arriving with no pending message → no-op."""
    assert queue.record_ack("K1ABC") is None


def test_record_ack_picks_most_recent_when_multiple(queue):
    """Multiple WAIT_ACK rows to same call → match the most recent."""
    id1 = queue.enqueue("M1", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(id1)
    queue.mark_wait_ack(id1)
    time.sleep(0.01)
    id2 = queue.enqueue("M2", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(id2)
    queue.mark_wait_ack(id2)

    matched = queue.record_ack("K1ABC")
    # The more recently transmitted one wins.
    assert matched == id2
    # The other stays WAIT_ACK.
    assert queue.get(id1).state == OutboundState.WAIT_ACK


# ── Timeout sweep ───────────────────────────────────────────────────


def test_find_timed_out_acks_returns_only_timed_out(queue):
    """Rows whose last_tx_at is older than ACK_TIMEOUT_S are timed out."""
    id_old = queue.enqueue("OLD", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(id_old)
    queue.mark_wait_ack(id_old)
    # Backdate.
    queue._conn.execute(
        "UPDATE outbound SET last_tx_at=? WHERE id=?",
        (time.time() - ACK_TIMEOUT_S - 10, id_old),
    )

    id_new = queue.enqueue("NEW", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(id_new)
    queue.mark_wait_ack(id_new)

    timed_out = queue.find_timed_out_acks(time.time())
    assert [m.id for m in timed_out] == [id_old]


def test_find_timed_out_acks_ignores_non_wait_ack(queue):
    """Only WAIT_ACK rows are candidates for timeout."""
    msg_id = queue.enqueue("HELLO", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    # Backdate but don't transition to WAIT_ACK.
    queue._conn.execute(
        "UPDATE outbound SET last_tx_at=? WHERE id=?",
        (time.time() - ACK_TIMEOUT_S - 10, msg_id),
    )
    timed_out = queue.find_timed_out_acks(time.time())
    assert timed_out == []


# ── Inspection helpers ───────────────────────────────────────────────


def test_active_count(queue):
    """Count includes QUEUED, SENDING, WAIT_ACK; excludes terminal states."""
    id1 = queue.enqueue("M1", OutboundKind.DIRECTED, to_call="K1ABC")
    id2 = queue.enqueue("M2", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(id2)
    id3 = queue.enqueue("M3", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_delivered(id3)
    # id1 QUEUED + id2 SENDING = 2 active.
    assert queue.active_count() == 2


def test_all_active_returns_non_terminal_rows(queue):
    id1 = queue.enqueue("M1", OutboundKind.DIRECTED, to_call="K1ABC")
    id2 = queue.enqueue("M2", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_delivered(id2)
    rows = queue.all_active()
    assert [r.id for r in rows] == [id1]


def test_all_abandoned(queue):
    msg_id = queue.enqueue("DEAD", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_abandoned(msg_id, error="dead")
    rows = queue.all_abandoned()
    assert [r.id for r in rows] == [msg_id]


# ── Persistence across connection close/reopen ───────────────────────


def test_queue_survives_reconnect(tmp_path: Path):
    """The whole point of SQLite is durability — verify it works."""
    db_path = tmp_path / "outbound.db"
    conn1 = sqlite3.connect(str(db_path), isolation_level=None)
    conn1.row_factory = sqlite3.Row
    q1 = OutboundQueue(conn1)
    msg_id = q1.enqueue("DURABLE", OutboundKind.DIRECTED, to_call="K1ABC")
    conn1.close()

    conn2 = sqlite3.connect(str(db_path), isolation_level=None)
    conn2.row_factory = sqlite3.Row
    q2 = OutboundQueue(conn2)
    msg = q2.get(msg_id)
    assert msg is not None
    assert msg.text == "DURABLE"
    assert msg.state == OutboundState.QUEUED
    conn2.close()


# ── enqueue_for_encoding / pick_next_encoding / mark_encoded ─────────


def test_enqueue_for_encoding_creates_encoding_row(queue):
    """enqueue_for_encoding should create the row in ENCODING state,
    NOT QUEUED. Scheduler ignores ENCODING rows; the encode worker
    picks them up."""
    msg_id = queue.enqueue_for_encoding("HI", OutboundKind.ALLCALL)
    assert msg_id is not None
    msg = queue.get(msg_id)
    assert msg.state is OutboundState.ENCODING
    # Scheduler-side query (pick_next) should NOT see it.
    assert queue.pick_next() is None
    # Worker-side query DOES see it.
    pending = queue.pick_next_encoding()
    assert pending is not None
    assert pending.id == msg_id


def test_pick_next_encoding_fifo_order(queue):
    """When multiple rows are in ENCODING, pick_next_encoding returns
    the oldest first (matches the FIFO contract of pick_next)."""
    id1 = queue.enqueue_for_encoding("FIRST", OutboundKind.ALLCALL)
    time.sleep(0.01)  # ensure distinct enqueued_at
    id2 = queue.enqueue_for_encoding("SECOND", OutboundKind.ALLCALL)

    pending = queue.pick_next_encoding()
    assert pending.id == id1


def test_pick_next_encoding_skips_other_states(queue):
    """pick_next_encoding only sees ENCODING. Rows in QUEUED, SENDING,
    etc. are invisible to the worker."""
    queued_id = queue.enqueue("Q", OutboundKind.ALLCALL)  # legacy: → QUEUED
    encoding_id = queue.enqueue_for_encoding("E", OutboundKind.ALLCALL)

    pending = queue.pick_next_encoding()
    assert pending is not None
    assert pending.id == encoding_id


def test_mark_encoded_transitions_to_queued(queue):
    msg_id = queue.enqueue_for_encoding("HELLO", OutboundKind.ALLCALL)
    queue.mark_encoded(msg_id)
    msg = queue.get(msg_id)
    assert msg.state is OutboundState.QUEUED
    # Now scheduler-side pick should return it.
    picked = queue.pick_next()
    assert picked is not None
    assert picked.id == msg_id


def test_mark_encoded_only_acts_on_encoding_state(queue):
    """mark_encoded shouldn't accidentally bump SENDING or other
    states back to QUEUED. Only the ENCODING → QUEUED transition.
    """
    msg_id = queue.enqueue("HI", OutboundKind.ALLCALL)  # already QUEUED
    # Move it to SENDING.
    queue.mark_sending(msg_id)
    assert queue.get(msg_id).state is OutboundState.SENDING
    # mark_encoded should NOT move SENDING → QUEUED.
    queue.mark_encoded(msg_id)
    assert queue.get(msg_id).state is OutboundState.SENDING


def test_reset_unencoded_to_encoding_resets_queued_rows(queue):
    """After daemon restart with in-memory cache, QUEUED rows have
    lost their cached audio. They need to go back to ENCODING."""
    msg_id = queue.enqueue("HELLO", OutboundKind.ALLCALL)  # → QUEUED
    assert queue.get(msg_id).state is OutboundState.QUEUED

    n_reset = queue.reset_unencoded_to_encoding()
    assert n_reset == 1
    assert queue.get(msg_id).state is OutboundState.ENCODING


def test_reset_unencoded_to_encoding_keeps_encoding_rows(queue):
    """Rows already in ENCODING stay there — the recovery is a no-op
    transition for them. (They might just-not-have-been-encoded-yet
    when the daemon died.)"""
    msg_id = queue.enqueue_for_encoding("HI", OutboundKind.ALLCALL)
    queue.reset_unencoded_to_encoding()
    assert queue.get(msg_id).state is OutboundState.ENCODING


def test_reset_unencoded_to_encoding_ignores_other_states(queue):
    """SENDING, WAIT_ACK, DELIVERED, ABANDONED rows are NOT affected.
    Only ENCODING and QUEUED."""
    sending_id = queue.enqueue("S", OutboundKind.ALLCALL)
    queue.mark_sending(sending_id)
    delivered_id = queue.enqueue("D", OutboundKind.ALLCALL)
    queue.mark_sending(delivered_id)
    queue.mark_delivered(delivered_id)
    queued_id = queue.enqueue("Q", OutboundKind.ALLCALL)

    queue.reset_unencoded_to_encoding()

    # SENDING stays SENDING; DELIVERED stays DELIVERED; QUEUED moves.
    assert queue.get(sending_id).state is OutboundState.SENDING
    assert queue.get(delivered_id).state is OutboundState.DELIVERED
    assert queue.get(queued_id).state is OutboundState.ENCODING


def test_enqueue_depth_includes_encoding_rows(queue):
    """The queue-full check counts ENCODING rows toward the depth
    limit. Otherwise you could OOM the encoder by spamming
    enqueue_for_encoding."""
    from microjs8.tx.queue import QUEUE_DEPTH

    # Fill the queue with ENCODING rows.
    ids = []
    for i in range(QUEUE_DEPTH):
        msg_id = queue.enqueue_for_encoding(f"M{i}", OutboundKind.ALLCALL)
        assert msg_id is not None
        ids.append(msg_id)

    # Next enqueue should fail (queue full).
    overflow = queue.enqueue_for_encoding("OVERFLOW", OutboundKind.ALLCALL)
    assert overflow is None
    # Same for legacy enqueue path.
    overflow2 = queue.enqueue("OVERFLOW", OutboundKind.ALLCALL)
    assert overflow2 is None
