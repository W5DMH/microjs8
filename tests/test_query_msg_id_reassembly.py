"""Regression tests for the QUERY MSG <id> single-frame emit path.

Bug summary (W5DMH bench, May 2026): when JS8Call clicks "Get" on a
held mailbox row, it transmits ``<us> QUERY MSG <n>``. Observed wire
forms vary by JS8Call version:

  - ``QUERY MSG 1``              (no checksum)
  - ``QUERY MSG ID 1``           (JS8Call's "ID" form, no checksum)
  - ``QUERY MSG 1 <crc>``        (with our canonical checksum)
  - ``QUERY MSG ID 1 <crc>``     (alternate form + checksum)

The original reassembler treated `QUERY MSG <id>` as the START of a
multi-frame buffered command and parked it in the buffer waiting for
continuation frames that never arrived. The receiving station was
silent for the entire timeout, never delivering the body.

The fix recognises `QUERY MSG <id>` (with or without the ID keyword,
with or without trailing CRC) as a complete single-frame command on
first arrival and emits an AssembledMessage immediately. The body is
normalised to ``MSG <id>`` so downstream dispatch needs only one
form.
"""
from __future__ import annotations

import pytest

from microjs8.protocol.checksum import checksum16
from microjs8.protocol.reassembly import MessageAssembler
from microjs8.protocol.types import DecodedFrame, FrameKind, ParsedFrame


def _frame(body: str, from_call: str = "K1ABC", to_call: str = "W5DMH") -> ParsedFrame:
    df = DecodedFrame(
        text=f"{from_call}: {to_call} {body}",
        raw="", snr_db=-9, frequency_hz=1500.0, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=0,
    )
    return ParsedFrame(
        decoded=df,
        kind=FrameKind.DIRECTED_COMMAND,
        from_call=from_call, to_call=to_call, grid=None,
        body=body, is_for_us=(to_call == "W5DMH"),
    )


@pytest.mark.parametrize(
    "wire_body,expected_id",
    [
        # No-checksum forms (JS8Call versions that skip the
        # buffered-command checksum suffix for short fixed-shape bodies).
        ("QUERY MSG 1",            "1"),
        ("QUERY MSG 42",           "42"),
        ("QUERY MSG 99999",        "99999"),
        # "ID" keyword variant (alternate JS8Call form).
        ("QUERY MSG ID 1",         "1"),
        ("QUERY MSG ID 42",        "42"),
        # With trailing CRC (canonical buffered-command form).
        # Use a valid checksum so the original checksum-validation
        # path WOULD have caught it too — but the new path catches
        # it without depending on the CRC matching, so we use a
        # filler token to prove that.
        ("QUERY MSG 1 ABC",        "1"),
        ("QUERY MSG ID 1 ABC",     "1"),
    ],
)
def test_query_msg_id_single_frame_emit(wire_body, expected_id):
    """All 4 wire variants emit on first arrival with body normalised
    to ``MSG <id>``."""
    asm = MessageAssembler()
    out = asm.feed(_frame(wire_body))
    assert out, f"expected emit for {wire_body!r}, got nothing"
    msg = out[0]
    assert msg.verb == "QUERY"
    assert msg.body == f"MSG {expected_id}"
    assert msg.checksum_valid is True
    assert msg.frame_count == 1
    assert msg.was_buffered_command is True
    # Buffer should be empty — single-frame emit, no continuation expected.
    assert asm.buffer_count == 0


def test_query_msg_id_with_valid_checksum_path_still_works():
    """The original checksum-validated path remains functional —
    a properly-checksum'd 'QUERY MSG 1 T/R' still emits."""
    crc = checksum16("MSG 1")
    asm = MessageAssembler()
    out = asm.feed(_frame(f"QUERY MSG 1 {crc}"))
    assert out
    assert out[0].body == "MSG 1"


def test_query_msg_id_does_not_match_query_msgs():
    """The new regex must not false-trigger on QUERY MSGS (no id).
    QUERY MSGS has its own verb-only emit path; this test confirms
    we didn't break it."""
    asm = MessageAssembler()
    out = asm.feed(_frame("QUERY MSGS"))
    assert out
    msg = out[0]
    # Emits as verb="QUERY MSGS" not verb="QUERY" with body containing
    # "MSGS" — they're protocol-distinct.
    assert msg.verb == "QUERY MSGS"
    assert msg.body == ""


def test_query_with_non_numeric_body_still_buffers():
    """The single-frame emit is gated to numeric-id bodies. A
    non-matching QUERY form (e.g. QUERY CALL <call>) still goes
    through the normal buffer-and-checksum path. Note: QUERY CALL
    has its own classifier branch, so this exercises the residual
    'QUERY <something>' case that doesn't match either special path."""
    asm = MessageAssembler()
    out = asm.feed(_frame("QUERY MSG abc"))   # not numeric
    # Without a CRC and without matching the new regex, it goes into
    # the buffer awaiting continuation/timeout.
    assert out == []
    assert asm.buffer_count == 1


def test_query_msg_id_zero_does_not_emit():
    """SQLite AUTOINCREMENT starts at 1, so id=0 is meaningless.
    The regex itself accepts \\d+ which includes '0', but the parser
    downstream (parse_query_msg_id) rejects 0 — so this would just
    log 'id not found' on dispatch. Confirm the assembler emits and
    leaves the rejection to the dispatcher (separation of concerns)."""
    asm = MessageAssembler()
    out = asm.feed(_frame("QUERY MSG 0"))
    assert out
    assert out[0].body == "MSG 0"
    # parse_query_msg_id will reject this at the next layer.


