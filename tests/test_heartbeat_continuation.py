"""Regression tests for the May 14 2026 on-air capture from W5DMH:

  1. ``_classify_directed_body`` crashed on whitespace-only body
     (IndexError at grammar.py:260). Caused parser failures that
     orphaned multi-frame directed-message continuations, breaking
     long free-text wrap testing.

  2. HEARTBEAT replies that include JS8Call's piggy-backed
     ``MSG ID <n>`` extension (e.g. ``KD8PGB: W5DMH HEARTBEAT SNR
     +04 MSG ID 61``) span two frames. Before the fix, the
     HEARTBEAT first-frame dispatched immediately as single-frame
     and the ``MSG ID 61`` continuation arrived orphaned (no buffer
     at the bucket) and was dropped. The fix adds DIRECTED
     HEARTBEAT to the non-buffered starter set so continuations
     attach.

  3. JS8 sometimes doesn't emit an inter-field space at frame
     boundaries when the field break aligns with the frame break.
     Wire ``...+04 MSG...`` arrives as ``+04`` (end of frame 1)
     and ``MSG`` (start of frame 2) — naïve concat yields
     ``+04MSG``. A narrow digit→uppercase-letter heuristic inserts
     a single space at the join, but ONLY for non-buffered
     commands (buffered commands have a CRC suffix that must be
     concatenated exactly, no space insertion).
"""

from __future__ import annotations

import pytest

from microjs8.protocol.grammar import _classify_directed_body, parse
from microjs8.protocol.reassembly import MessageAssembler
from microjs8.protocol.types import DecodedFrame, FrameKind, ParsedFrame


def _df(text: str = "", freq: float = 1500.0) -> DecodedFrame:
    return DecodedFrame(
        text=text, raw="", snr_db=0, frequency_hz=freq, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=0,
    )


def _pf(body: str, kind: FrameKind, *,
        from_call=None, to_call=None, freq: float = 1500.0) -> ParsedFrame:
    return ParsedFrame(
        decoded=_df("", freq),
        kind=kind, from_call=from_call, to_call=to_call,
        grid=None, body=body,
        is_for_us=(to_call == "W5DMH"),
    )


# ── Fix 1/2: whitespace body must not crash classifier ──────────────


@pytest.mark.parametrize("body", ["", " ", "  ", "\t", "\n", " \t\n "])
def test_classify_directed_body_whitespace_safe(body):
    """Whitespace-only or empty bodies classify as DIRECTED_MESSAGE
    without raising. Previously raised IndexError on the
    ``upper.split(None, 1)[0]`` path."""
    kind = _classify_directed_body(body)
    assert kind is FrameKind.DIRECTED_MESSAGE


def test_parse_frame_with_whitespace_body_does_not_crash():
    """End-to-end parse: a decoded frame containing only the
    envelope and whitespace body must round-trip cleanly through
    the parser without exception."""
    df = DecodedFrame(
        text="KD8PGB: W5DMH  ",
        raw="", snr_db=10, frequency_hz=1500.0, dt_seconds=0.0,
        submode=0, quality=0, frame_type=0,
        utc_seconds_of_day=0, received_at=0,
    )
    parsed = parse(df, our_callsign="W5DMH")
    assert parsed.from_call == "KD8PGB"
    assert parsed.to_call == "W5DMH"
    assert parsed.kind is FrameKind.DIRECTED_MESSAGE


# ── Fix 3: HEARTBEAT directed-to-us starts a buffer for continuations ─


def test_directed_heartbeat_starts_non_buffered_buffer():
    """HEARTBEAT replies directed to a real callsign (not @HB) start
    a non-buffered buffer so any continuation frames (e.g., MSG ID)
    can attach. Per W5DMH bench, May 2026."""
    asm = MessageAssembler()
    pf = _pf("HEARTBEAT SNR +04", FrameKind.HEARTBEAT,
             from_call="KD8PGB", to_call="W5DMH", freq=700.0)
    asm.feed(pf)
    assert asm.buffer_count == 1
    # Specifically the bucket is keyed on the audio offset.
    keys = list(asm._buffers.keys())
    assert keys[0] == ("KD8PGB", "W5DMH", 700.0)


def test_broadcast_heartbeat_to_at_hb_does_not_buffer():
    """Routine @HB broadcasts are single-frame sightings; they never
    carry follow-up content, so we don't waste memory on buffering
    them. Regression: confirms broadcast filter still works after
    the directed-HEARTBEAT acceptance."""
    asm = MessageAssembler()
    pf = _pf("HEARTBEAT EN82", FrameKind.HEARTBEAT,
             from_call="KD8PGB", to_call="@HB", freq=700.0)
    asm.feed(pf)
    assert asm.buffer_count == 0


