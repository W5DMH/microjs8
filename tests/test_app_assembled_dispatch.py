"""Tests for app.py's ``_dispatch_assembled`` — the path called when
the reassembler emits a complete + checksum-validated message.

These tests differ from ``test_app_inbox_dispatch.py`` in WHERE they
hook into the pipeline:

  - ``test_app_inbox_dispatch.py`` calls ``_dispatch_inbox(parsed,
    frame)`` directly with a single parsed frame. Useful for
    targeted unit testing of the inbox handlers.

  - This file calls ``_dispatch_assembled(assembled, frame)`` with
    an AssembledMessage. This is the LIVE production path — the
    decode handler routes everything through the reassembler and
    only calls ``_dispatch_assembled`` when a message validates.

We also include an end-to-end smoke test that feeds the full multi-
frame KD8PGB scenario through ``_assembler.feed`` → manual dispatch,
proving the wiring works end-to-end.

ACK behavior is the most important regression guard here: ACK fires
ONLY for ``checksum_valid=True`` messages. Older code paths would
ACK on any directed-MSG; the new path enforces JS8 protocol rules
about validated delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from microjs8.app import MicroJS8App
from microjs8.config import Config, StationConfig
from microjs8.protocol.checksum import checksum16
from microjs8.protocol.reassembly import AssembledMessage, MessageAssembler
from microjs8.protocol.types import (
    DecodedFrame,
    FrameKind,
    ParsedFrame,
)
from microjs8.store.inbox import MailboxStore, TYPE_STORE, TYPE_UNREAD
from microjs8.tx.queue import OutboundKind


# ── Fakes (mirrored from test_app_inbox_dispatch.py) ────────────────


@dataclass
class _EnqueuedMessage:
    text: str
    kind: OutboundKind
    to_call: Optional[str]


class _FakeOutboundQueue:
    """Captures enqueue calls without persisting anywhere."""

    def __init__(self) -> None:
        self.enqueued: list[_EnqueuedMessage] = []
        self._next_id = 100

    def enqueue_for_encoding(
        self, text: str, kind: OutboundKind,
        to_call: Optional[str] = None,
    ) -> int:
        self.enqueued.append(
            _EnqueuedMessage(text=text, kind=kind, to_call=to_call)
        )
        n = self._next_id
        self._next_id += 1
        return n

    def get(self, message_id: int):
        return None

    def record_ack(self, ack_from_call: str):
        return None


def _make_app(tmp_path: Path) -> MicroJS8App:
    """Construct a minimal app for unit-testing assembled dispatch."""
    cfg = Config(station=StationConfig(callsign="W5DMH", grid="EN83"))
    app = MicroJS8App(cfg, headless=True)
    app._mailbox = MailboxStore(tmp_path / "inbox.db")
    app._outbound_queue = _FakeOutboundQueue()
    app._ui_state = None
    return app


def _frame(*, freq: float = 1500.0, snr: int = 5,
           received_at: float = 1700000000.0) -> DecodedFrame:
    return DecodedFrame(
        text="", raw="", snr_db=snr, frequency_hz=freq,
        dt_seconds=0.0, submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=received_at,
    )


def _assembled(
    *, from_call: str, to_call: str, verb: str, body: str,
    checksum_valid: bool = True, frame_count: int = 1,
    offset_hz: float = 1500.0,
) -> AssembledMessage:
    """Build an AssembledMessage as the reassembler would emit it."""
    return AssembledMessage(
        from_call=from_call,
        to_call=to_call,
        verb=verb,
        body=body,
        checksum_valid=checksum_valid,
        raw_text=body,
        offset_hz=offset_hz,
        started_at=1700000000.0,
        completed_at=1700000030.0,
        frame_count=frame_count,
    )


# ── 1. The canary: full multi-frame KD8PGB scenario end-to-end ────


def test_canary_multiframe_msg_dispatches_correctly(tmp_path: Path):
    """End-to-end: feed the on-air KD8PGB sequence through the
    assembler, confirm dispatch fires correctly on completion."""
    app = _make_app(tmp_path)
    asm = MessageAssembler()

    # Helper to build parsed frames matching the on-air log.
    def _parsed_directed(body: str) -> ParsedFrame:
        return ParsedFrame(
            decoded=_frame(freq=1616.1),
            kind=FrameKind.DIRECTED_MESSAGE,
            from_call="KD8PGB", to_call="W5DMH", grid=None,
            body=body, is_for_us=True,
        )

    def _parsed_continuation(body: str) -> ParsedFrame:
        return ParsedFrame(
            decoded=_frame(freq=1616.1),
            kind=FrameKind.UNKNOWN,
            from_call=None, to_call=None, grid=None,
            body=body, is_for_us=False,
        )

    # Feed the 3 frames through the assembler.
    a1 = asm.feed(_parsed_directed("MSG"))
    a2 = asm.feed(_parsed_continuation("HELLO FROM REFERENCE J6"))
    a3_list = asm.feed(_parsed_continuation("X"))

    # Only the third feed should produce an AssembledMessage.
    assert a1 == []
    assert a2 == []
    assert len(a3_list) == 1
    a3 = a3_list[0]
    assert a3.checksum_valid

    # Now dispatch and verify side effects.
    app._dispatch_assembled(a3, _frame())

    # An UNREAD row should be in our inbox with the validated body
    # (no checksum suffix).
    rows = app._mailbox.list_inbox()
    assert len(rows) == 1
    row = rows[0]
    assert row.type == TYPE_UNREAD
    assert row.from_call == "KD8PGB"
    assert row.text == "HELLO FROM REFERENCE"

    # An auto-ACK should have been queued.
    assert len(app._outbound_queue.enqueued) == 1
    ack = app._outbound_queue.enqueued[0]
    assert ack.kind == OutboundKind.REPLY
    assert ack.to_call == "KD8PGB"


# ── 2. Checksum-invalid messages do NOT dispatch ───────────────────


def test_failed_checksum_does_not_store_or_ack(tmp_path: Path):
    """A buffer that emitted with checksum_valid=False (e.g. timeout
    or EOT-with-bad-CRC) must NOT result in any inbox row or ACK."""
    app = _make_app(tmp_path)
    asm = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="MSG", body="PARTIAL TEXT",
        checksum_valid=False,
        raw_text="PARTIAL TEXT",
        offset_hz=1500.0,
        started_at=1700000000.0,
        completed_at=1700000030.0,
        frame_count=2,
    )
    app._dispatch_assembled(asm, _frame())

    # No inbox rows.
    assert app._mailbox.list_inbox() == []
    # No ACK queued.
    assert app._outbound_queue.enqueued == []


# ── 3. MSG dispatch ─────────────────────────────────────────────────


def test_msg_direct_to_us_creates_inbox_row(tmp_path: Path):
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="MSG", body="HELLO MIKE",
    )
    app._dispatch_assembled(msg, _frame())

    rows = app._mailbox.list_inbox()
    assert len(rows) == 1
    assert rows[0].type == TYPE_UNREAD
    assert rows[0].from_call == "KC1WDO"
    assert rows[0].text == "HELLO MIKE"


def test_msg_direct_to_us_queues_auto_ack(tmp_path: Path):
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="MSG", body="OK",
    )
    app._dispatch_assembled(msg, _frame())

    assert len(app._outbound_queue.enqueued) == 1
    assert app._outbound_queue.enqueued[0].kind == OutboundKind.REPLY
    assert app._outbound_queue.enqueued[0].to_call == "KC1WDO"


def test_auto_ack_uses_reply_kind_not_directed(tmp_path: Path):
    """Regression guard for the on-air ACK loop bug.

    Before the fix, auto-ACKs were enqueued as kind=DIRECTED. The
    scheduler put them in WAIT_ACK, the recipient (correctly) didn't
    ACK an ACK, and we retransmitted every 90s forever.

    The protocol-correct kind for an auto-ACK is REPLY: directed at
    a specific callsign, but terminal in the protocol exchange.

    If this test ever fails, somebody flipped _queue_ack_to back to
    DIRECTED. Don't.
    """
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="MSG", body="HELLO",
    )
    app._dispatch_assembled(msg, _frame())

    assert len(app._outbound_queue.enqueued) == 1
    ack = app._outbound_queue.enqueued[0]
    assert ack.text == "KC1WDO ACK"
    assert ack.kind == OutboundKind.REPLY, (
        f"Auto-ACK was queued as {ack.kind} — must be REPLY to avoid "
        f"the WAIT_ACK retransmit loop"
    )


def test_msg_for_other_callsign_is_ignored(tmp_path: Path):
    """An MSG addressed to someone else is not stored or ACKed."""
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="K3OTHER",  # not us
        verb="MSG", body="HELLO ELSEWHERE",
    )
    app._dispatch_assembled(msg, _frame())
    assert app._mailbox.list_inbox() == []
    assert app._outbound_queue.enqueued == []


def test_msg_with_empty_body_does_not_create_row(tmp_path: Path):
    """An empty MSG body shouldn't create an empty inbox row."""
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="MSG", body="",
    )
    app._dispatch_assembled(msg, _frame())
    assert app._mailbox.list_inbox() == []


