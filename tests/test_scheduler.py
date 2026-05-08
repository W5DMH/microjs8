"""Tests for minijs8.tx.scheduler.TxScheduler.

We never actually run the scheduler thread loop in tests — instead we
call ``_tick()`` directly. That keeps tests fast, deterministic, and
free of slot-timing flakiness.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from minijs8.tx.queue import (
    ACK_TIMEOUT_S,
    MAX_ATTEMPTS,
    OutboundKind,
    OutboundQueue,
    OutboundState,
)
from minijs8.tx.scheduler import TxScheduler
from minijs8.tx.tx_backend import FakeTxBackend, TxResult


@pytest.fixture
def conn(tmp_path: Path):
    db = sqlite3.connect(
        str(tmp_path / "msg.db"),
        check_same_thread=False,
        isolation_level=None,
    )
    db.row_factory = sqlite3.Row
    yield db
    db.close()


@pytest.fixture
def queue(conn):
    return OutboundQueue(conn)


class _AlwaysAllowGate:
    def check_can_transmit(self):
        return True, None


class _AlwaysBlockGate:
    def __init__(self, reason: str = "test block"):
        self.reason = reason

    def check_can_transmit(self):
        return False, self.reason


def _make_sched(queue, backend, gate=None, **kwargs):
    """Construct a scheduler ready for direct tick calls."""
    return TxScheduler(
        queue=queue,
        backend=backend,
        safety_gate=gate or _AlwaysAllowGate(),
        **kwargs,
    )


# ── Empty queue ──────────────────────────────────────────────────────


def test_tick_no_messages_does_nothing(queue):
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    assert backend.transmissions == []
    assert backend.encoded_for == []
    assert backend.audio_played == []


# ── Successful TX (broadcast vs directed) ────────────────────────────


def test_tick_successful_broadcast_transitions_to_delivered(queue):
    msg_id = queue.enqueue(
        "K1ABC: @HB HEARTBEAT FN42",
        OutboundKind.HEARTBEAT,
        to_call=None,
    )
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()

    # Scheduler uses encode() + transmit_audio() per frame. For a
    # single-frame heartbeat: 1 encode call, 1 audio buffer played.
    assert backend.encoded_for == ["K1ABC: @HB HEARTBEAT FN42"]
    assert len(backend.audio_played) == 1
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.DELIVERED


def test_tick_successful_cq_goes_to_delivered(queue):
    msg_id = queue.enqueue("K1ABC: CQ FN42", OutboundKind.CQ, to_call=None)
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    assert queue.get(msg_id).state == OutboundState.DELIVERED


def test_tick_successful_allcall_goes_to_delivered(queue):
    msg_id = queue.enqueue(
        "K1ABC: @ALLCALL HELLO", OutboundKind.ALLCALL, to_call=None,
    )
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    assert queue.get(msg_id).state == OutboundState.DELIVERED


def test_tick_successful_directed_goes_to_wait_ack(queue):
    msg_id = queue.enqueue(
        "K8XYZ MSG HELLO", OutboundKind.DIRECTED, to_call="K8XYZ",
    )
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    assert queue.get(msg_id).state == OutboundState.WAIT_ACK


def test_tick_successful_reply_goes_straight_to_delivered(queue):
    """REPLY-kind messages (auto-ACKs, QUERY MSGS notifications) MUST
    transition SENDING→DELIVERED on TX completion, not SENDING→WAIT_ACK.

    Regression guard for the on-air loop bug: an outbound auto-ACK was
    queued with kind=DIRECTED, the scheduler put it in WAIT_ACK, no ACK
    came back from the recipient (correctly — JS8Call doesn't ACK an
    ACK), and we retransmitted the ACK every 90s forever.

    If this test ever fails, somebody re-introduced the loop. Look at
    the broadcast-skip tuple in scheduler.py around the SENDING→
    DELIVERED-vs-WAIT_ACK branch.
    """
    msg_id = queue.enqueue(
        "K1ABC: K8XYZ ACK", OutboundKind.REPLY, to_call="K8XYZ",
    )
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    assert queue.get(msg_id).state == OutboundState.DELIVERED, (
        "REPLY-kind message ended in WAIT_ACK — the on-air ACK loop is "
        "back. Add OutboundKind.REPLY to the broadcast-skip tuple in "
        "tx/scheduler.py"
    )


def test_tick_reply_kind_does_not_wait_for_ack(queue):
    """Companion to the above: a REPLY-kind TX must not be retried
    after the ACK timeout, because we never expected an ACK in the
    first place. The state must stay DELIVERED (not transition back
    to QUEUED for retry)."""
    msg_id = queue.enqueue(
        "K1ABC: K8XYZ NO", OutboundKind.REPLY, to_call="K8XYZ",
    )
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    # Run a second tick — would expose buggy retries if state were WAIT_ACK
    # and the ACK timeout had elapsed (we can't easily fast-forward time
    # here, so we rely on state-stays-DELIVERED as the principal guard).
    sched._tick()
    assert queue.get(msg_id).state == OutboundState.DELIVERED
    # And exactly one transmission ever happened.
    assert len(backend.audio_played) == 1


def test_canary_outbound_query_msgs_with_kind_none_does_not_loop(queue):
    """End-to-end regression test for the on-air QUERY MSGS loop.

    Reproduces the exact scenario from the bench-test log: somebody
    queues an outbound "<call> QUERY MSGS" without specifying a kind.
    With auto-classification active in enqueue, the kind comes out
    as REPLY, and the scheduler transitions SENDING→DELIVERED on TX
    completion — no WAIT_ACK, no retry, no abandoned-after-3-attempts.

    If this test fails, the loop is back. The fix is to keep
    ``infer_outbound_kind`` honest about which verbs aren't ACKed by
    the recipient.
    """
    # Simulate the exact text shape from the on-air log, with NO
    # explicit kind — exactly like a manual tool, REPL session, or
    # future Compose UI would do.
    msg_id = queue.enqueue("KD8PGB QUERY MSGS", to_call="KD8PGB")
    msg = queue.get(msg_id)
    assert msg.kind == OutboundKind.REPLY, (
        f"Auto-classified as {msg.kind} — should be REPLY to avoid "
        f"the WAIT_ACK retransmit loop"
    )

    # Run the scheduler — it should TX once and mark delivered.
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    assert queue.get(msg_id).state == OutboundState.DELIVERED, (
        "Outbound QUERY MSGS ended in WAIT_ACK — the on-air loop is back"
    )

    # Run more ticks — there must be no retry or retransmit. This
    # catches a bug where the scheduler somehow picks the row up again
    # after marking DELIVERED.
    for _ in range(3):
        sched._tick()
    assert queue.get(msg_id).state == OutboundState.DELIVERED
    assert len(backend.audio_played) == 1, "outbound query was retransmitted"


def test_outbound_msg_with_kind_none_still_uses_directed(queue):
    """Counterpart canary: a REAL MSG (mail content delivery) must
    still be classified as DIRECTED so we wait for the recipient's
    auto-ACK and only mark DELIVERED on inbound ACK match.

    If the classifier ever over-defaults to REPLY, we'd lose the
    delivery-confirmation guarantee for actual mail."""
    msg_id = queue.enqueue("K1ABC MSG HELLO MIKE", to_call="K1ABC")
    msg = queue.get(msg_id)
    assert msg.kind == OutboundKind.DIRECTED, (
        f"Outbound MSG should be DIRECTED (recipient auto-ACKs), "
        f"got {msg.kind}"
    )

    # Scheduler must put this in WAIT_ACK (not auto-deliver).
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    assert queue.get(msg_id).state == OutboundState.WAIT_ACK


# ── TX failure → retry ───────────────────────────────────────────────


def test_tick_tx_failure_first_attempt_requeues(queue):
    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    backend = FakeTxBackend(
        return_value=TxResult(success=False, message="cat down"),
    )
    sched = _make_sched(queue, backend)
    sched._tick()
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.QUEUED  # re-queued
    assert msg.attempts == 1
    assert "cat down" in msg.error


def test_tick_tx_failure_max_attempts_abandons(queue):
    """After MAX_ATTEMPTS failures, the message is abandoned."""
    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    backend = FakeTxBackend(
        return_value=TxResult(success=False, message="boom"),
    )
    sched = _make_sched(queue, backend)
    for _ in range(MAX_ATTEMPTS):
        sched._tick()
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.ABANDONED
    assert msg.attempts == MAX_ATTEMPTS


# ── ACK timeout → retry FSM ──────────────────────────────────────────


def test_tick_processes_ack_timeout_requeue(queue):
    """A WAIT_ACK row whose timeout has elapsed should be re-queued
    if attempts remain. The retry TX happens on the NEXT tick (not
    the same tick that swept the timeout), so encode latency doesn't
    blow the slot-alignment budget."""
    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)
    # Backdate to force timeout.
    queue._conn.execute(
        "UPDATE outbound SET last_tx_at=? WHERE id=?",
        (time.time() - ACK_TIMEOUT_S - 1, msg_id),
    )

    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)

    # First tick: timeout sweep transitions WAIT_ACK→QUEUED. The row
    # is in skip_ids for this tick so no TX yet — sweep + DB UPDATE
    # on this tick, encode + start_burst on the next tick.
    sched._tick()
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.QUEUED, (
        f"after first tick should be QUEUED (deferred), "
        f"got {msg.state}"
    )
    assert msg.attempts == 1, (
        f"sweep doesn't bump attempts; that happens at TX time. "
        f"got attempts={msg.attempts}"
    )
    assert backend.audio_played == [], (
        "no TX should have happened on the timeout-sweep tick"
    )

    # Second tick: row gets picked up normally and transmitted.
    sched._tick()
    msg = queue.get(msg_id)
    assert msg.attempts == 2  # incremented by mark_sending on retry
    assert msg.state == OutboundState.WAIT_ACK
    assert len(backend.audio_played) == 1


def test_tick_processes_ack_timeout_abandons_at_max(queue):
    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)
    # Force attempts to MAX.
    queue._conn.execute(
        "UPDATE outbound SET attempts=?, last_tx_at=? WHERE id=?",
        (MAX_ATTEMPTS, time.time() - ACK_TIMEOUT_S - 1, msg_id),
    )

    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.ABANDONED
    assert backend.transmissions == []  # never re-transmitted
    assert backend.audio_played == []


def test_ack_timeout_retry_deferred_to_next_slot(queue):
    """When a row times out and gets requeued for retry, the retry
    encode + start_burst happens on the FOLLOWING tick, not the
    same tick that did the WAIT_ACK→QUEUED transition.

    Why: doing both in one tick on a Pi Zero 2W blew our 500 ms
    slot-alignment budget — measured at 250-560 ms in production.
    The DB UPDATE for mark_retry plus the gfsk8 encode of 3 frames
    is expensive enough that on a slow tick we couldn't make
    transmit_frame's wall-clock-aligned silence pad in time.
    Splitting the work over two ticks gives us the full ~14 seconds
    between to do all the encoding work.

    The defer is per-tick, so picking up on the next slot is the
    expected behavior.
    """
    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(msg_id)
    queue.mark_wait_ack(msg_id)
    queue._conn.execute(
        "UPDATE outbound SET last_tx_at=? WHERE id=?",
        (time.time() - ACK_TIMEOUT_S - 1, msg_id),
    )

    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)

    # Tick A: timeout sweep transitions WAIT_ACK→QUEUED. The
    # freshly_retried set contains this row's ID, so _pick_for_this_slot
    # SKIPS it. No TX work happens on this tick — no encode, no
    # start_burst, nothing.
    sched._tick()
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.QUEUED
    assert backend.encoded_for == [], (
        "encode should NOT happen on the timeout-sweep tick"
    )
    assert backend.burst_events == [], (
        "no burst should start on the timeout-sweep tick"
    )

    # Tick B: row is QUEUED but NOT in skip_ids. Picked normally,
    # encode + start_burst happen.
    sched._tick()
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.WAIT_ACK
    assert backend.encoded_for == ["K1ABC MSG HI"], (
        f"expected encode of 'K1ABC MSG HI', got {backend.encoded_for!r}"
    )
    assert "start" in backend.burst_events
    assert "end" in backend.burst_events
    assert len(backend.audio_played) == 1


def test_two_independent_timeouts_only_one_defers_per_tick(queue):
    """If two rows time out in the same tick, BOTH go to the
    skip set. Neither transmits this tick. Both are eligible
    next tick (priority order: queue order, just like normal).

    Defensive: this case probably won't happen in practice because
    we only allow ONE outbound message in flight at a time, but
    the data structure should handle it cleanly.
    """
    a = queue.enqueue("K1AAA MSG HI", OutboundKind.DIRECTED, to_call="K1AAA")
    b = queue.enqueue("K1BBB MSG HI", OutboundKind.DIRECTED, to_call="K1BBB")
    for mid in (a, b):
        queue.mark_sending(mid)
        queue.mark_wait_ack(mid)
    queue._conn.execute(
        "UPDATE outbound SET last_tx_at=? WHERE state=?",
        (time.time() - ACK_TIMEOUT_S - 1, OutboundState.WAIT_ACK.value),
    )

    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    # Both got requeued, neither was TX'd this tick.
    assert queue.get(a).state == OutboundState.QUEUED
    assert queue.get(b).state == OutboundState.QUEUED
    assert backend.encoded_for == []


# ── Safety gate ──────────────────────────────────────────────────────


def test_tick_safety_gate_blocks_tx(queue):
    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend, gate=_AlwaysBlockGate("no GPS fix"))
    sched._tick()
    # Message stays QUEUED, no TX attempted.
    assert queue.get(msg_id).state == OutboundState.QUEUED
    assert backend.transmissions == []
    assert backend.audio_played == []
    assert sched.status.last_blocked_reason == "no GPS fix"


def test_tick_safety_gate_does_not_increment_attempts(queue):
    """Blocked TX shouldn't burn an attempt — the operator may have
    fixed the issue (plugged in GPS) by the next slot."""
    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend, gate=_AlwaysBlockGate())
    sched._tick()
    sched._tick()
    sched._tick()
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.QUEUED
    assert msg.attempts == 0


# ── FIFO across mixed kinds ──────────────────────────────────────────


def test_tick_fifo_across_mixed_kinds(queue):
    id1 = queue.enqueue("FIRST", OutboundKind.DIRECTED, to_call="K1ABC")
    time.sleep(0.01)
    id2 = queue.enqueue("SECOND", OutboundKind.HEARTBEAT, to_call=None)
    time.sleep(0.01)
    id3 = queue.enqueue("THIRD", OutboundKind.DIRECTED, to_call="K1ABC")

    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    sched._tick()
    sched._tick()

    # FakeTxBackend.encode() is called once per message-pickup.
    # Order matches the FIFO order we enqueued.
    assert backend.encoded_for == ["FIRST", "SECOND", "THIRD"]


# ── Status callbacks ────────────────────────────────────────────────


def test_tick_invokes_on_tx_complete(queue):
    msg_id = queue.enqueue("HI", OutboundKind.HEARTBEAT, to_call=None)
    backend = FakeTxBackend()
    seen = []
    sched = _make_sched(
        queue, backend, on_tx_complete=lambda kind, result: seen.append((kind, result.success)),
    )
    sched._tick()
    assert seen == [(OutboundKind.HEARTBEAT, True)]


def test_tick_status_reflects_active_count(queue):
    queue.enqueue("K1ABC MSG M1", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.enqueue("K1ABC MSG M2", OutboundKind.DIRECTED, to_call="K1ABC")
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()
    # After processing one, M2 still active, M1 in WAIT_ACK.
    assert sched.status.queue_active == 2


# ── Slot timing math ─────────────────────────────────────────────────


def test_seconds_until_next_preflight_at_slot_boundary():
    """When time is exactly on a slot boundary, we sleep ≈ slot_seconds
    + the target offset. We wake just after the NEXT slot boundary so
    transmit_frame's wall-clock alignment sees mstr ≈ target_offset
    + queue-work-and-settle and pads silence to land modulation at
    slot+500ms."""
    from minijs8.tx.scheduler import _TARGET_OFFSET_S
    backend = FakeTxBackend()
    sched = TxScheduler(
        queue=None,  # type: ignore[arg-type]
        backend=backend,
        safety_gate=_AlwaysAllowGate(),
        slot_seconds=15,
    )
    # Force time to land on a slot boundary.
    sched._now = lambda: 1005.0  # multiple of 15
    sleep_s = sched._seconds_until_next_preflight()
    # past = 0, until_boundary = 15.0, target = 15.0 + offset.
    expected = 15.0 + _TARGET_OFFSET_S
    assert abs(sleep_s - expected) < 0.001, (
        f"expected {expected:.3f}s, got {sleep_s:.3f}s"
    )


def test_seconds_until_next_preflight_just_before_boundary():
    """When time is just before a slot boundary, sleep is small —
    we wake just AFTER the upcoming boundary."""
    from minijs8.tx.scheduler import _TARGET_OFFSET_S
    backend = FakeTxBackend()
    sched = TxScheduler(
        queue=None,  # type: ignore[arg-type]
        backend=backend,
        safety_gate=_AlwaysAllowGate(),
        slot_seconds=15,
    )
    # 14.95 → 50 ms before next slot boundary (at 15.0).
    # past = 14.95, until_boundary = 0.05, target = 0.05 + offset.
    sched._now = lambda: 14.95
    sleep_s = sched._seconds_until_next_preflight()
    expected = 0.05 + _TARGET_OFFSET_S
    assert abs(sleep_s - expected) < 0.001, (
        f"expected {expected:.3f}s, got {sleep_s:.3f}s"
    )


def test_seconds_until_next_preflight_just_after_boundary():
    """When time is just past a slot boundary, sleep is almost a full
    slot — we wake at the NEXT boundary + offset, not this one."""
    from minijs8.tx.scheduler import _TARGET_OFFSET_S
    backend = FakeTxBackend()
    sched = TxScheduler(
        queue=None,  # type: ignore[arg-type]
        backend=backend,
        safety_gate=_AlwaysAllowGate(),
        slot_seconds=15,
    )
    # 1005.05 — 50 ms past the slot boundary at 1005.0.
    # past = 0.05, until_boundary = 14.95, target = 14.95 + offset.
    sched._now = lambda: 1005.05
    sleep_s = sched._seconds_until_next_preflight()
    expected = 14.95 + _TARGET_OFFSET_S
    assert abs(sleep_s - expected) < 0.001, (
        f"expected {expected:.3f}s, got {sleep_s:.3f}s"
    )


# ── Multi-frame TX (Phase 2) ─────────────────────────────────────────


def test_multi_frame_two_frame_message_takes_two_ticks(queue):
    """A 2-frame message TXes one frame per tick. Row stays SENDING
    after frame 0, transitions to DELIVERED after frame 1."""
    msg_id = queue.enqueue(
        "@ALLCALL TESTING MULTIFRAME",
        OutboundKind.ALLCALL,
        to_call=None,
    )
    backend = FakeTxBackend(frames_per_msg=2)
    sched = _make_sched(queue, backend)

    # Tick 1: encode all frames, mark SENDING, TX frame 0.
    sched._tick()
    assert backend.encoded_for == ["@ALLCALL TESTING MULTIFRAME"]
    assert len(backend.audio_played) == 1
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.SENDING

    # Tick 2: TX frame 1 (last). Row goes to DELIVERED.
    sched._tick()
    # encode() not called again — encoded once at pickup.
    assert backend.encoded_for == ["@ALLCALL TESTING MULTIFRAME"]
    assert len(backend.audio_played) == 2
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.DELIVERED


def test_multi_frame_three_frame_directed_takes_three_ticks(queue):
    """A 3-frame directed message TXes 3 frames over 3 ticks; goes
    to WAIT_ACK (not DELIVERED) after the final frame because
    directed messages need an ACK."""
    msg_id = queue.enqueue(
        "KN4CRD MSG HELLO",
        OutboundKind.DIRECTED,
        to_call="KN4CRD",
    )
    backend = FakeTxBackend(frames_per_msg=3)
    sched = _make_sched(queue, backend)

    sched._tick()  # frame 0
    assert queue.get(msg_id).state == OutboundState.SENDING
    sched._tick()  # frame 1
    assert queue.get(msg_id).state == OutboundState.SENDING
    sched._tick()  # frame 2 (last)
    assert len(backend.audio_played) == 3
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.WAIT_ACK


def test_multi_frame_burst_keeps_ptt_keyed_across_slots(queue):
    """The defining property of the new multi-frame design: PTT is
    keyed ONCE at the start of a burst and held across all frames
    until the final frame completes. This is what JS8Call does and
    what receivers expect — one continuous TX from key-on to key-off.

    We verify by counting burst lifecycle events: exactly one "start"
    and one "end" should be recorded for a 3-frame message, even
    though it spans 3 separate scheduler ticks."""
    msg_id = queue.enqueue(
        "@ALLCALL THREE FRAME MULTIFRAME TEST",
        OutboundKind.ALLCALL,
        to_call=None,
    )
    backend = FakeTxBackend(frames_per_msg=3)
    sched = _make_sched(queue, backend)

    # Tick 1: encode + start_burst + transmit_frame(0). One "start".
    sched._tick()
    assert backend.burst_events == ["start"], (
        f"after frame 0: expected ['start'], got {backend.burst_events!r}"
    )

    # Tick 2: transmit_frame(1) — burst still active, no new events.
    sched._tick()
    assert backend.burst_events == ["start"], (
        f"after frame 1: should still be just ['start'] "
        f"(PTT stays keyed), got {backend.burst_events!r}"
    )

    # Tick 3: transmit_frame(2) + end_burst. Now we see "end".
    sched._tick()
    assert backend.burst_events == ["start", "end"], (
        f"after frame 2: expected ['start', 'end'], "
        f"got {backend.burst_events!r}"
    )

    msg = queue.get(msg_id)
    assert msg.state == OutboundState.DELIVERED
    assert len(backend.audio_played) == 3


def test_multi_frame_burst_releases_ptt_on_failure(queue):
    """When a frame fails mid-burst, end_burst() runs to release PTT.
    Critical safety: we never leave the radio keyed after a failure."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )

    class _FailMidBackend(FakeTxBackend):
        def transmit_frame(self, audio):
            super().transmit_frame(audio)
            n = len(self.audio_played)
            if n == 2:
                return TxResult(success=False, message="simulated glitch")
            return TxResult(success=True, message="ok",
                            tx_started_at=time.time())

    backend = _FailMidBackend(frames_per_msg=3)
    sched = _make_sched(queue, backend)

    sched._tick()  # frame 0 OK
    assert backend.burst_events == ["start"]
    sched._tick()  # frame 1 FAIL → end_burst runs
    assert backend.burst_events == ["start", "end"], (
        "burst MUST end (PTT released) even on failure; "
        f"got {backend.burst_events!r}"
    )
    # Row went back to QUEUED for retry.
    assert queue.get(msg_id).state == OutboundState.QUEUED


