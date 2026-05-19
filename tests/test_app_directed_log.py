"""Tests for the decode-handler's directed-activity logging path.

Regression coverage for the bug where HEARTBEAT replies (and other
non-ACK single-frame directed responses) were decoded correctly but
did NOT appear in the DIRECTED chat view. The decode handler's
filter was overly narrow (``kind is FrameKind.ACK``) when it should
have logged ALL non-buffered single-frame frames directed at us.

These tests drive ``_on_decoded_frame`` end-to-end with synthetic
DecodedFrames and assert on the resulting DirectedActivityLog state.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from microjs8.app import MicroJS8App
from microjs8.config import Config, StationConfig
from microjs8.protocol.types import DecodedFrame
from microjs8.store.inbox import MailboxStore
from microjs8.tx.queue import OutboundKind


@dataclass
class _Enq:
    text: str
    kind: OutboundKind
    to_call: object


class _FakeOutboundQueue:
    def __init__(self):
        self.enqueued: list[_Enq] = []
        self._next = 200

    def enqueue_for_encoding(self, text, kind=None, to_call=None):
        self.enqueued.append(_Enq(text=text, kind=kind, to_call=to_call))
        n = self._next
        self._next += 1
        return n

    def get(self, message_id):
        return None

    def record_ack(self, ack_from_call):
        return None


def _make_app(tmp_path: Path) -> MicroJS8App:
    cfg = Config(station=StationConfig(callsign="W5DMH", grid="EN83"))
    app = MicroJS8App(cfg, headless=True)
    app._mailbox = MailboxStore(tmp_path / "inbox.db")
    app._outbound_queue = _FakeOutboundQueue()
    # Match the live wiring: app constructs a DirectedActivityLog in
    # __init__ but doesn't expose it on the UIState in headless mode.
    # The log itself accumulates entries regardless of UI presence;
    # tests inspect it directly.
    return app


def _hb_reply_frame(from_call: str, body: str) -> DecodedFrame:
    """Build a DecodedFrame matching what the modem hands us for a
    HEARTBEAT reply directed back to W5DMH. The body is the post-
    envelope content the operator sees in the log; the parser
    constructs from_call / to_call from the JS8 envelope.

    On-air shape: '<from>: W5DMH HEARTBEAT SNR -09'  →  the parser
    strips the from-envelope and the to-call (W5DMH), leaving the
    body. We hand the parser the full text so it can do that.
    """
    return DecodedFrame(
        text=f"{from_call}: W5DMH {body}",
        raw="",
        snr_db=-13,
        frequency_hz=781.2,
        dt_seconds=-0.30,
        submode=0,
        quality=0,
        frame_type=0,
        utc_seconds_of_day=51810,
        received_at=1700000000.0,
    )


def _directed_in_frame(from_call: str, verb_and_body: str) -> DecodedFrame:
    """Generic single-frame directed frame to us."""
    return DecodedFrame(
        text=f"{from_call}: W5DMH {verb_and_body}",
        raw="", snr_db=5, frequency_hz=1500.0, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )


def _heard_broadcast_frame(from_call: str) -> DecodedFrame:
    """An @HB heartbeat broadcast — NOT directed to us. Should NOT
    land in the directed log (it's a routine background sighting,
    surfaced on the HEARD screen instead)."""
    return DecodedFrame(
        text=f"{from_call}: @HB HEARTBEAT FN42",
        raw="", snr_db=10, frequency_hz=1500.0, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )


# ── HEARTBEAT replies (the reported bug) ────────────────────────────


def _drain_assembler(app) -> None:
    """Force the assembler to drain any non-buffered buffers as if
    the 40 s timeout had elapsed, then dispatch the emits through
    ``_dispatch_assembled``. Mimics the production sweep-loop's
    ``sweep_completed`` → ``_dispatch_assembled`` step that fires on
    a background thread but isn't running in unit tests.

    Used in tests where a frame goes through the non-buffered
    reassembly path (HEARTBEAT directed-to-us, free-text directed
    messages) — the buffer holds the body until timeout, so the
    test has to age and drain explicitly to see the directed-log
    entry produced.
    """
    for buf in app._assembler._buffers.values():
        # Force the buffer's last_frame_at into the past, beyond the
        # non-buffered grace period. The assembler emits on the next
        # sweep.
        buf.last_frame_at -= 120
    try:
        completions = app._assembler.sweep_completed()
    except Exception:
        completions = []
    for assembled in completions:
        app._dispatch_assembled(assembled, None)


def test_hb_reply_lands_in_directed_log(tmp_path):
    """HEARTBEAT replies decoded by us should appear in the
    DIRECTED activity log. With the May 2026 update, directed
    heartbeats are buffered for ~40 s to collect any JS8Call
    ``MSG ID <n>`` continuation that may follow; emission happens
    via the reassembler's sweep (in production: background thread;
    in tests: ``_drain_assembler`` helper)."""
    app = _make_app(tmp_path)
    frame = _hb_reply_frame("KI4HDU", "HEARTBEAT SNR -09")
    app._on_decoded_frame(frame)
    # Immediate dispatch is skipped for directed heartbeats — the
    # entry only lands after the buffer drains.
    assert len(app._directed_activity.snapshot()) == 0
    _drain_assembler(app)
    entries = app._directed_activity.snapshot()
    assert len(entries) == 1
    e = entries[0]
    assert e.other_call == "KI4HDU"
    assert e.verb == "HEARTBEAT"
    assert "SNR -09" in e.body


def test_multiple_hb_replies_all_logged(tmp_path):
    """Two stations replying to one of our heartbeats — both must
    appear in the log in receive order after the assembler drains."""
    app = _make_app(tmp_path)
    app._on_decoded_frame(_hb_reply_frame("KI4HDU", "HEARTBEAT SNR -09"))
    app._on_decoded_frame(_hb_reply_frame("KD8GIJ", "HEARTBEAT SNR -21"))
    _drain_assembler(app)
    entries = app._directed_activity.snapshot()
    assert len(entries) == 2
    assert entries[0].other_call == "KI4HDU"
    assert entries[1].other_call == "KD8GIJ"


# ── Other single-frame protocol responses ──────────────────────────


def test_snr_reply_lands_in_directed_log(tmp_path):
    """SNR replies to a SNR? we sent earlier."""
    app = _make_app(tmp_path)
    app._on_decoded_frame(_directed_in_frame("K1ABC", "SNR -11"))
    entries = app._directed_activity.snapshot()
    assert len(entries) == 1
    assert entries[0].other_call == "K1ABC"
    assert entries[0].verb == "SNR"


def test_grid_reply_lands_in_directed_log(tmp_path):
    app = _make_app(tmp_path)
    app._on_decoded_frame(_directed_in_frame("K1ABC", "GRID FN42"))
    entries = app._directed_activity.snapshot()
    assert len(entries) == 1
    assert entries[0].other_call == "K1ABC"
    assert entries[0].verb == "GRID"
    assert "FN42" in entries[0].body


def test_ack_still_lands_in_directed_log(tmp_path):
    """Regression: the ACK path that already worked must still
    work after the filter loosened."""
    app = _make_app(tmp_path)
    app._on_decoded_frame(_directed_in_frame("K1ABC", "ACK"))
    entries = app._directed_activity.snapshot()
    assert len(entries) == 1
    assert entries[0].other_call == "K1ABC"
    assert entries[0].verb == "ACK"


# ── Things that should NOT land in the directed log ────────────────


def test_hb_broadcast_does_not_land_in_directed_log(tmp_path):
    """A routine @HB broadcast from a remote station goes to the
    HEARD screen, NOT the DIRECTED log. The check that distinguishes
    them is ``parsed.is_for_us``."""
    app = _make_app(tmp_path)
    app._on_decoded_frame(_heard_broadcast_frame("KI4HDU"))
    assert app._directed_activity.snapshot() == ()


def test_directed_frame_to_other_call_not_logged(tmp_path):
    """A directed frame between two OTHER stations (we just happen
    to decode it) must not appear in OUR directed log."""
    app = _make_app(tmp_path)
    # KI4HDU → K1ABC, not us
    frame = DecodedFrame(
        text="KI4HDU: K1ABC HEARTBEAT SNR -09",
        raw="", snr_db=-13, frequency_hz=1500.0, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )
    app._on_decoded_frame(frame)
    assert app._directed_activity.snapshot() == ()


def test_buffered_msg_does_not_double_log_at_decode_handler(tmp_path):
    """Buffered MSG frames go through the reassembler — the decode
    handler must NOT log them on the way in (the assembled-dispatch
    path is where MSG/STORE land in the mailbox; the directed log
    sees the assembled body separately for non-MSG buffered verbs).
    """
    app = _make_app(tmp_path)
    # First frame of a buffered MSG (will sit in the assembler, not
    # complete on this decode).
    frame = DecodedFrame(
        text="KI4HDU: W5DMH MSG HELLO",
        raw="", snr_db=5, frequency_hz=1500.0, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=1700000000.0,
    )
    app._on_decoded_frame(frame)
    # The frame is for us and parses as a buffered MSG starter.
    # is_buffered_protocol_frame returns True → the decode handler
    # path does NOT log to the directed log. (The MSG eventually
    # lands in the mailbox via the assembled-dispatch path when the
    # checksum frame arrives — not tested here.)
    assert app._directed_activity.snapshot() == ()


# ── Deferred dispatch for directed HEARTBEAT (May 2026 UX fix) ──────


def test_directed_heartbeat_no_immediate_dispatch(tmp_path):
    """The May 2026 UX fix: directed HEARTBEAT replies are NOT logged
    immediately on first-frame decode. They sit in the reassembler's
    buffer until the timeout-emit (40 s) so any JS8Call ``MSG ID <n>``
    continuation frame can attach. Operator sees ONE complete entry
    rather than a partial entry that updates 30 s later (the partial
    led operators to assume no message was pending and move away
    before the update fired)."""
    app = _make_app(tmp_path)
    app._on_decoded_frame(_hb_reply_frame("KD8PGB", "HEARTBEAT SNR +04"))
    # No immediate log entry — buffer holds it.
    assert len(app._directed_activity.snapshot()) == 0
    # Reassembler has the buffer.
    assert app._assembler.buffer_count == 1


def test_directed_heartbeat_emits_full_body_after_continuation(tmp_path):
    """The full JS8Call protocol case: heartbeat first frame +
    ``MSG ID 61`` continuation reassembles and the operator sees ONE
    log entry showing the complete content."""
    app = _make_app(tmp_path)
    app._on_decoded_frame(_hb_reply_frame("KD8PGB", "HEARTBEAT SNR +04"))
    # Continuation arrives ~15 s later as an UNKNOWN-kind frame.
    from microjs8.protocol.types import FrameKind
    # Reuse the same frame builder pattern: minimal DecodedFrame with
    # the continuation body and matching audio offset.
    cont = DecodedFrame(
        text="MSG ID 61", raw="", snr_db=2, frequency_hz=781.2,
        dt_seconds=-0.42, submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=0,
    )
    app._on_decoded_frame(cont)
    _drain_assembler(app)
    entries = app._directed_activity.snapshot()
    assert len(entries) == 1, (
        f"expected single combined entry; got {len(entries)}: "
        f"{[(e.verb, e.body) for e in entries]}"
    )
    e = entries[0]
    assert e.other_call == "KD8PGB"
    assert e.verb == "HEARTBEAT"
    # Body contains BOTH the original SNR and the continuation MSG ID.
    assert "SNR +04" in e.body
    assert "MSG ID 61" in e.body