# ── 4. MSG TO: dispatch ────────────────────────────────────────────


def test_msg_to_creates_store_row_for_recipient(tmp_path: Path):
    """MSG TO:KC1WDO body should hold a STORE row for KC1WDO.

    STORE rows are addressed-to-someone-else and don't show in
    ``list_inbox`` (the home-screen inbox view); they're accessed
    via ``list_holding_for(recipient)``.
    """
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KD8PGB", to_call="W5DMH",
        verb="MSG TO:", body="KC1WDO HELLO FROM PETER",
    )
    app._dispatch_assembled(msg, _frame())

    # STORE row should be queryable via list_holding_for
    rows = app._mailbox.list_holding_for("KC1WDO")
    assert len(rows) == 1
    assert rows[0].type == TYPE_STORE
    assert rows[0].from_call == "KD8PGB"
    assert rows[0].to_call == "KC1WDO"
    assert rows[0].text == "HELLO FROM PETER"
    # And it does NOT appear in the inbox view
    assert app._mailbox.list_inbox() == []


def test_msg_to_queues_auto_ack(tmp_path: Path):
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KD8PGB", to_call="W5DMH",
        verb="MSG TO:", body="KC1WDO OK",
    )
    app._dispatch_assembled(msg, _frame())

    assert len(app._outbound_queue.enqueued) == 1
    assert app._outbound_queue.enqueued[0].kind == OutboundKind.REPLY
    assert app._outbound_queue.enqueued[0].to_call == "KD8PGB"


# ── 5. QUERY MSGS dispatch ─────────────────────────────────────────