def test_multi_frame_burst_start_failure_no_end_needed(queue):
    """If start_burst() fails (CAT down, ptt_on returns False), no
    end_burst() is needed because PTT was never keyed. Row goes
    through retry FSM."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )
    backend = FakeTxBackend(
        frames_per_msg=2, start_burst_fails=True,
    )
    sched = _make_sched(queue, backend)

    sched._tick()
    # start was called but burst_active was never set → no "end" event.
    assert backend.burst_events == ["start"], (
        f"expected just ['start'] (failed), "
        f"got {backend.burst_events!r}"
    )
    # No frames were played.
    assert len(backend.audio_played) == 0
    # Row went to retry.
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.QUEUED
    assert msg.attempts == 1
    assert "fake start_burst failure" in (msg.error or "")


# ── Burst watchdog ───────────────────────────────────────────────────


def test_burst_watchdog_deadline_set_at_burst_start(queue):
    """When a multi-frame burst starts, the watchdog deadline is set
    to ``n_frames × _BURST_WATCHDOG_PER_FRAME_S`` seconds in the
    future. We check the formula with both 1- and 3-frame messages."""
    from minijs8.tx.scheduler import _BURST_WATCHDOG_PER_FRAME_S

    # 3-frame message
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )
    backend = FakeTxBackend(frames_per_msg=3)
    sched = _make_sched(queue, backend)

    # Pin "now" so we can compute the expected deadline deterministically.
    fixed_now = 1_700_000_000.0
    sched._now = lambda: fixed_now

    sched._tick()  # frame 0 fires; ctx created
    ctx = sched._mf_state[msg_id]
    expected_deadline = fixed_now + 3 * _BURST_WATCHDOG_PER_FRAME_S
    assert abs(ctx.burst_deadline - expected_deadline) < 0.001, (
        f"expected deadline {expected_deadline}, got {ctx.burst_deadline}"
    )


def test_burst_watchdog_fires_on_overshoot_abandons_message(queue):
    """If a burst is still active past its watchdog deadline, the
    next tick force-ends it, abandons the row, and logs loudly."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )
    # Use 3 frames so the burst is "in progress" after one tick.
    backend = FakeTxBackend(frames_per_msg=3)
    sched = _make_sched(queue, backend)

    # First tick: frame 0 fires, burst context created.
    sched._tick()
    assert msg_id in sched._mf_state
    assert backend.burst_events == ["start"]
    assert queue.get(msg_id).state == OutboundState.SENDING

    # Simulate "much later" — far past the watchdog deadline. The
    # actual deadline is ~now+60s; jump 100s ahead.
    original_now = sched._now
    deadline = sched._mf_state[msg_id].burst_deadline
    sched._now = lambda: deadline + 100.0  # 100s past deadline

    # Next tick: watchdog fires.
    sched._tick()

    # State after watchdog:
    # - end_burst was called → burst_events has "end"
    assert "end" in backend.burst_events, (
        f"watchdog must end_burst(); got {backend.burst_events!r}"
    )
    # - in-memory state cleared
    assert msg_id not in sched._mf_state
    # - row ABANDONED (not retried)
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.ABANDONED, (
        f"watchdog must abandon, not retry; row is {msg.state}"
    )
    # - error message is descriptive
    assert "watchdog" in (msg.error or "").lower()
    assert "abandoned" in (msg.error or "").lower()