def test_broadcast_heartbeat_to_at_allcall_does_not_buffer():
    asm = MessageAssembler()
    pf = _pf("HEARTBEAT EN82", FrameKind.HEARTBEAT,
             from_call="KD8PGB", to_call="@ALLCALL", freq=700.0)
    asm.feed(pf)
    assert asm.buffer_count == 0


def test_heartbeat_plus_msg_id_continuation_assembles():
    """The full on-air capture: HEARTBEAT SNR +04 in frame 1 +
    MSG ID 61 continuation in frame 2 reassembles into the
    structured-field body with the space restored at the
    digit→uppercase-letter boundary."""
    asm = MessageAssembler()
    asm.feed(_pf("HEARTBEAT SNR +04", FrameKind.HEARTBEAT,
                 from_call="KD8PGB", to_call="W5DMH", freq=700.0))
    asm.feed(_pf("MSG ID 61", FrameKind.UNKNOWN,
                 from_call=None, to_call=None, freq=700.0))
    # Inspect buffer body directly (the timeout-emit path is exercised
    # via _evict_stale_non_buffered, which we test separately).
    buf = next(iter(asm._buffers.values()))
    assert buf.body == "HEARTBEAT SNR +04 MSG ID 61"
    assert buf.frame_count == 2


# ── Fix 3 heuristic: digit→uppercase-letter space insertion ─────────


def test_continuation_heuristic_inserts_space_at_field_boundary():
    """Non-buffered continuation at a digit→uppercase-letter boundary
    gains a space. The most common case: HEARTBEAT-style structured
    fields where JS8 doesn't emit the inter-field space."""
    asm = MessageAssembler()
    asm.feed(_pf("GRID FN42", FrameKind.DIRECTED_MESSAGE,
                 from_call="K1ABC", to_call="W5DMH"))
    asm.feed(_pf("INFO weather is great", FrameKind.UNKNOWN))
    buf = next(iter(asm._buffers.values()))
    assert buf.body == "GRID FN42 INFO weather is great"


def test_continuation_no_space_insertion_at_letter_letter_boundary():
    """Mid-word splits (letter→letter) must NOT gain a space.
    JS8 packs whole huffman codes per frame; long words can span
    a frame boundary mid-character. Inserting a space here would
    corrupt the body."""
    asm = MessageAssembler()
    asm.feed(_pf("TEST MESSAGE TO SEE IF ", FrameKind.DIRECTED_MESSAGE,
                 from_call="K1ABC", to_call="W5DMH"))
    asm.feed(_pf("IT WRAPS CORRECTLY ON T", FrameKind.UNKNOWN))
    asm.feed(_pf("HE DIRECTED DISPLAY", FrameKind.UNKNOWN))
    buf = next(iter(asm._buffers.values()))
    # The "T HE" → "THE" join must be preserved as "THE", not "T HE".
    assert "T HE" not in buf.body
    assert "THE DIRECTED DISPLAY" in buf.body


def test_continuation_no_space_when_prior_already_trailing_space():
    """If the prior frame already ended with whitespace, no extra
    space is inserted — JS8 chose to put the boundary space at the
    end of frame N and the continuation has no leading space."""
    asm = MessageAssembler()
    asm.feed(_pf("YES ", FrameKind.DIRECTED_MESSAGE,
                 from_call="KD8PGB", to_call="W5DMH"))
    asm.feed(_pf("MSG ID 61", FrameKind.UNKNOWN))
    buf = next(iter(asm._buffers.values()))
    # Exactly one space — no double-spacing.
    assert buf.body == "YES MSG ID 61"


def test_continuation_heuristic_does_not_apply_to_buffered_commands():
    """Buffered commands carry an exact-byte CRC suffix that must
    not be modified by the assembler. Even when the CRC starts with
    an uppercase letter after a digit at the frame boundary, the
    body must be concatenated as-is. Without this restriction, a
    CRC like 'J6X' could land at the digit→letter boundary and
    silently fail validation."""
    from microjs8.protocol.checksum import checksum16
    # Construct a body where the natural CRC suffix happens to have
    # a digit→letter pattern at the frame boundary. The actual CRC
    # depends on the body, but we exercise the path by setting up
    # a multi-frame buffered MSG.
    asm = MessageAssembler()
    # Frame 1 is the buffered starter (verb=MSG).
    asm.feed(_pf("MSG", FrameKind.DIRECTED_COMMAND,
                 from_call="KD8PGB", to_call="W5DMH"))
    # Frame 2 is a continuation ending in a digit.
    asm.feed(_pf("HELLO 1", FrameKind.UNKNOWN))
    # Frame 3 is a continuation starting with an uppercase letter
    # (CRC chars often look like this).
    asm.feed(_pf("ABC", FrameKind.UNKNOWN))
    # The buffered body must concat exactly, with no space inserted
    # between "1" and "A".
    buf = next(iter(asm._buffers.values()))
    assert "1 A" not in buf.body
    assert buf.body == "HELLO 1ABC"