def test_query_msgs_direct_with_holding_replies_msg_id(tmp_path: Path):
    """QUERY MSGS direct-to-us when we're holding mail for the asker
    should reply with the held message id."""
    app = _make_app(tmp_path)
    # Prime: someone left a message for KC1WDO at our station.
    app._mailbox.add_remote_store(
        sender_call="N0XYZ", recipient_call="KC1WDO",
        text="HELLO KC1WDO",
    )

    # KC1WDO asks us for held mail.
    asm = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="QUERY MSGS", body="",
    )
    app._dispatch_assembled(asm, _frame())

    # Should have queued a "MSG <id>" reply
    assert len(app._outbound_queue.enqueued) == 1
    reply = app._outbound_queue.enqueued[0]
    assert "MSG " in reply.text


def test_query_msgs_direct_with_no_holding_replies_no(tmp_path: Path):
    """QUERY MSGS direct-to-us with nothing to deliver replies NO."""
    app = _make_app(tmp_path)
    asm = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="QUERY MSGS", body="",
    )
    app._dispatch_assembled(asm, _frame())

    # Should reply with NO
    assert len(app._outbound_queue.enqueued) == 1
    reply = app._outbound_queue.enqueued[0]
    assert "NO" in reply.text


def test_query_msgs_reply_uses_reply_kind_not_directed(tmp_path: Path):
    """Regression guard: QUERY MSGS notification replies must be REPLY-kind.

    The "<asker> NO" and "<asker> MSG <id>" responses are informational
    and per JS8Call protocol are NOT auto-ACKed by the asker. Queueing
    them as DIRECTED would trigger the same WAIT_ACK retransmit loop
    that bit us with auto-ACKs.
    """
    app = _make_app(tmp_path)
    # "NO" reply path
    asm = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="QUERY MSGS", body="",
    )
    app._dispatch_assembled(asm, _frame())
    assert len(app._outbound_queue.enqueued) == 1
    assert app._outbound_queue.enqueued[0].kind == OutboundKind.REPLY, (
        "QUERY MSGS 'NO' reply queued as DIRECTED — would loop"
    )

    # "MSG <id>" notification reply path
    app._outbound_queue.enqueued.clear()
    app._mailbox.add_remote_store(
        sender_call="N0XYZ", recipient_call="KC1WDO",
        text="HELLO HELD",
    )
    app._dispatch_assembled(asm, _frame())
    assert len(app._outbound_queue.enqueued) == 1
    msg_id_reply = app._outbound_queue.enqueued[0]
    assert "MSG " in msg_id_reply.text
    assert msg_id_reply.kind == OutboundKind.REPLY, (
        "QUERY MSGS 'MSG <id>' reply queued as DIRECTED — would loop"
    )


def test_query_msgs_allcall_with_no_holding_silent(tmp_path: Path):
    """@ALLCALL QUERY MSGS with nothing to deliver is silent (don't
    pollute the band)."""
    app = _make_app(tmp_path)
    asm = _assembled(
        from_call="KC1WDO", to_call="@ALLCALL",
        verb="QUERY MSGS", body="",
    )
    app._dispatch_assembled(asm, _frame())
    assert app._outbound_queue.enqueued == []


# ── 6. QUERY MSG <id> ─────────────────────────────────────────────


def test_query_msg_id_delivers_held_body(tmp_path: Path):
    """KC1WDO sending QUERY MSG <id> for a STORE row addressed to
    them should deliver the body."""
    app = _make_app(tmp_path)
    held = app._mailbox.add_remote_store(
        sender_call="N0XYZ", recipient_call="KC1WDO",
        text="HELLO FROM N0XYZ",
    )

    asm = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="QUERY", body=f"MSG {held}",
    )
    app._dispatch_assembled(asm, _frame())

    # We should have queued a delivery of the body.
    assert len(app._outbound_queue.enqueued) >= 1
    text = " ".join(e.text for e in app._outbound_queue.enqueued)
    assert "HELLO FROM N0XYZ" in text


# ── 7. Edge cases ─────────────────────────────────────────────────


def test_dispatch_assembled_with_no_mailbox_does_not_crash(tmp_path: Path):
    """If the mailbox failed to open at startup, dispatch must not
    raise — it should silently no-op."""
    app = _make_app(tmp_path)
    app._mailbox = None
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="MSG", body="HELLO",
    )
    # Should NOT raise
    app._dispatch_assembled(msg, _frame())


def test_dispatch_assembled_with_no_from_call_is_ignored(tmp_path: Path):
    """Anonymous frames don't drive inbox state."""
    app = _make_app(tmp_path)
    msg = AssembledMessage(
        from_call="",  # missing
        to_call="W5DMH",
        verb="MSG", body="HELLO",
        checksum_valid=True,
        raw_text="HELLO", offset_hz=1500.0,
        started_at=0, completed_at=0, frame_count=1,
    )
    app._dispatch_assembled(msg, _frame())
    assert app._mailbox.list_inbox() == []
    assert app._outbound_queue.enqueued == []


def test_dispatch_assembled_with_unknown_verb_silent(tmp_path: Path):
    """A verb we don't handle (e.g. HEARING from the future) should
    be silently dropped rather than crash."""
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="HEARING", body="N0XYZ K1ABC",  # not a buffered verb anyway
    )
    app._dispatch_assembled(msg, _frame())
    assert app._mailbox.list_inbox() == []
    assert app._outbound_queue.enqueued == []


# ── Directed-activity logging integration ────────────────────────