def test_burst_watchdog_does_not_fire_during_normal_burst(queue):
    """During a normal multi-frame burst, the watchdog does NOT fire
    — we should be well under the deadline. This test runs a 3-frame
    burst end-to-end and verifies no watchdog action."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )
    backend = FakeTxBackend(frames_per_msg=3)
    sched = _make_sched(queue, backend)

    sched._tick()  # frame 0
    sched._tick()  # frame 1
    sched._tick()  # frame 2 + end_burst

    # Burst completed normally — exactly one start, one end.
    assert backend.burst_events == ["start", "end"], (
        f"expected ['start', 'end'], got {backend.burst_events!r}"
    )
    assert queue.get(msg_id).state == OutboundState.DELIVERED
    # State cleared.
    assert msg_id not in sched._mf_state


def test_burst_watchdog_fires_even_when_end_burst_raises(queue):
    """Defensive: if end_burst() raises during watchdog cleanup, we
    still abandon the row and clear in-memory state. We can't
    guarantee PTT was actually released (we logged the failure), but
    the queue and scheduler state stay consistent."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )

    class _BackendWhereEndBurstRaises(FakeTxBackend):
        def end_burst(self):
            super().end_burst()
            raise RuntimeError("simulated CAT failure during cleanup")

    backend = _BackendWhereEndBurstRaises(frames_per_msg=3)
    sched = _make_sched(queue, backend)
    sched._tick()  # frame 0

    deadline = sched._mf_state[msg_id].burst_deadline
    sched._now = lambda: deadline + 50.0

    # Watchdog tick — end_burst raises but we should still clean up.
    sched._tick()

    # Row was abandoned despite the cleanup error.
    assert queue.get(msg_id).state == OutboundState.ABANDONED
    # In-memory state cleared.
    assert msg_id not in sched._mf_state


