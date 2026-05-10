"""Tests for app.py inbox-dispatch logic (Phase 1+2).

These tests exercise the inbox state machine in MicroJS8App by:

  1. Constructing an app with a real MailboxStore (tmp DB) but with
     OutboundQueue and other heavy components stubbed out as fakes.
  2. Synthesizing a ParsedFrame as if a decode produced it.
  3. Invoking _dispatch_inbox() directly and asserting on:
       - what was added to the inbox (UNREAD / STORE rows)
       - what TX replies were enqueued (auto-ACK, NO, MSG <id>, etc.)

We do NOT exercise the full daemon — that's covered by other test
files. We're focused on the dispatch table being correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from microjs8.app import MicroJS8App
from microjs8.config import Config, StationConfig
from microjs8.protocol.types import (
    DecodedFrame,
    FrameKind,
    ParsedFrame,
)
from microjs8.store.inbox import MailboxStore, TYPE_STORE, TYPE_UNREAD
from microjs8.tx.queue import OutboundKind


# ── Fakes ───────────────────────────────────────────────────────────


@dataclass
class _EnqueuedMessage:
    text: str
    kind: OutboundKind
    to_call: Optional[str]


class _FakeOutboundQueue:
    """Captures enqueue calls without persisting anywhere.

    Mimics the methods _dispatch_inbox actually uses:
    enqueue_for_encoding, get, record_ack.
    """

    def __init__(self) -> None:
        self.enqueued: list[_EnqueuedMessage] = []
        self._next_id = 100

    def enqueue_for_encoding(
        self,
        text: str,
        kind: OutboundKind,
        to_call: Optional[str] = None,
    ) -> int:
        self.enqueued.append(
            _EnqueuedMessage(text=text, kind=kind, to_call=to_call)
        )
        n = self._next_id
        self._next_id += 1
        return n

    # The other methods aren't called by _dispatch_inbox, but the
    # interface stub keeps test failures noisy if app.py grows new
    # call sites.
    def get(self, message_id: int):
        return None

    def record_ack(self, ack_from_call: str):
        return None


# ── Fixture ────────────────────────────────────────────────────────


def _make_app(tmp_path: Path) -> MicroJS8App:
    """Construct a minimal app for unit-testing _dispatch_inbox.

    Wires in a real MailboxStore (tmp DB) so we can assert on
    persisted rows. UI state is constructed but the heavy lifecycle
    starters are bypassed — we only need the dispatcher methods.
    """
    cfg = Config(station=StationConfig(callsign="W5DMH", grid="EN83"))
    app = MicroJS8App(cfg, headless=True)

    # Real mailbox store — exercises the actual schema & SQL.
    app._mailbox = MailboxStore(tmp_path / "inbox.db")

    # Fake outbound queue so we can assert on TX replies.
    app._outbound_queue = _FakeOutboundQueue()

    # No UI in this scope — _refresh_inbox_ui no-ops when ui_state is None.
    app._ui_state = None

    return app


def _frame() -> DecodedFrame:
    """A minimal DecodedFrame for test construction.

    Only frequency + snr + received_at matter to the dispatcher;
    the text content is filled in via the ParsedFrame wrapper.
    """
    return DecodedFrame(
        text="",
        raw="",
        snr_db=-3,
        frequency_hz=1500.0,
        dt_seconds=0.0,
        submode=0,
        quality=0,
        frame_type=0,
        utc_seconds_of_day=0,
        received_at=1700000000.0,
    )


def _parsed(
    *,
    kind: FrameKind,
    from_call: str,
    to_call: str,
    body: str,
    is_for_us: bool,
) -> ParsedFrame:
    return ParsedFrame(
        decoded=_frame(),
        kind=kind,
        from_call=from_call,
        to_call=to_call,
        grid=None,
        body=body,
        is_for_us=is_for_us,
    )


# ── Inbound MSG (store as UNREAD + auto-ACK) ────────────────────────


def test_inbound_msg_stores_unread(tmp_path: Path):
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_MESSAGE,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="MSG hello mike",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())

    # One UNREAD row should be in the mailbox.
    rows = app._mailbox.list_inbox()
    assert len(rows) == 1
    assert rows[0].type == TYPE_UNREAD
    assert rows[0].from_call == "KC1WDO"
    assert rows[0].text == "hello mike"


def test_inbound_msg_queues_auto_ack(tmp_path: Path):
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_MESSAGE,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="MSG hello",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())

    enq = app._outbound_queue.enqueued
    assert len(enq) == 1
    assert enq[0].text == "KC1WDO ACK"
    assert enq[0].kind is OutboundKind.REPLY
    assert enq[0].to_call == "KC1WDO"


def test_inbound_msg_not_for_us_is_ignored(tmp_path: Path):
    """A frame addressed to someone else (we just heard it) must not
    be added to our inbox."""
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_MESSAGE,
        from_call="KC1WDO",
        to_call="W4MSI",
        body="MSG hi",
        is_for_us=False,
    )
    app._dispatch_inbox(parsed, _frame())
    assert app._mailbox.list_inbox() == []
    assert app._outbound_queue.enqueued == []


# ── Inbound MSG TO: (store as STORE for recipient + auto-ACK) ──────


def test_inbound_msg_to_stores_for_recipient(tmp_path: Path):
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="MSG TO:W4MSI dinner at 7",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())

    held = app._mailbox.list_holding_for("W4MSI")
    assert len(held) == 1
    assert held[0].type == TYPE_STORE
    assert held[0].from_call == "KC1WDO"
    assert held[0].to_call == "W4MSI"
    assert held[0].text == "dinner at 7"


def test_inbound_msg_to_queues_auto_ack(tmp_path: Path):
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="MSG TO:W4MSI hello there",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())

    enq = app._outbound_queue.enqueued
    assert len(enq) == 1
    assert enq[0].text == "KC1WDO ACK"
    assert enq[0].to_call == "KC1WDO"


def test_inbound_msg_to_does_not_create_inbox_row(tmp_path: Path):
    """MSG TO: is for a third party — must NOT land in our UNREAD inbox."""
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="MSG TO:W4MSI hi",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())
    assert app._mailbox.list_inbox() == []  # no UNREAD/READ rows


# ── QUERY MSGS direct-to-us ────────────────────────────────────────


def test_query_msgs_direct_with_holding_replies_msg_id(tmp_path: Path):
    app = _make_app(tmp_path)
    # We're holding for KC1WDO.
    rid = app._mailbox.add_local_store(
        recipient_call="KC1WDO", text="hello future you", our_call="W5DMH",
    )
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="QUERY MSGS",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())

    enq = app._outbound_queue.enqueued
    assert len(enq) == 1
    assert enq[0].text == f"KC1WDO MSG {rid}"
    assert enq[0].to_call == "KC1WDO"


def test_query_msgs_direct_with_no_holding_replies_no(tmp_path: Path):
    """Direct-to-us QUERY MSGS with empty holding → 'NO'."""
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="QUERY MSGS",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())

    enq = app._outbound_queue.enqueued
    assert len(enq) == 1
    assert enq[0].text == "KC1WDO NO"


# ── QUERY MSGS @ALLCALL broadcast ──────────────────────────────────


def test_query_msgs_allcall_with_holding_replies(tmp_path: Path):
    """We're holding for the asker — even though it's a broadcast,
    we should reply because we have something for them."""
    app = _make_app(tmp_path)
    rid = app._mailbox.add_local_store(
        recipient_call="KC1WDO", text="for them", our_call="W5DMH",
    )
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="@ALLCALL",
        body="QUERY MSGS",
        is_for_us=False,
    )
    app._dispatch_inbox(parsed, _frame())

    enq = app._outbound_queue.enqueued
    assert len(enq) == 1
    assert enq[0].text == f"KC1WDO MSG {rid}"


def test_query_msgs_allcall_with_no_holding_silent(tmp_path: Path):
    """Empty holding + @ALLCALL → silent (don't pollute the band)."""
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="@ALLCALL",
        body="QUERY MSGS",
        is_for_us=False,
    )
    app._dispatch_inbox(parsed, _frame())
    assert app._outbound_queue.enqueued == []