def test_msg_for_us_does_not_log_to_directed_activity(tmp_path: Path):
    """MSG addressed to us is an inbox event, not a directed-log event.

    The user's design: only non-MSG/non-MSG-TO directed traffic shows
    in the chat-style DIRECTED log. The mailbox content lives on the
    INBOX screen instead.
    """
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="MSG", body="HELLO",
    )
    app._dispatch_assembled(msg, _frame())

    # Inbound side: NOT logged to directed activity
    snap = app._directed_activity.snapshot()
    inbound_entries = [e for e in snap if e.direction.value == "IN"]
    assert inbound_entries == [], (
        f"MSG should not appear in directed-log inbound, got {inbound_entries}"
    )
    # Outbound auto-ACK IS logged (any outbound reply lands in chat log)
    outbound_entries = [e for e in snap if e.direction.value == "OUT"]
    assert len(outbound_entries) == 1
    assert outbound_entries[0].verb == "ACK"
    assert outbound_entries[0].other_call == "KC1WDO"


def test_msg_to_does_not_log_to_directed_activity(tmp_path: Path):
    """MSG TO: addressed to us is a held-mail event, not a directed-log event."""
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KD8PGB", to_call="W5DMH",
        verb="MSG TO:", body="KC1WDO HELLO FROM PETER",
    )
    app._dispatch_assembled(msg, _frame())

    snap = app._directed_activity.snapshot()
    inbound_entries = [e for e in snap if e.direction.value == "IN"]
    assert inbound_entries == []


def test_query_msgs_logs_inbound_and_outbound_to_directed_activity(tmp_path: Path):
    """QUERY MSGS round-trip: inbound query AND our outbound reply both
    appear in the directed log so the operator sees the full exchange."""
    app = _make_app(tmp_path)
    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="QUERY MSGS", body="",
    )
    app._dispatch_assembled(msg, _frame())

    snap = app._directed_activity.snapshot()
    assert len(snap) == 2, f"expected 2 entries (in + out), got {snap}"

    # Inbound query
    in_entry = snap[0]
    assert in_entry.direction.value == "IN"
    assert in_entry.other_call == "KC1WDO"
    assert in_entry.verb == "QUERY MSGS"

    # Outbound reply
    out_entry = snap[1]
    assert out_entry.direction.value == "OUT"
    assert out_entry.other_call == "KC1WDO"
    # Empty inbox → "NO" reply
    assert out_entry.verb == "NO"


def test_query_msg_id_logs_round_trip_to_directed_activity(tmp_path: Path):
    """QUERY MSG <id> body delivery exchange appears in the directed log."""
    app = _make_app(tmp_path)
    held = app._mailbox.add_remote_store(
        sender_call="N0XYZ", recipient_call="KC1WDO",
        text="HELD MSG TEXT",
    )

    msg = _assembled(
        from_call="KC1WDO", to_call="W5DMH",
        verb="QUERY", body=f"MSG {held}",
    )
    app._dispatch_assembled(msg, _frame())

    snap = app._directed_activity.snapshot()
    assert len(snap) >= 2
    # Inbound query is first
    in_entry = snap[0]
    assert in_entry.direction.value == "IN"
    assert in_entry.other_call == "KC1WDO"
    assert in_entry.verb == "QUERY"
    # And we have at least one outbound entry (the body delivery)
    outbound = [e for e in snap if e.direction.value == "OUT"]
    assert len(outbound) >= 1


def test_failed_checksum_buffered_directed_to_us_logs_incomplete(tmp_path: Path):
    """A failed-checksum buffered message addressed to us should
    surface to the directed activity log with an ⚠ INCOMPLETE tag —
    so the operator visibly sees that something arrived but was
    unrecoverable. Behavior change from the previous silent-drop:
    silent drops are scary, visible incompletes prompt the operator
    to ask the sender to retransmit.

    Still: NO inbox row, NO auto-ACK (the CRC is the protocol
    contract for 'I got it intact' — we didn't, so we don't lie).
    """
    app = _make_app(tmp_path)
    bad = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="MSG", body="garbled body content",
        checksum_valid=False,
        raw_text="garbled body content",
        offset_hz=1616.0,
        started_at=0.0, completed_at=0.0, frame_count=4,
    )
    app._dispatch_assembled(bad, _frame())

    # Inbox unchanged
    assert app._mailbox.list_inbox() == []
    # No auto-ACK queued
    assert app._outbound_queue.enqueued == []
    # ⚠ INCOMPLETE entry IS in the activity log
    snap = app._directed_activity.snapshot()
    assert len(snap) == 1
    e = snap[0]
    assert e.direction.value == "IN"
    assert e.other_call == "KD8PGB"
    assert "INCOMPLETE" in e.verb, (
        f"expected verb to contain 'INCOMPLETE' tag, got {e.verb!r}"
    )
    assert "MSG" in e.verb  # original verb preserved in tag


def test_failed_checksum_buffered_to_other_station_does_not_log(tmp_path: Path):
    """An incomplete frame addressed to ANOTHER station (not us, not
    @ALLCALL) is noise — don't surface it. We only care about
    incomplete content that was supposed to reach us."""
    app = _make_app(tmp_path)
    bad = AssembledMessage(
        from_call="KD8PGB", to_call="N0XYZ",  # not us
        verb="QUERY MSGS", body="(garbage)",
        checksum_valid=False,
        raw_text="(garbage)",
        offset_hz=1500.0,
        started_at=0.0, completed_at=0.0, frame_count=2,
    )
    app._dispatch_assembled(bad, _frame())
    assert app._directed_activity.snapshot() == ()


# ── Single-frame directed-activity logging (decode handler path) ──────