def test_burst_hold_seconds_set_before_start_burst(queue):
    """The scheduler tells the CAT layer (via the backend) how long
    the burst will hold PTT, BEFORE keying. The value is the
    scheduler watchdog deadline plus CAT grace, so the CAT-layer
    safety net only fires if the scheduler itself is stuck."""
    from minijs8.tx.scheduler import (
        _BURST_WATCHDOG_PER_FRAME_S,
        _CAT_WATCHDOG_GRACE_S,
    )
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )
    backend = FakeTxBackend(frames_per_msg=3)
    sched = _make_sched(queue, backend)

    sched._tick()  # frame 0 fires (start_burst happens here)

    # Backend recorded ONE call to set_burst_hold_seconds before
    # start_burst. The value is n_frames * 20 + 5.
    expected = 3 * _BURST_WATCHDOG_PER_FRAME_S + _CAT_WATCHDOG_GRACE_S
    assert backend.burst_hold_seconds_calls == [expected], (
        f"expected [{expected}], got {backend.burst_hold_seconds_calls!r}"
    )
    # Verify ordering: set_burst_hold called BEFORE start_burst.
    # We can't check timestamps cheaply, but we can check that
    # set_burst_hold was called and the burst started successfully.
    assert "start" in backend.burst_events
    # Row went to SENDING (in-progress).
    assert queue.get(msg_id).state == OutboundState.SENDING