# ── QUERY MSG <id> (deliver held message body) ─────────────────────


def test_query_msg_id_delivers_body(tmp_path: Path):
    app = _make_app(tmp_path)
    rid = app._mailbox.add_local_store(
        recipient_call="KC1WDO",
        text="here is the secret",
        our_call="W5DMH",
    )
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body=f"QUERY MSG {rid}",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())

    enq = app._outbound_queue.enqueued
    assert len(enq) == 1
    assert enq[0].text == f"KC1WDO MSG {rid} here is the secret"


def test_query_msg_id_refuses_to_deliver_unread_inbox(tmp_path: Path):
    """An UNREAD row in our inbox is NOT held mail — refuse to deliver."""
    app = _make_app(tmp_path)
    rid = app._mailbox.add_unread(
        from_call="KC1WDO",
        text="our private mail",
        our_call="W5DMH",
    )
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body=f"QUERY MSG {rid}",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())
    # No reply enqueued; row was UNREAD, not STORE.
    assert app._outbound_queue.enqueued == []


def test_query_msg_id_refuses_to_deliver_to_wrong_callsign(tmp_path: Path):
    """STORE row for W4MSI being asked about by KC1WDO — refuse."""
    app = _make_app(tmp_path)
    rid = app._mailbox.add_local_store(
        recipient_call="W4MSI", text="x", our_call="W5DMH",
    )
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body=f"QUERY MSG {rid}",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())
    assert app._outbound_queue.enqueued == []