def test_single_frame_snr_query_logs_to_directed_activity(tmp_path: Path):
    """An inbound DIRECTED_QUERY (e.g. SNR?) is single-frame, doesn't
    go through the assembler, but should still appear in the
    directed log via the decode-handler hook."""
    from microjs8.protocol.types import DecodedFrame, FrameKind, ParsedFrame

    app = _make_app(tmp_path)
    # Compose a single-frame SNR? directed at us.
    df = DecodedFrame(
        text="KD8PGB: W5DMH SNR?", raw="",
        snr_db=-9, frequency_hz=1500.0,
        dt_seconds=0.0, submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )
    parsed = ParsedFrame(
        decoded=df, kind=FrameKind.DIRECTED_QUERY,
        from_call="KD8PGB", to_call="W5DMH", grid=None,
        body="SNR?", is_for_us=True,
    )
    # Drive the decode handler by calling its inbound-log helper directly
    # (the handler itself is an asyncio coroutine; the helper is what
    # we want to exercise).
    app._log_directed_in(parsed)

    snap = app._directed_activity.snapshot()
    assert len(snap) == 1
    e = snap[0]
    assert e.direction.value == "IN"
    assert e.other_call == "KD8PGB"
    assert e.verb == "SNR?"
    assert e.snr_db == -9
    assert e.freq_hz == 1500.0


def test_single_frame_ack_logs_to_directed_activity(tmp_path: Path):
    """An inbound ACK (response to one of our outbound MSGs) shows in
    the directed log. This is how the operator sees that a station
    received and ACKed something we sent."""
    from microjs8.protocol.types import DecodedFrame, FrameKind, ParsedFrame

    app = _make_app(tmp_path)
    df = DecodedFrame(
        text="KD8PGB: W5DMH ACK", raw="",
        snr_db=3, frequency_hz=1500.0,
        dt_seconds=0.0, submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )
    parsed = ParsedFrame(
        decoded=df, kind=FrameKind.ACK,
        from_call="KD8PGB", to_call="W5DMH", grid=None,
        body="ACK", is_for_us=True,
    )
    app._log_directed_in(parsed)

    snap = app._directed_activity.snapshot()
    assert len(snap) == 1
    assert snap[0].verb == "ACK"
    assert snap[0].other_call == "KD8PGB"
    assert snap[0].snr_db == 3


# ── Non-buffered multi-frame reassembly (YES MSG ID 57 scenario) ──────


def test_non_buffered_assembled_logs_to_directed_activity_no_dispatch(tmp_path: Path):
    """A reassembled non-buffered directed message (YES MSG ID 57) should:
      - Land in the directed-activity log (operator sees full body)
      - NOT trigger inbox dispatch (it's not mail content)
      - NOT auto-ACK (no protocol obligation)
    """
    app = _make_app(tmp_path)
    nb = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="YES", body="YES MSG ID 57",
        checksum_valid=True,
        raw_text="YES MSG ID 57",
        offset_hz=1616.0,
        started_at=0.0, completed_at=0.0, frame_count=2,
        was_buffered_command=False,
    )
    app._dispatch_assembled(nb, _frame())

    # Inbox should NOT receive this
    assert app._mailbox.list_inbox() == []
    # No outbound auto-ACK (since the message is non-buffered)
    assert app._outbound_queue.enqueued == []
    # But it IS in the directed activity log
    snap = app._directed_activity.snapshot()
    assert len(snap) == 1
    e = snap[0]
    assert e.direction.value == "IN"
    assert e.other_call == "KD8PGB"
    assert e.verb == "YES"
    # Body contains the full reassembled text
    assert "MSG ID 57" in e.body or "MSG ID 57" in (e.verb + " " + e.body)


def test_canary_yes_msg_id_full_pipeline(tmp_path: Path):
    """End-to-end on-air canary: feed the exact 2-frame YES MSG ID 57
    sequence through the assembler, verify reassembly + dispatch +
    activity log."""
    app = _make_app(tmp_path)
    asm = app._assembler
    clock = [10000.0]
    # Override the assembler clock for deterministic timing
    asm._clock = lambda: clock[0]

    # Frame 1: directed envelope, body="YES" at offset 1616 Hz
    f1 = ParsedFrame(
        decoded=_frame(freq=1616.1),
        kind=FrameKind.DIRECTED_MESSAGE,
        from_call="KD8PGB", to_call="W5DMH", grid=None,
        body="YES", is_for_us=True,
    )
    assert asm.feed(f1) == []  # buffered, no immediate emit

    # Frame 2: continuation 16s later
    clock[0] = 10016.0
    f2 = ParsedFrame(
        decoded=_frame(freq=1616.1),
        kind=FrameKind.UNKNOWN,
        from_call=None, to_call=None, grid=None,
        body="MSG ID 57", is_for_us=False,
    )
    assert asm.feed(f2) == []  # appended, still no emit

    # Past the 20s timeout (21s after frame 2)
    clock[0] = 10037.0
    # Use sweep to surface the completion
    results = asm.sweep_completed()
    assert len(results) == 1
    assembled = results[0]
    assert assembled.from_call == "KD8PGB"
    assert assembled.verb == "YES"
    assert assembled.was_buffered_command is False
    assert assembled.checksum_valid is True
    assert assembled.frame_count == 2
    # Full body reassembled
    assert "YES" in assembled.body
    assert "MSG ID 57" in assembled.body

    # Dispatch
    app._dispatch_assembled(assembled, _frame())

    # Inbox unchanged (non-buffered → activity only)
    assert app._mailbox.list_inbox() == []
    # No outbound (non-buffered → no protocol reply)
    assert app._outbound_queue.enqueued == []
    # Activity log got the full reassembled body
    snap = app._directed_activity.snapshot()
    assert len(snap) == 1
    e = snap[0]
    assert e.other_call == "KD8PGB"
    assert e.direction.value == "IN"
    # Body+verb contains the full message
    full_text = e.verb + " " + (e.body or "")
    assert "MSG ID 57" in full_text