def test_burst_hold_seconds_cleared_on_start_burst_failure(queue):
    """If start_burst() fails, we should clear the override (call
    set_burst_hold_seconds(0)) so a stale value doesn't affect the
    next TX path. Otherwise: we set 60s for a 3-frame message that
    failed, then a single-frame heartbeat queues — without clearing,
    the watchdog cap would still be 60s for that heartbeat."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )
    backend = FakeTxBackend(
        frames_per_msg=3, start_burst_fails=True,
    )
    sched = _make_sched(queue, backend)
    sched._tick()

    # Should have called set_burst_hold_seconds twice:
    # first to set the override, second (with 0) to clear it.
    assert len(backend.burst_hold_seconds_calls) == 2, (
        f"expected 2 calls (set + clear), "
        f"got {backend.burst_hold_seconds_calls!r}"
    )
    assert backend.burst_hold_seconds_calls[0] > 0  # set
    assert backend.burst_hold_seconds_calls[1] == 0  # cleared


def test_multi_frame_takes_priority_over_new_queued(queue):
    """Once a multi-frame TX is in progress, a freshly enqueued
    message must not jump in front of it. Continuation frames
    have priority."""
    id1 = queue.enqueue("FIRST MULTIFRAME", OutboundKind.ALLCALL, to_call=None)
    backend = FakeTxBackend(frames_per_msg=2)
    sched = _make_sched(queue, backend)

    # Frame 0 of msg 1 fires. Row goes SENDING.
    sched._tick()
    assert queue.get(id1).state == OutboundState.SENDING

    # Now enqueue a second message during the multi-frame TX.
    time.sleep(0.01)
    id2 = queue.enqueue("SECOND MESSAGE", OutboundKind.HEARTBEAT, to_call=None)

    # Tick 2 should TX frame 1 of msg 1 (continuation), NOT msg 2.
    sched._tick()
    assert queue.get(id1).state == OutboundState.DELIVERED
    assert queue.get(id2).state == OutboundState.QUEUED  # still waiting

    # Encode order so far: only msg 1.
    assert backend.encoded_for == ["FIRST MULTIFRAME"]

    # Tick 3 picks up msg 2 (encodes it now). Since FakeTxBackend's
    # frames_per_msg=2, msg 2 also takes 2 ticks.
    sched._tick()
    assert queue.get(id2).state == OutboundState.SENDING
    sched._tick()
    assert queue.get(id2).state == OutboundState.DELIVERED
    # Encode was called twice — once per message, NOT once per frame.
    assert backend.encoded_for == ["FIRST MULTIFRAME", "SECOND MESSAGE"]


def test_multi_frame_safety_gate_skipped_mid_burst(queue):
    """The safety gate is checked at the START of a burst only — once
    we've keyed PTT and TX'd frame 0, mid-burst frames bypass the
    gate. Dropping PTT mid-message because of a transient gate
    failure (chrony losing sync briefly, etc.) is worse than
    completing the message: the receiver gets an undecodable partial
    TX in either case, but with mid-burst-abort the radio also has
    a half-finished keying cycle.
    """

    class _GateThatBlocksLater:
        def __init__(self):
            self.calls = 0

        def check_can_transmit(self):
            self.calls += 1
            # Allow first call (frame 0 / burst start). Block second.
            if self.calls == 1:
                return True, None
            return False, "GPS lost"

    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )
    backend = FakeTxBackend(frames_per_msg=2)
    gate = _GateThatBlocksLater()
    sched = _make_sched(queue, backend, gate=gate)

    sched._tick()  # frame 0 fires; gate consulted (allowed)
    assert queue.get(msg_id).state == OutboundState.SENDING
    assert len(backend.audio_played) == 1

    sched._tick()  # frame 1 fires; gate NOT consulted
    msg = queue.get(msg_id)
    # Message DELIVERED — gate was bypassed mid-burst.
    assert msg.state == OutboundState.DELIVERED, (
        f"expected DELIVERED, got {msg.state}"
    )
    assert len(backend.audio_played) == 2
    # Gate was called exactly ONCE (at burst start), not twice.
    assert gate.calls == 1, (
        f"expected gate consulted once at burst start, "
        f"got {gate.calls} calls"
    )
    # Burst lifecycle: one start, one end (PTT keyed across both frames).
    assert backend.burst_events == ["start", "end"]


def test_multi_frame_tx_failure_aborts_and_retries(queue):
    """If transmit_frame() fails on a middle frame, the WHOLE message
    is queued for retry from frame 0 (not from the failed frame).
    Receivers can't decode partial messages."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )

    # Custom backend: succeeds on first transmit_frame, fails on second.
    class _FailMidBackend(FakeTxBackend):
        def transmit_frame(self, audio):
            super().transmit_frame(audio)  # records but ignores result
            n = len(self.audio_played)
            if n == 2:
                return TxResult(success=False, message="audio glitch")
            return TxResult(success=True, message="ok",
                            tx_started_at=time.time())

    backend = _FailMidBackend(frames_per_msg=2)
    sched = _make_sched(queue, backend)

    sched._tick()  # frame 0 OK
    assert queue.get(msg_id).state == OutboundState.SENDING
    sched._tick()  # frame 1 fails
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.QUEUED  # retry
    assert msg.attempts == 1
    assert "audio glitch" in (msg.error or "")
    # Burst was ended cleanly even on failure (PTT released).
    assert backend.burst_events == ["start", "end"]