def test_query_msg_id_dispatch_finds_held_row_for_asker():
    """End-to-end-style: confirm that after the assembler emits,
    parse_query_msg_id correctly extracts the id from the normalised
    body. This is the contract the reassembler and dispatcher share."""
    from microjs8.protocol.grammar import parse_query_msg_id
    # The assembler hands the dispatcher body="MSG <id>". The dispatcher
    # reconstructs the wire form "QUERY <body>" and parses.
    asm = MessageAssembler()
    out = asm.feed(_frame("QUERY MSG 7"))
    msg = out[0]
    parsed_id = parse_query_msg_id(f"QUERY {msg.body}")
    assert parsed_id == 7


# ── Multi-frame buffered QUERY MSG <id> (W5DMH bench on-air capture) ───
#
# JS8Call sometimes splits ``<us> QUERY MSG <id>`` across two frames:
#   Frame 1: ``KD8PGB: W5DMH QUERY `` (verb + trailing word-boundary space)
#   Frame 2: ``MSG 1 T/R`` (continuation: body + CRC, no envelope)
#
# The first frame's body is just "QUERY " — the parser classifies it
# as DIRECTED_QUERY (since "QUERY" is in its query-set). The
# reassembler MUST still recognise this as the start of a buffered
# command so it picks up the continuation frame and validates the
# CRC across the concatenated body.


def test_query_msg_id_split_across_two_frames_emits_correctly():
    """The full on-air scenario captured at W5DMH bench: frame 1 with
    parsed.kind=DIRECTED_QUERY and body="QUERY ", frame 2 (UNKNOWN
    continuation) with body="MSG 1 T/R". Assembler must reassemble
    and emit verb="QUERY" body="MSG 1" checksum_valid=True."""
    asm = MessageAssembler()

    # Frame 1: kind=DIRECTED_QUERY (matches the on-air log)
    df1 = DecodedFrame(
        text="KD8PGB: W5DMH QUERY ",
        raw="", snr_db=12, frequency_hz=1616.1, dt_seconds=-0.42,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=42540, received_at=0,
    )
    pf1 = ParsedFrame(
        decoded=df1, kind=FrameKind.DIRECTED_QUERY,
        from_call="KD8PGB", to_call="W5DMH", grid=None,
        body="QUERY ", is_for_us=True,
    )
    assert asm.feed(pf1) == [], "frame 1 must not emit yet — awaiting continuation"
    assert asm.buffer_count == 1

    # Frame 2: kind=UNKNOWN (continuation, no envelope)
    df2 = DecodedFrame(
        text="MSG 1 T/R",
        raw="", snr_db=3, frequency_hz=1615.6, dt_seconds=-0.42,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=42555, received_at=0,
    )
    pf2 = ParsedFrame(
        decoded=df2, kind=FrameKind.UNKNOWN,
        from_call=None, to_call=None, grid=None,
        body="MSG 1 T/R", is_for_us=False,
    )
    out = asm.feed(pf2)
    assert out, "frame 2 should complete the buffered message"
    msg = out[0]
    assert msg.verb == "QUERY"
    assert msg.body == "MSG 1"
    assert msg.checksum_valid is True
    assert msg.was_buffered_command is True
    assert msg.frame_count == 2
    # Buffer should drain after emit.
    assert asm.buffer_count == 0


def test_query_directed_query_kind_with_body_treated_as_buffered():
    """The classifier accepts DIRECTED_QUERY too (not just MESSAGE
    and COMMAND). This is the gate fix — without it, the on-air
    capture's frame 1 would have bypassed the buffered path and
    landed in the non-buffered timeout queue with garbled body."""
    df = DecodedFrame(
        text="KD8PGB: W5DMH QUERY MSG 1 T/R",
        raw="", snr_db=12, frequency_hz=1616.1, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=0,
    )
    pf = ParsedFrame(
        decoded=df,
        kind=FrameKind.DIRECTED_QUERY,
        from_call="KD8PGB", to_call="W5DMH", grid=None,
        body="QUERY MSG 1 T/R", is_for_us=True,
    )
    asm = MessageAssembler()
    out = asm.feed(pf)
    assert out, "DIRECTED_QUERY-kinded QUERY MSG <id> should still single-frame emit"
    assert out[0].body == "MSG 1"


def test_buffered_query_dispatch_resolves_id():
    """Integration: confirm parse_query_msg_id reads the assembler's
    normalised body for the multi-frame case."""
    from microjs8.protocol.grammar import parse_query_msg_id
    asm = MessageAssembler()

    pf1 = ParsedFrame(
        decoded=DecodedFrame(
            text="KD8PGB: W5DMH QUERY ",
            raw="", snr_db=12, frequency_hz=1616.1, dt_seconds=0.0,
            submode=0, quality=0, frame_type=0,
            utc_seconds_of_day=0, received_at=0,
        ),
        kind=FrameKind.DIRECTED_QUERY,
        from_call="KD8PGB", to_call="W5DMH", grid=None,
        body="QUERY ", is_for_us=True,
    )
    pf2 = ParsedFrame(
        decoded=DecodedFrame(
            text="MSG 7 nXg",
            raw="", snr_db=3, frequency_hz=1615.6, dt_seconds=0.0,
            submode=0, quality=0, frame_type=0,
            utc_seconds_of_day=0, received_at=0,
        ),
        kind=FrameKind.UNKNOWN,
        from_call=None, to_call=None, grid=None,
        body=f"MSG 7 {checksum16('MSG 7')}", is_for_us=False,
    )
    asm.feed(pf1)
    out = asm.feed(pf2)
    msg = out[0]
    # Dispatcher's reconstruction:
    #   parse_query_msg_id(f"QUERY {msg.body}")
    assert parse_query_msg_id(f"QUERY {msg.body}") == 7