def test_failed_checksum_buffered_still_no_inbox_no_ack(tmp_path: Path):
    """A buffered command with checksum_valid=False:
    - Still NOT written to inbox (incomplete content is not mail)
    - Still NOT auto-ACKed (CRC is the protocol contract for 'intact receipt')
    - DOES surface to directed activity log with ⚠ INCOMPLETE tag
      (visibility for operator) — see the dedicated incomplete-logs
      test for that assertion. This test focuses on the still-true
      no-side-effects invariants.
    """
    app = _make_app(tmp_path)
    bad = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="MSG", body="(garbled)",
        checksum_valid=False,
        raw_text="(garbled)",
        offset_hz=1500.0,
        started_at=0.0, completed_at=0.0, frame_count=2,
        was_buffered_command=True,
    )
    app._dispatch_assembled(bad, _frame())
    assert app._mailbox.list_inbox() == []
    assert app._outbound_queue.enqueued == []


def test_assembled_non_buffered_strips_duplicate_verb_from_body(tmp_path: Path):
    """Activity log should not duplicate the verb token when body
    already starts with it.

    For non-buffered messages, the assembler keeps the entire frame
    body in ``body`` (verb included) so the assembler can stay
    verb-agnostic. The activity log dedups when emitting so the
    DIRECTED screen doesn't show "KD8PGB YES YES MSG ID 57" — which
    would occupy 24 chars on a tight screen and overflow the row.
    """
    app = _make_app(tmp_path)
    am = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="YES", body="YES MSG ID 57",  # verb duplicated in body
        checksum_valid=True,
        raw_text="YES MSG ID 57",
        offset_hz=1616.0,
        started_at=0.0, completed_at=0.0, frame_count=2,
        was_buffered_command=False,
    )
    app._dispatch_assembled(am, _frame())
    snap = app._directed_activity.snapshot()
    assert len(snap) == 1
    e = snap[0]
    assert e.verb == "YES"
    # Body should be "MSG ID 57" — verb stripped to avoid duplication
    assert e.body == "MSG ID 57", (
        f"verb not stripped from body: got {e.body!r}, expected 'MSG ID 57'"
    )


def test_assembled_strip_does_not_eat_unrelated_prefix(tmp_path: Path):
    """If body just HAPPENS to start with the verb's letters but
    isn't actually the verb token (e.g. body='YES_OK' as a single
    token, or body 'YESTERDAY...'), the strip MUST NOT touch it.
    Word-boundary check prevents false-positive stripping."""
    app = _make_app(tmp_path)
    am = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="YES", body="YESTERDAY was busy",  # body STARTS with "YES" but isn't the verb
        checksum_valid=True,
        raw_text="YESTERDAY was busy",
        offset_hz=1616.0,
        started_at=0.0, completed_at=0.0, frame_count=1,
        was_buffered_command=False,
    )
    app._dispatch_assembled(am, _frame())
    snap = app._directed_activity.snapshot()
    e = snap[0]
    assert e.verb == "YES"
    assert e.body == "YESTERDAY was busy", (
        f"body got falsely stripped: {e.body!r}"
    )


def test_assembled_strip_handles_verb_only_body(tmp_path: Path):
    """When body == verb exactly (single-frame command like a bare
    "ACK"), strip leaves an empty body."""
    app = _make_app(tmp_path)
    am = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="STATUS", body="STATUS",
        checksum_valid=True,
        raw_text="STATUS",
        offset_hz=1616.0,
        started_at=0.0, completed_at=0.0, frame_count=1,
        was_buffered_command=False,
    )
    app._dispatch_assembled(am, _frame())
    snap = app._directed_activity.snapshot()
    e = snap[0]
    assert e.verb == "STATUS"
    assert e.body == ""


# ── HEARTBEAT supersede: single+multiframe collapse to one entry ──


def test_heartbeat_multiframe_supersedes_single_frame_entry(tmp_path):
    """When the reassembler emits a multi-frame non-buffered message
    (e.g., HEARTBEAT SNR +04 + continuation MSG ID 61), it should
    REPLACE the prior single-frame entry the same wire produced,
    not append a duplicate. End-to-end check that
    _log_directed_in_assembled routes multi-frame non-buffered emits
    through record_in_supersede. Per W5DMH bench, May 2026."""
    import time as _time
    app = _make_app(tmp_path)

    # Use a timestamp recent enough that the 60 s supersede window
    # captures the prior entry when _log_directed_in_assembled fires.
    now = _time.time()

    # Simulate the immediate single-frame dispatch (what
    # _log_directed_in does on frame 1).
    app._directed_activity.record_in(
        from_call="KD8PGB",
        verb="HEARTBEAT",
        body="SNR +04",
        snr_db=14,
        freq_hz=700.0,
        at_unix=now - 5.0,    # 5 s ago — inside the window
    )
    assert len(app._directed_activity) == 1

    # Now the reassembled multi-frame emit fires with the full body.
    # frame_count=2 and was_buffered_command=False trigger the
    # supersede branch.
    asm = AssembledMessage(
        from_call="KD8PGB", to_call="W5DMH",
        verb="HEARTBEAT", body="HEARTBEAT SNR +04 MSG ID 61",
        checksum_valid=True,
        raw_text="HEARTBEAT SNR +04 MSG ID 61",
        offset_hz=700.0,
        started_at=now - 5.0,
        completed_at=now,
        frame_count=2,
        was_buffered_command=False,
    )
    app._log_directed_in_assembled(asm)

    # Single entry — the original was replaced, not appended.
    snap = app._directed_activity.snapshot()
    assert len(snap) == 1, (
        f"expected one entry after supersede, got {len(snap)}: "
        f"{[(e.verb, e.body) for e in snap]}"
    )
    e = snap[0]
    assert e.verb == "HEARTBEAT"
    assert e.body == "SNR +04 MSG ID 61"
    # SNR preserved from the original single-frame entry — the
    # reassembled emit doesn't carry SNR.
    assert e.snr_db == 14