def test_multi_frame_retry_restarts_from_frame_zero(queue):
    """After a multi-frame failure, retry encodes again from scratch
    and starts at frame 0 — not frame N where it failed."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )

    fail_count = [0]

    class _FailFirstAttemptBackend(FakeTxBackend):
        def transmit_frame(self, audio):
            super().transmit_frame(audio)
            n = len(self.audio_played)
            # Fail on the second frame of the FIRST attempt only.
            if n == 2 and fail_count[0] == 0:
                fail_count[0] += 1
                return TxResult(success=False, message="boom")
            return TxResult(success=True, message="ok",
                            tx_started_at=time.time())

    backend = _FailFirstAttemptBackend(frames_per_msg=2)
    sched = _make_sched(queue, backend)

    sched._tick()  # attempt 1, frame 0 OK
    sched._tick()  # attempt 1, frame 1 FAIL → retry
    assert queue.get(msg_id).state == OutboundState.QUEUED

    sched._tick()  # attempt 2, frame 0 (restarting from frame 0)
    sched._tick()  # attempt 2, frame 1 OK
    assert queue.get(msg_id).state == OutboundState.DELIVERED

    # encode() was called twice — once per attempt, both fresh.
    assert backend.encoded_for == ["@ALLCALL MULTI", "@ALLCALL MULTI"]
    # Total audio played: 2 frames (attempt 1) + 2 frames (attempt 2) = 4.
    assert len(backend.audio_played) == 4


def test_multi_frame_max_attempts_abandons_message(queue):
    """After MAX_ATTEMPTS failed multi-frame attempts, the message
    is abandoned with the most recent failure error."""
    msg_id = queue.enqueue(
        "@ALLCALL MULTI", OutboundKind.ALLCALL, to_call=None,
    )

    class _AlwaysFailMidBackend(FakeTxBackend):
        def transmit_frame(self, audio):
            super().transmit_frame(audio)
            # Fail on every second frame.
            n = len(self.audio_played)
            if n % 2 == 0:
                return TxResult(success=False, message="always fails")
            return TxResult(success=True, message="ok",
                            tx_started_at=time.time())

    backend = _AlwaysFailMidBackend(frames_per_msg=2)
    sched = _make_sched(queue, backend)

    # Each attempt: tick (frame 0 OK), tick (frame 1 FAIL).
    # Run MAX_ATTEMPTS attempts.
    for _ in range(MAX_ATTEMPTS):
        sched._tick()  # frame 0
        sched._tick()  # frame 1 fail → retry or abandon

    msg = queue.get(msg_id)
    assert msg.state == OutboundState.ABANDONED
    assert msg.attempts == MAX_ATTEMPTS


def test_multi_frame_encode_failure_abandons_immediately(queue):
    """If encode() raises (e.g. bad message format, no identity),
    the message is marked SENDING then immediately ABANDONED. We
    don't retry encode failures — they don't fix themselves."""
    from minijs8.modem.encoder import EncoderError

    msg_id = queue.enqueue("BAD", OutboundKind.ALLCALL, to_call=None)

    class _EncodeFailsBackend(FakeTxBackend):
        def encode(self, message):
            raise EncoderError("cannot pack")

    backend = _EncodeFailsBackend()
    sched = _make_sched(queue, backend)
    sched._tick()

    msg = queue.get(msg_id)
    assert msg.state == OutboundState.ABANDONED
    assert "encode failed" in (msg.error or "")
    assert msg.attempts == 1
    # No frames played — encode failed before any TX.
    assert backend.audio_played == []


# ── Stale SENDING cleanup at startup ─────────────────────────────────


def test_run_cleans_up_stale_sending_rows(queue):
    """On daemon restart, rows left in SENDING from a previous run
    are abandoned with a clear error message — they're partial
    transmissions that nothing can recover."""
    # Simulate a row that was mid-TX when the daemon last died.
    stale_id = queue.enqueue("STALE", OutboundKind.ALLCALL, to_call=None)
    queue.mark_sending(stale_id)
    assert queue.get(stale_id).state == OutboundState.SENDING

    # Drive run() through one iteration of the cleanup, then stop.
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    # We don't actually want to enter the run loop; the cleanup is
    # the very first thing run() does. Trigger the cleanup directly
    # by replicating that logic. (Alternatively we'd start the
    # thread and stop it immediately; this is more deterministic.)
    cleared = queue.abandon_stale_sending(
        error="interrupted by daemon restart",
    )
    assert cleared == 1
    msg = queue.get(stale_id)
    assert msg.state == OutboundState.ABANDONED
    assert "daemon restart" in (msg.error or "")


def test_abandon_stale_sending_does_not_touch_other_states(queue):
    """abandon_stale_sending should ONLY transition SENDING rows,
    not QUEUED or WAIT_ACK rows. (Tested separately for DELIVERED
    so we stay under QUEUE_DEPTH=3.)"""
    queued_id = queue.enqueue("Q", OutboundKind.ALLCALL, to_call=None)
    sending_id = queue.enqueue("S", OutboundKind.ALLCALL, to_call=None)
    wait_ack_id = queue.enqueue("W", OutboundKind.DIRECTED, to_call="K1ABC")
    queue.mark_sending(sending_id)
    queue.mark_sending(wait_ack_id)
    queue.mark_wait_ack(wait_ack_id)

    cleared = queue.abandon_stale_sending(error="restart")
    assert cleared == 1  # only the SENDING row

    assert queue.get(queued_id).state == OutboundState.QUEUED
    assert queue.get(sending_id).state == OutboundState.ABANDONED
    assert queue.get(wait_ack_id).state == OutboundState.WAIT_ACK


def test_abandon_stale_sending_skips_delivered_and_abandoned(queue):
    """Done-state rows (DELIVERED / ABANDONED) are out of the active
    set entirely and stay where they are."""
    delivered_id = queue.enqueue("D", OutboundKind.ALLCALL, to_call=None)
    queue.mark_sending(delivered_id)
    queue.mark_delivered(delivered_id)

    abandoned_id = queue.enqueue("A", OutboundKind.ALLCALL, to_call=None)
    queue.mark_sending(abandoned_id)
    queue.mark_abandoned(abandoned_id, error="prior")

    sending_id = queue.enqueue("S", OutboundKind.ALLCALL, to_call=None)
    queue.mark_sending(sending_id)

    cleared = queue.abandon_stale_sending(error="restart")
    assert cleared == 1

    # Done-state rows untouched.
    assert queue.get(delivered_id).state == OutboundState.DELIVERED
    assert queue.get(abandoned_id).state == OutboundState.ABANDONED
    assert queue.get(abandoned_id).error == "prior"  # original error preserved
    assert queue.get(sending_id).state == OutboundState.ABANDONED


def test_abandon_stale_sending_returns_zero_when_none(queue):
    """If there are no SENDING rows, cleanup is a no-op."""
    queue.enqueue("Q", OutboundKind.ALLCALL, to_call=None)
    cleared = queue.abandon_stale_sending(error="restart")
    assert cleared == 0


# ── EncodedAudioCache integration ───────────────────────────────────


def test_scheduler_uses_cached_audio_when_present(queue):
    """When the cache has audio for a message, scheduler reads it
    INSTEAD of calling backend.encode(). This is the production
    happy-path: encode worker pre-rendered the audio, scheduler
    just hands it to playback."""
    import numpy as np

    from minijs8.tx.encode_worker import EncodedAudioCache

    backend = FakeTxBackend()
    cache = EncodedAudioCache()

    msg_id = queue.enqueue("HELLO", OutboundKind.ALLCALL, to_call=None)
    # Pretend the worker pre-encoded this. Use a sentinel array so
    # we can confirm the SAME buffer ended up in transmit().
    sentinel = np.full(100, 99, dtype=np.int16)
    cache.put(msg_id, [sentinel])

    sched = _make_sched(queue, backend, encoded_audio_cache=cache)
    sched._tick()

    # backend.encode_calls is empty — scheduler used the cached audio,
    # not the backend's encoder. (FakeTxBackend records both
    # encoded_for and transmissions; we want zero encode calls.)
    assert backend.encoded_for == []


def test_scheduler_falls_back_to_inline_encode_on_cache_miss(queue):
    """If cache is configured but a particular message has no entry
    (rare — daemon restart between worker put() and queue
    mark_encoded()), scheduler falls back to inline encode rather
    than abandoning the message."""
    from minijs8.tx.encode_worker import EncodedAudioCache

    backend = FakeTxBackend()
    cache = EncodedAudioCache()  # empty

    msg_id = queue.enqueue("HELLO", OutboundKind.ALLCALL, to_call=None)
    sched = _make_sched(queue, backend, encoded_audio_cache=cache)
    sched._tick()

    # Cache was empty — scheduler had to call backend.encode.
    assert backend.encoded_for == ["HELLO"]


def test_scheduler_legacy_path_still_works_without_cache(queue):
    """When NO cache is supplied (cache=None), scheduler always
    encodes inline. This is the backward-compatible path used by
    older tests."""
    backend = FakeTxBackend()

    queue.enqueue("LEGACY", OutboundKind.ALLCALL, to_call=None)
    sched = _make_sched(queue, backend)  # no cache
    sched._tick()

    assert backend.encoded_for == ["LEGACY"]


def test_scheduler_discards_cache_on_delivered(queue):
    """Broadcasts go straight to DELIVERED on TX success. The cached
    audio should be dropped at that moment so memory doesn't leak."""
    import numpy as np

    from minijs8.tx.encode_worker import EncodedAudioCache

    backend = FakeTxBackend()
    cache = EncodedAudioCache()

    msg_id = queue.enqueue("HELLO", OutboundKind.ALLCALL, to_call=None)
    cache.put(msg_id, [np.zeros(100, dtype=np.int16)])
    assert cache.has(msg_id)

    sched = _make_sched(queue, backend, encoded_audio_cache=cache)
    sched._tick()

    # Delivered → cache discarded.
    assert queue.get(msg_id).state == OutboundState.DELIVERED
    assert not cache.has(msg_id)