def test_query_msg_id_unknown_id_silent(tmp_path: Path):
    """Asking about a non-existent id should not crash and not
    spew a reply."""
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_COMMAND,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="QUERY MSG 999",
        is_for_us=True,
    )
    app._dispatch_inbox(parsed, _frame())
    assert app._outbound_queue.enqueued == []


# ── ACK back-correlation marks STORE → DELIVERED ──────────────────


def test_maybe_mark_inbox_delivered_recognizes_held_mail_format(
    tmp_path: Path,
):
    """When an outbound's text matches '<call> MSG <id> <body>',
    _maybe_mark_inbox_delivered transitions the inbox row."""
    app = _make_app(tmp_path)
    rid = app._mailbox.add_local_store(
        recipient_call="KC1WDO", text="x", our_call="W5DMH",
    )

    # Stub outbound_queue.get to return a "MSG <id>" delivery row
    class _Stub:
        text = f"KC1WDO MSG {rid} secret payload"
    saved_queue = app._outbound_queue

    def fake_get(_id):
        return _Stub()
    saved_queue.get = fake_get  # type: ignore[assignment]

    app._maybe_mark_inbox_delivered(outbound_id=42)

    rec = app._mailbox.get(rid)
    assert rec is not None
    assert rec.type == "DELIVERED"


def test_maybe_mark_inbox_delivered_skips_normal_messages(tmp_path: Path):
    """A regular directed text message (no 'MSG <id>' prefix) must
    not trigger any inbox state change."""
    app = _make_app(tmp_path)
    rid = app._mailbox.add_local_store(
        recipient_call="KC1WDO", text="x", our_call="W5DMH",
    )

    class _Stub:
        text = "KC1WDO HW CPY?"

    def fake_get(_id):
        return _Stub()
    app._outbound_queue.get = fake_get  # type: ignore[assignment]

    app._maybe_mark_inbox_delivered(outbound_id=42)
    rec = app._mailbox.get(rid)
    assert rec is not None
    assert rec.type == "STORE"  # unchanged


# ── Defensive: dispatcher works with no mailbox open ───────────────


def test_dispatch_with_no_mailbox_does_not_crash(tmp_path: Path):
    app = _make_app(tmp_path)
    app._mailbox.close()
    app._mailbox = None  # store failed to open at startup

    parsed = _parsed(
        kind=FrameKind.DIRECTED_MESSAGE,
        from_call="KC1WDO",
        to_call="W5DMH",
        body="MSG hello",
        is_for_us=True,
    )
    # Should not raise.
    app._dispatch_inbox(parsed, _frame())
    # And nothing got enqueued (we can't ACK without a reliable record).
    assert app._outbound_queue.enqueued == []


def test_dispatch_with_no_from_call_is_ignored(tmp_path: Path):
    """Anonymous frames don't drive inbox state."""
    app = _make_app(tmp_path)
    parsed = _parsed(
        kind=FrameKind.DIRECTED_MESSAGE,
        from_call="",  # missing
        to_call="W5DMH",
        body="MSG hi",
        is_for_us=True,
    )
    # Note: ParsedFrame's from_call is a string field, so we pass
    # empty string. The dispatcher checks for falsy from_call.
    app._dispatch_inbox(parsed, _frame())
    assert app._mailbox.list_inbox() == []
    assert app._outbound_queue.enqueued == []


# ── Inbox delete callback ─────────────────────────────────────────────


def test_delete_inbox_row_sync_removes_row_from_store(tmp_path: Path):
    """The app's _delete_inbox_row_sync callback (wired into the
    router) hard-deletes a row from the mailbox store. Returns True
    on success; on retry the same row is gone (idempotent)."""
    app = _make_app(tmp_path)
    rid = app._mailbox.add_unread(
        from_call="K1ABC", text="hello", our_call="W5DMH",
    )
    assert app._mailbox.get(rid) is not None

    # Delete via the router-facing callback
    assert app._delete_inbox_row_sync(rid) is True
    # Row is gone
    assert app._mailbox.get(rid) is None
    # Idempotent — repeat returns False (no row to delete)
    assert app._delete_inbox_row_sync(rid) is False


def test_delete_inbox_row_sync_returns_false_when_no_mailbox(tmp_path: Path):
    """No mailbox store wired → callback returns False, doesn't crash."""
    app = _make_app(tmp_path)
    app._mailbox = None
    assert app._delete_inbox_row_sync(123) is False


def test_delete_inbox_row_sync_swallows_store_errors(tmp_path: Path):
    """If the mailbox raises, the callback returns False rather than
    crashing the input thread. The router's exception handler will
    log; the operator's keypress still feels responsive (the in-
    memory drop already happened in the router)."""
    app = _make_app(tmp_path)

    class _FailingStore:
        def delete(self, row_id):
            raise RuntimeError("simulated db error")
    app._mailbox = _FailingStore()
    # Should NOT raise
    assert app._delete_inbox_row_sync(1) is False