def test_single_frame_buffered_does_not_supersede(tmp_path):
    """Single-frame buffered commands (frame_count=1, was_buffered=
    True) go through the regular record_in path. A prior matching
    entry won't be collapsed (no supersede branch fires).

    This guards against accidentally consolidating unrelated buffered
    commands — those have their own protocol semantics distinct from
    free-text continuation."""
    import time as _time
    app = _make_app(tmp_path)
    now = _time.time()
    app._directed_activity.record_in(
        from_call="K1ABC", verb="QUERY MSGS", body="",
        snr_db=10, at_unix=now - 5.0,
    )
    asm = AssembledMessage(
        from_call="K1ABC", to_call="W5DMH",
        verb="QUERY MSGS", body="",
        checksum_valid=True, raw_text="",
        offset_hz=1500.0,
        started_at=now - 5.0,
        completed_at=now,
        frame_count=1,
        was_buffered_command=True,
    )
    app._log_directed_in_assembled(asm)
    snap = app._directed_activity.snapshot()
    # Both entries present — no supersede.
    assert len(snap) == 2



# ── Phase 19 v0.0.10: defensive heard upsert from assembled dispatch ──


def test_dispatch_assembled_upserts_heard_for_sender(tmp_path: Path):
    """Phase 19 v0.0.10: _dispatch_assembled must upsert the sender
    into the heard list. Closes the field-test issue where a directed
    message sender (e.g. ND7M sending "GRID DM16") wouldn't appear in
    Heard if the first frame's parse was noisy — the reassembled
    message would still surface in the DIRECTED log but the operator
    couldn't reply via Compose's TO dropdown because the callsign
    wasn't in Heard.

    The upsert is idempotent (ON CONFLICT DO UPDATE), so this firing
    in addition to _on_decoded_frame's single-frame path is safe.
    """
    from microjs8.store.db import MessageStore

    app = _make_app(tmp_path)
    # Attach a real MessageStore so the heard upsert has somewhere to land
    app._store = MessageStore(tmp_path / "store.db")
    asm_msg = _assembled(
        from_call="ND7M",
        to_call="W5DMH",
        verb="GRID",
        body="DM16",
        checksum_valid=False,   # non-buffered directed; doesn't matter for heard
    )
    frame = _frame(snr=-12, freq=987.5)

    # Pre-check: heard table empty
    assert len(app._store.heard_stations()) == 0

    app._dispatch_assembled(asm_msg, frame)

    # Post: ND7M should be in heard
    heard = app._store.heard_stations()
    assert len(heard) == 1, f"expected 1 heard row, got {len(heard)}"
    assert heard[0].callsign == "ND7M"
    # GRID body parsed → grid populated
    assert heard[0].grid == "DM16"
    # SNR taken from the frame parameter
    assert heard[0].snr_db == -12


def test_dispatch_assembled_with_no_store_does_not_crash(tmp_path: Path):
    """Phase 19 v0.0.10: defensive — if no MessageStore is attached
    (tests / minimal embed), the new heard-upsert path must skip
    cleanly without raising."""
    app = _make_app(tmp_path)
    app._store = None     # explicitly no store

    asm_msg = _assembled(
        from_call="ND7M", to_call="W5DMH", verb="GRID", body="DM16",
        checksum_valid=False,
    )
    # Must not raise
    app._dispatch_assembled(asm_msg, _frame())


def test_dispatch_assembled_heard_upsert_idempotent(tmp_path: Path):
    """Phase 19 v0.0.10: firing twice for the same sender keeps a
    single heard row (the upsert clause does ON CONFLICT DO UPDATE,
    not INSERT)."""
    from microjs8.store.db import MessageStore

    app = _make_app(tmp_path)
    app._store = MessageStore(tmp_path / "store.db")

    asm_msg = _assembled(
        from_call="ND7M", to_call="W5DMH", verb="GRID", body="DM16",
        checksum_valid=False,
    )
    app._dispatch_assembled(asm_msg, _frame(snr=-12))
    app._dispatch_assembled(asm_msg, _frame(snr=-5))  # fresher SNR

    heard = app._store.heard_stations()
    assert len(heard) == 1, "duplicate upserts must not multiply rows"
    # The freshest SNR wins (ON CONFLICT DO UPDATE replaces snr_db)
    assert heard[0].snr_db == -5


# ── Phase 19 v0.0.11: own-callsign filter + skip-no-SNR ──────────────