def test_scheduler_keeps_cache_during_wait_ack(queue):
    """Directed messages enter WAIT_ACK after a successful TX. The
    cached audio must be PRESERVED — if the ACK times out we'll
    retry, and we want to reuse the same audio rather than re-encoding."""
    import numpy as np

    from minijs8.tx.encode_worker import EncodedAudioCache

    backend = FakeTxBackend()
    cache = EncodedAudioCache()

    msg_id = queue.enqueue("K1ABC MSG HI", OutboundKind.DIRECTED, to_call="K1ABC")
    cache.put(msg_id, [np.zeros(100, dtype=np.int16)])

    sched = _make_sched(queue, backend, encoded_audio_cache=cache)
    sched._tick()

    assert queue.get(msg_id).state == OutboundState.WAIT_ACK
    # Cache still has audio (waiting for ACK or retry).
    assert cache.has(msg_id)


def test_scheduler_discards_cache_on_inline_encode_failure(queue):
    """If cache miss + inline encode fails, the row is abandoned
    AND the (empty) cache slot is cleaned up. This is defense-in-
    depth — even though the cache had no entry to begin with,
    discard is safe to call."""
    from minijs8.tx.encode_worker import EncodedAudioCache

    class _BrokenBackend(FakeTxBackend):
        def encode(self, text):
            from minijs8.modem.encoder import EncoderError
            raise EncoderError("simulated encode failure")

    backend = _BrokenBackend()
    cache = EncodedAudioCache()

    msg_id = queue.enqueue("BAD", OutboundKind.ALLCALL, to_call=None)
    sched = _make_sched(queue, backend, encoded_audio_cache=cache)
    sched._tick()

    assert queue.get(msg_id).state == OutboundState.ABANDONED
    assert not cache.has(msg_id)


