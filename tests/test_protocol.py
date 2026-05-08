"""Tests for minijs8.protocol.grammar.

Exercises every JS8 message kind we care about plus malformed inputs.
We construct synthetic ``DecodedFrame`` records and feed them through
``parse()``.
"""

from __future__ import annotations

import time

import pytest

from minijs8.protocol.grammar import parse
from minijs8.protocol.types import DecodedFrame, FrameKind


def _frame(text: str, snr: int = -10, freq: float = 1500.0) -> DecodedFrame:
    return DecodedFrame(
        text=text, raw="", snr_db=snr, frequency_hz=freq,
        dt_seconds=0.0, submode=0, quality=80, frame_type=0,
        utc_seconds_of_day=0, received_at=time.time(),
    )


# ── Heartbeat ───────────────────────────────────────────────────────


def test_heartbeat_basic():
    p = parse(_frame("K8XYZ @HB EN82"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.from_call == "K8XYZ"
    assert p.grid == "EN82"
    assert p.to_call == "@HB"
    assert not p.is_for_us


def test_heartbeat_with_subsquare_grid():
    p = parse(_frame("K8XYZ @HB EN82dj"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.grid == "EN82dj"


# ── CQ ──────────────────────────────────────────────────────────────


def test_cq_basic():
    p = parse(_frame("K8XYZ: CQ EN82"), our_callsign="K1ABC")
    assert p.kind is FrameKind.CQ
    assert p.from_call == "K8XYZ"
    assert p.grid == "EN82"
    assert p.to_call == "@ALLCALL"


def test_cq_with_allcall_prefix():
    p = parse(_frame("K8XYZ: @ALLCALL CQ EN82"), our_callsign="K1ABC")
    assert p.kind is FrameKind.CQ
    assert p.from_call == "K8XYZ"
    assert p.grid == "EN82"


def test_cq_without_grid():
    """CQ without grid is still a valid CQ."""
    p = parse(_frame("K8XYZ: CQ"), our_callsign="K1ABC")
    assert p.kind is FrameKind.CQ
    assert p.from_call == "K8XYZ"
    assert p.grid is None


# ── Directed messages ───────────────────────────────────────────────


def test_directed_message_to_us_is_for_us():
    p = parse(_frame("K8XYZ: K1ABC HELLO HOW ARE YOU"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_MESSAGE
    assert p.from_call == "K8XYZ"
    assert p.to_call == "K1ABC"
    assert p.body == "HELLO HOW ARE YOU"
    assert p.is_for_us


def test_directed_message_to_other_is_not_for_us():
    p = parse(_frame("K8XYZ: VE3ABC HELLO"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_MESSAGE
    assert not p.is_for_us


def test_directed_to_us_case_insensitive():
    p = parse(_frame("K8XYZ: k1abc HELLO"), our_callsign="K1ABC")
    assert p.is_for_us


def test_directed_message_when_we_are_unconfigured():
    """No is_for_us match when our_callsign is None."""
    p = parse(_frame("K8XYZ: K1ABC HELLO"), our_callsign=None)
    assert p.kind is FrameKind.DIRECTED_MESSAGE
    assert not p.is_for_us


# ── Queries / commands ──────────────────────────────────────────────


def test_directed_query_snr():
    p = parse(_frame("K8XYZ: K1ABC SNR?"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_QUERY
    assert p.is_for_us


def test_directed_query_grid():
    p = parse(_frame("K8XYZ: K1ABC GRID?"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_QUERY


def test_directed_query_agn():
    """AGN? = "say it again", common in JS8."""
    p = parse(_frame("K8XYZ: K1ABC AGN?"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_QUERY


def test_directed_command_info():
    p = parse(_frame("K8XYZ: K1ABC INFO TEMP IS 72F"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_COMMAND
    assert p.body == "INFO TEMP IS 72F"


def test_directed_ack():
    p = parse(_frame("K8XYZ: K1ABC ACK -12"), our_callsign="K1ABC")
    assert p.kind is FrameKind.ACK
    assert p.is_for_us


# ── Allcall ─────────────────────────────────────────────────────────


def test_allcall_broadcast():
    """`<from>: @ALLCALL <text>` is an allcall, not a directed message."""
    p = parse(_frame("K8XYZ: @ALLCALL ANYBODY HOME"), our_callsign="K1ABC")
    assert p.kind is FrameKind.ALLCALL
    assert p.from_call == "K8XYZ"
    assert p.to_call == "@ALLCALL"
    assert p.body == "ANYBODY HOME"
    assert not p.is_for_us


# ── Robustness ──────────────────────────────────────────────────────


def test_unparseable_returns_unknown():
    """Random garbage doesn't crash; classified as UNKNOWN."""
    p = parse(_frame("???? !!!! ####"), our_callsign="K1ABC")
    assert p.kind is FrameKind.UNKNOWN
    assert p.from_call is None


def test_empty_text_classified_as_unknown():
    p = parse(_frame(""), our_callsign="K1ABC")
    assert p.kind is FrameKind.UNKNOWN


def test_known_real_decode_from_prototype():
    """The Step-0 prototype actually decoded this. We must classify it."""
    p = parse(_frame("KC3VYM: WK8D INFO"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_COMMAND
    assert p.from_call == "KC3VYM"
    assert p.to_call == "WK8D"
    assert p.body == "INFO"


def test_callsign_with_slash_portable():
    """Portable suffixes like K1ABC/M must round-trip."""
    p = parse(_frame("K1ABC/M @HB FN42"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.from_call == "K1ABC/M"


# ── Real-world traffic from on-air journal capture (Apr 2026, 7.078 MHz) ────
# These are golden samples — actual decoded JS8 frames. The original
# v1 grammar misclassified all of these, so the regression suite below
# exists to make sure they keep parsing correctly.


def test_real_modern_heartbeat_broadcast_k3clr():
    """Modern JS8Call HB broadcast: '<call>: @HB HEARTBEAT <grid>'."""
    p = parse(_frame("K3CLR: @HB HEARTBEAT FM18"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT, f"expected HEARTBEAT, got {p.kind}"
    assert p.from_call == "K3CLR"
    assert p.to_call == "@HB"
    assert p.grid == "FM18", \
        f"grid must be extracted as FM18, not part of body, got {p.grid}"


def test_real_modern_heartbeat_wo7i_dn10():
    p = parse(_frame("WO7I: @HB HEARTBEAT DN10"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.from_call == "WO7I"
    assert p.grid == "DN10"


def test_real_modern_heartbeat_kd2uwr_fn30():
    p = parse(_frame("KD2UWR: @HB HEARTBEAT FN30"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.grid == "FN30"


def test_real_modern_heartbeat_with_slash_callsign():
    """W3BFO/P sending a heartbeat with portable suffix."""
    p = parse(_frame("W3BFO/P: @HB HEARTBEAT FM19"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.from_call == "W3BFO/P"
    assert p.grid == "FM19"


def test_real_heartbeat_reply_classified_as_heartbeat():
    """'<from>: <to> HEARTBEAT SNR <db>' — heartbeat-reply form.

    Was UNKNOWN/DIRECTED in v1, must be HEARTBEAT now so we add the
    sender to the heard list with no grid (correct — they didn't send
    one in this frame)."""
    p = parse(_frame("KQ4QZX: K3CLR HEARTBEAT SNR -19"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.from_call == "KQ4QZX"
    assert p.to_call == "K3CLR"
    assert p.grid is None  # reply doesn't carry the sender's grid


def test_real_heartbeat_reply_to_us_is_for_us():
    """If someone sends a heartbeat-reply addressed to us, is_for_us=True."""
    p = parse(_frame("KQ4QZX: K1ABC HEARTBEAT SNR -10"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.is_for_us


def test_real_heartbeat_reply_positive_snr():
    """Real traffic: HEARTBEAT SNR +06 (positive sign, two digits)."""
    p = parse(_frame("KC1WDO: WO7I HEARTBEAT SNR +06"), our_callsign="K1ABC")
    assert p.kind is FrameKind.HEARTBEAT
    assert p.from_call == "KC1WDO"


def test_real_cq_double_form_with_grid():
    """JS8Call's default CQ: 'CQ CQ <grid>' (doubled)."""
    p = parse(_frame("K3CLR: CQ CQ FM18"), our_callsign="K1ABC")
    assert p.kind is FrameKind.CQ, f"expected CQ, got {p.kind}"
    assert p.from_call == "K3CLR"
    assert p.grid == "FM18"


def test_real_cq_single_form_still_works():
    """Legacy single 'CQ <grid>' still parses."""
    p = parse(_frame("K8XYZ: CQ EN82"), our_callsign="K1ABC")
    assert p.kind is FrameKind.CQ
    assert p.grid == "EN82"


def test_real_directed_ack_short_form():
    """Real traffic: ACK without SNR suffix (just 'ACK')."""
    p = parse(_frame("NZ1ON: HC5PH ACK"), our_callsign="K1ABC")
    assert p.kind is FrameKind.ACK
    assert p.from_call == "NZ1ON"
    assert p.to_call == "HC5PH"


def test_real_directed_query_snr_to_specific_call():
    """'AL0A: KA9GAP SNR?' — directed SNR query."""
    p = parse(_frame("AL0A: KA9GAP SNR?"), our_callsign="K1ABC")
    assert p.kind is FrameKind.DIRECTED_QUERY
    assert p.from_call == "AL0A"
    assert p.to_call == "KA9GAP"


def test_real_truncated_message_classified_unknown():
    """Real traffic shows malformed/truncated frames like 'KN4SOX/B:'.

    These are usually fragments from frame-count limits. Must classify
    as UNKNOWN, NOT crash, NOT pollute the heard list with from_call=None.
    """
    p = parse(_frame("KN4SOX/B:"), our_callsign="K1ABC")
    assert p.kind is FrameKind.UNKNOWN
    assert p.from_call is None


def test_real_truncated_heartbeat_reply_unknown():
    """Real traffic: 'K3CLR HEARTBEAT SNR +14' — missing the colon.
    Without the FROM: TO structure we can't classify it; must be UNKNOWN."""
    p = parse(_frame("K3CLR HEARTBEAT SNR +14"), our_callsign="K1ABC")
    assert p.kind is FrameKind.UNKNOWN
    assert p.from_call is None


# ── Inbox / mailbox body inspectors (Phase 1+2) ──────────────────


from minijs8.protocol.grammar import (
    is_query_msgs,
    parse_msg,
    parse_msg_to,
    parse_query_msg_id,
)


# parse_msg ────────────────────────────────────────────────────────


def test_parse_msg_basic():
    assert parse_msg("MSG hello mike") == "hello mike"


def test_parse_msg_lowercase():
    assert parse_msg("msg lowercase ok") == "lowercase ok"


def test_parse_msg_strips_outer_whitespace():
    assert parse_msg("  MSG  hello mike  ") == "hello mike"


def test_parse_msg_rejects_msg_to_form():
    """parse_msg must NOT match MSG TO: — that's a separate command."""
    assert parse_msg("MSG TO:W4MSI hello") is None


def test_parse_msg_rejects_msg_to_lowercase():
    assert parse_msg("msg to:W4MSI hello") is None


def test_parse_msg_no_text_returns_none():
    """'MSG' with no body should not match — body is required."""
    assert parse_msg("MSG") is None
    assert parse_msg("MSG ") is None


def test_parse_msg_other_verbs_return_none():
    assert parse_msg("SNR -3") is None
    assert parse_msg("ACK") is None
    assert parse_msg("HELLO") is None


def test_parse_msg_preserves_case_in_body():
    """Verb is case-insensitive but body text preserves caller's case."""
    assert parse_msg("MSG Hello Mike") == "Hello Mike"


# parse_msg_to ─────────────────────────────────────────────────────


def test_parse_msg_to_basic():
    assert parse_msg_to("MSG TO:W4MSI dinner at 7") == ("W4MSI", "dinner at 7")


def test_parse_msg_to_lowercase():
    assert parse_msg_to("msg to:w4msi hello") == ("W4MSI", "hello")


def test_parse_msg_to_with_whitespace_around_colon():
    """JS8Call traffic sometimes has whitespace around the colon."""
    assert parse_msg_to("MSG TO: W4MSI hello") == ("W4MSI", "hello")
    assert parse_msg_to("MSG TO :W4MSI hello") == ("W4MSI", "hello")


def test_parse_msg_to_uppercases_recipient():
    assert parse_msg_to("MSG TO:w4msi hello")[0] == "W4MSI"


def test_parse_msg_to_with_portable_recipient():
    """Portable suffix should be allowed in the recipient call."""
    assert parse_msg_to("MSG TO:W4MSI/P hello") == ("W4MSI/P", "hello")


def test_parse_msg_to_no_body_returns_none():
    assert parse_msg_to("MSG TO:W4MSI") is None


def test_parse_msg_to_plain_msg_returns_none():
    """parse_msg_to must NOT match 'MSG <text>' — that's a different command."""
    assert parse_msg_to("MSG hello") is None


def test_parse_msg_to_other_verbs_return_none():
    assert parse_msg_to("MSG hello") is None
    assert parse_msg_to("SNR -3") is None
    assert parse_msg_to("ACK") is None


# is_query_msgs ────────────────────────────────────────────────────


def test_is_query_msgs_basic():
    assert is_query_msgs("QUERY MSGS") is True


def test_is_query_msgs_lowercase():
    assert is_query_msgs("query msgs") is True


def test_is_query_msgs_with_whitespace():
    assert is_query_msgs("  QUERY MSGS  ") is True


def test_is_query_msgs_rejects_query_msg_singular():
    """QUERY MSG <id> is a different command."""
    assert is_query_msgs("QUERY MSG 42") is False


def test_is_query_msgs_rejects_extra_args():
    """Strict whole-body match — no trailing payload."""
    assert is_query_msgs("QUERY MSGS PLEASE") is False


def test_is_query_msgs_other_verbs_false():
    assert is_query_msgs("SNR -3") is False
    assert is_query_msgs("MSG hello") is False
    assert is_query_msgs("") is False


# parse_query_msg_id ───────────────────────────────────────────────


def test_parse_query_msg_id_basic():
    assert parse_query_msg_id("QUERY MSG 42") == 42


def test_parse_query_msg_id_lowercase():
    assert parse_query_msg_id("query msg 42") == 42


def test_parse_query_msg_id_with_whitespace():
    assert parse_query_msg_id("  QUERY MSG 42  ") == 42


def test_parse_query_msg_id_one():
    """Smallest valid id — SQLite AUTOINCREMENT starts at 1."""
    assert parse_query_msg_id("QUERY MSG 1") == 1


def test_parse_query_msg_id_zero_invalid():
    """0 is not a valid SQLite AUTOINCREMENT id."""
    assert parse_query_msg_id("QUERY MSG 0") is None


def test_parse_query_msg_id_negative_invalid():
    assert parse_query_msg_id("QUERY MSG -1") is None


def test_parse_query_msg_id_non_numeric_returns_none():
    assert parse_query_msg_id("QUERY MSG abc") is None


def test_parse_query_msg_id_query_msgs_returns_none():
    """QUERY MSGS (with S) should not be confused with QUERY MSG <id>."""
    assert parse_query_msg_id("QUERY MSGS") is None


def test_parse_query_msg_id_msg_alone_returns_none():
    assert parse_query_msg_id("MSG 42") is None


# Cross-checks against _classify_directed_body ─────────────────────


def test_msg_to_classified_as_directed_command():
    """MSG TO: must classify as COMMAND, not DIRECTED_MESSAGE,
    so the inbox-dispatch branch in app.py picks it up."""
    p = parse(_frame("KC1WDO: W5DMH MSG TO:W4MSI hello"), our_callsign="W5DMH")
    assert p.kind is FrameKind.DIRECTED_COMMAND


def test_query_msgs_classified_as_directed_command():
    p = parse(_frame("KC1WDO: W5DMH QUERY MSGS"), our_callsign="W5DMH")
    assert p.kind is FrameKind.DIRECTED_COMMAND


def test_query_msg_id_classified_as_directed_command():
    p = parse(_frame("KC1WDO: W5DMH QUERY MSG 42"), our_callsign="W5DMH")
    assert p.kind is FrameKind.DIRECTED_COMMAND


def test_plain_msg_still_classified_as_directed_message():
    """MSG <text> should remain DIRECTED_MESSAGE so it's classified
    consistently with a free-text directed message — both are
    inbox-bound."""
    p = parse(_frame("KC1WDO: W5DMH MSG hello there"), our_callsign="W5DMH")
    assert p.kind is FrameKind.DIRECTED_MESSAGE