def test_dispatch_assembled_does_not_upsert_self(tmp_path: Path):
    """v0.0.11: never add our own callsign to the heard list.

    The radio's monitor / hardware loopback can feed our TX back into
    the decoder, producing an AssembledMessage with from_call == us.
    Pre-v0.0.11 we'd add W5DMH to Heard as an echo of ourselves.
    """
    from microjs8.store.db import MessageStore

    app = _make_app(tmp_path)
    app._store = MessageStore(tmp_path / "store.db")
    asm_msg = _assembled(
        from_call="W5DMH",     # OUR callsign (matches the test app config)
        to_call="@HB",
        verb="HEARTBEAT",
        body="EN83",
        checksum_valid=False,
    )
    frame = _frame(snr=-3, freq=1500.0)
    app._dispatch_assembled(asm_msg, frame)

    heard = app._store.heard_stations()
    assert len(heard) == 0, (
        f"refused: our own callsign must not be added to heard list; "
        f"got {heard}"
    )


def test_dispatch_assembled_skips_when_no_frame_provided(tmp_path: Path):
    """v0.0.11: skip defensive upsert when frame=None (reassembly
    timeout path). The single-frame _update_heard_for path has
    already run with a real SNR for that sender's first frame —
    trying to insert with snr=None hits NOT NULL constraint.

    Closes the IntegrityError spam observed in PI-2W-TEST journal.
    """
    from microjs8.store.db import MessageStore

    app = _make_app(tmp_path)
    app._store = MessageStore(tmp_path / "store.db")
    asm_msg = _assembled(
        from_call="KD8GIJ", to_call="W5DMH", verb="HEARTBEAT",
        body="HEARTBEAT SNR -02",
        checksum_valid=False,
    )
    # No frame — this is the reassembly-timeout call site.
    # Pre-v0.0.11 this raised sqlite3.IntegrityError; post-v0.0.11
    # it should silently skip (no row added, no exception).
    app._dispatch_assembled(asm_msg, None)

    heard = app._store.heard_stations()
    assert len(heard) == 0, (
        f"defensive upsert must skip when frame=None; got {heard}"
    )


def test_dispatch_assembled_normal_path_still_upserts(tmp_path: Path):
    """v0.0.11 regression guard: the original v0.0.10 path (real
    frame + foreign callsign) must STILL upsert. We don't want the
    self-filter or no-SNR-skip to break the actual use case."""
    from microjs8.store.db import MessageStore

    app = _make_app(tmp_path)
    app._store = MessageStore(tmp_path / "store.db")
    asm_msg = _assembled(
        from_call="KD8PGB",        # NOT us
        to_call="W5DMH",
        verb="GRID",
        body="EN82",
        checksum_valid=False,
    )
    frame = _frame(snr=-9, freq=1500.0)
    app._dispatch_assembled(asm_msg, frame)

    heard = app._store.heard_stations()
    assert len(heard) == 1
    assert heard[0].callsign == "KD8PGB"
    assert heard[0].snr_db == -9
    assert heard[0].grid == "EN82"


def test_update_heard_for_does_not_upsert_self(tmp_path: Path):
    """v0.0.11: _update_heard_for must refuse our own callsign.

    The radio's monitor or hardware-level loopback can feed our own
    TX back into the decoder, producing a ParsedFrame with
    from_call == us. The Heard list represents OTHER stations, never
    us. Without this filter, operators see W5DMH appear in their own
    Heard list every time they transmit a heartbeat (observed in
    PI-2W-TEST field testing of v0.0.10).
    """
    from microjs8.store.db import MessageStore
    from microjs8.protocol.types import FrameKind, ParsedFrame

    app = _make_app(tmp_path)
    app._store = MessageStore(tmp_path / "store.db")

    # Build a ParsedFrame that LOOKS like our own heartbeat returning
    # via the radio's monitor: from_call = "W5DMH" (matches config).
    decoded = DecodedFrame(
        text="W5DMH: @HB HEARTBEAT EN83", raw="",
        snr_db=0, frequency_hz=1500.0,
        dt_seconds=0.0, submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )
    parsed = ParsedFrame(
        decoded=decoded,
        kind=FrameKind.HEARTBEAT,
        from_call="W5DMH",       # our own callsign
        to_call="@HB",
        grid="EN83",
        body="",
        is_for_us=False,
    )
    app._update_heard_for(parsed)

    heard = app._store.heard_stations()
    assert len(heard) == 0, (
        f"_update_heard_for must refuse our own callsign; "
        f"got {[h.callsign for h in heard]}"
    )


def test_update_heard_for_normal_path_still_works(tmp_path: Path):
    """v0.0.11 regression guard: a foreign callsign still gets
    upserted via _update_heard_for."""
    from microjs8.store.db import MessageStore
    from microjs8.protocol.types import FrameKind, ParsedFrame

    app = _make_app(tmp_path)
    app._store = MessageStore(tmp_path / "store.db")

    decoded = DecodedFrame(
        text="KD8PGB: @HB HEARTBEAT EN82", raw="",
        snr_db=-7, frequency_hz=1500.0,
        dt_seconds=0.0, submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )
    parsed = ParsedFrame(
        decoded=decoded,
        kind=FrameKind.HEARTBEAT,
        from_call="KD8PGB",        # not us
        to_call="@HB",
        grid="EN82",
        body="",
        is_for_us=False,
    )
    app._update_heard_for(parsed)

    heard = app._store.heard_stations()
    assert len(heard) == 1
    assert heard[0].callsign == "KD8PGB"