def test_classifier_safety_net_rescues_misclassified_query(queue):
    """Direct-SQL canary: a row inserted with kind=DIRECTED but text
    that infers as REPLY (e.g., "<call> QUERY MSGS") must transition
    SENDING→DELIVERED on TX completion, not WAIT_ACK.

    This is the safety net for any path that bypasses the Python API
    classifier — manual SQL INSERTs from the operator console, prototype
    tooling, REPL sessions, future Compose UI bugs. The scheduler
    re-infers kind from the text at the WAIT_ACK decision point so the
    on-air loop can never recur regardless of what kind got persisted.

    Reproduces the exact scenario from the bench-test log:
        sudo -u minijs8 sqlite3 /var/minijs8/messages.db
        INSERT INTO outbound (kind, text, to_call, ...)
        VALUES ('DIRECTED', 'KD8PGB QUERY MSGS', 'KD8PGB', ...)
    """
    msg_id = queue.enqueue(
        "KD8PGB QUERY MSGS",
        OutboundKind.DIRECTED,  # MISCLASSIFIED — caller forced DIRECTED
        to_call="KD8PGB",
    )
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()

    # Despite the stored kind=DIRECTED, the safety net re-infers from
    # text "KD8PGB QUERY MSGS" → REPLY → skips WAIT_ACK → DELIVERED.
    msg = queue.get(msg_id)
    assert msg.state == OutboundState.DELIVERED, (
        f"safety net failed: row was {msg.state} after TX. The "
        f"on-air QUERY MSGS loop will return."
    )

    # No retransmits across multiple ticks.
    for _ in range(3):
        sched._tick()
    assert queue.get(msg_id).state == OutboundState.DELIVERED
    assert len(backend.audio_played) == 1, "outbound query was retransmitted"


def test_classifier_safety_net_does_not_overreach_on_real_msg(queue):
    """Counterpart: a row with stored kind=DIRECTED AND text that
    infers as DIRECTED ("<call> MSG <body>") MUST still go to
    WAIT_ACK. We don't want the safety net to mis-rescue real mail
    deliveries that genuinely need ACK tracking.
    """
    msg_id = queue.enqueue(
        "KC1WDO MSG HELLO MIKE",
        OutboundKind.DIRECTED,
        to_call="KC1WDO",
    )
    backend = FakeTxBackend()
    sched = _make_sched(queue, backend)
    sched._tick()

    msg = queue.get(msg_id)
    assert msg.state == OutboundState.WAIT_ACK, (
        f"real MSG ended in {msg.state} — safety net over-rescued"
    )
