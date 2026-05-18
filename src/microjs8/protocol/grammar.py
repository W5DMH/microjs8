"""JS8 directed-message grammar parser.

The GFSK8 wrapper hands us a Varicode-unpacked text string per decoded
frame. This module classifies that text into a ``ParsedFrame`` so the
rest of the daemon can react appropriately (heartbeat → just heard list,
directed-to-us → notify, etc.).

JS8 message conventions (from the JS8Call source AND on-air observation
of real traffic captured Apr 2026 on 7.078 MHz):

  Heartbeat (legacy):   "<call> @HB <grid>"
                        Older JS8Call versions; no colon, no "HEARTBEAT".

  Heartbeat (modern):   "<call>: @HB HEARTBEAT <grid>"
                        Current JS8Call format. Includes "@HB" as the
                        to-field plus the literal word HEARTBEAT, then
                        the sender's grid.

  Heartbeat reply:      "<call>: <other> HEARTBEAT SNR <db>"
                        e.g.  "K8IMT: K3CLR HEARTBEAT SNR -10"
                        Sent in response to seeing someone's HB. Does
                        NOT carry the sender's grid — just the SNR
                        report. Still classified as HEARTBEAT for
                        Heard-list purposes (the sending station is
                        active right now), but grid stays None.

  CQ:                   "<call>: CQ <grid>"
                        "<call>: CQ CQ <grid>"  (modern, doubled)
                        "<call>: @ALLCALL CQ <grid>"
                        "<call>: @ALLCALL CQ CQ <grid>"

  All-call broadcast:   "<call>: @ALLCALL <text>"

  Directed message:     "<from>: <to> <text>"
                        e.g.  "K8XYZ: K1ABC HELLO HOW ARE YOU"

  Directed query:       "<from>: <to> <CMD>?"

  Directed cmd:         "<from>: <to> <CMD> <ARG>"

  ACK reply:            "<from>: <to> ACK -<snr>"

We don't need every documented sub-command in Step 5 — most just need
to be classified correctly so the screen knows where to put them. Step 6
adds per-command handling for outgoing replies.
"""

from __future__ import annotations

import re
from typing import Optional

from microjs8.protocol.types import DecodedFrame, FrameKind, ParsedFrame

# Reusable regex pieces — DRY for callsigns and grids.
# Callsign is loose: 3-7 alphanumerics with optional /SUFFIX (portable, mobile,
# beacon designators like /B). Some real traffic uses lowercase; we accept
# both even though JS8 normalizes to uppercase, since the parser shouldn't
# refuse a frame on case alone.
_CALL = r"[A-Za-z0-9]{3,7}(?:/[A-Za-z0-9]+)?"
_GRID = r"[A-Ra-r]{2}[0-9]{2}(?:[a-xA-X]{2})?"

# Modern heartbeat: "<call>: @HB HEARTBEAT <grid>"
# This is the format current JS8Call versions use. Confirmed on-air.
_HB_MODERN_RE = re.compile(
    rf"^\s*(?P<call>{_CALL})\s*:\s*@HB\s+HEARTBEAT\s+(?P<grid>{_GRID})\s*$"
)

# Legacy heartbeat: "<call> @HB <grid>" (no colon, no HEARTBEAT word).
# Older JS8Call versions or alternate clients. Kept for compatibility.
_HB_LEGACY_RE = re.compile(
    rf"^\s*(?P<call>{_CALL})\s+@HB\s+(?P<grid>{_GRID})\s*$"
)

# Heartbeat reply: a directed message whose body is "HEARTBEAT SNR <db>".
# Sender is responding to someone else's HB — they're active, we want
# them in the heard list, but they don't transmit their own grid here.
_HB_REPLY_RE = re.compile(
    rf"^\s*(?P<frm>{_CALL})\s*:\s*(?P<to>@\w+|{_CALL})\s+HEARTBEAT\s+SNR\s+[+-]?\d+\s*$"
)

# CQ: "<call>: CQ <grid>" or "<call>: CQ CQ <grid>" (modern doubled form).
# Optional @ALLCALL prefix on the to-field. The "CQ CQ" doubled form is
# what JS8Call emits by default — was the missing case in the v1 parser.
_CQ_RE = re.compile(
    rf"^\s*(?P<call>{_CALL})\s*:\s*"
    r"(?:@ALLCALL\s+)?"
    r"CQ(?:\s+CQ)?"
    rf"(?:\s+(?P<grid>{_GRID}))?"
    r"\s*(?P<extra>.*?)\s*$"
)

# Generic directed: "<from>: <to> <body>"
# Catches everything that isn't a heartbeat or CQ. The classifier in
# _classify_directed_body decides FrameKind from the body text.
_DIRECTED_RE = re.compile(
    rf"^\s*(?P<frm>{_CALL})\s*:\s*(?P<to>@\w+|{_CALL})\s+(?P<body>.+)$"
)
# NOTE: body is ``.+$`` (greedy to end of string) NOT ``.+?\s*$``.
# JS8Call packs whole huffman codes per frame (Varicode.cpp::
# packHuffMessage L1935) so frame boundaries fall on character
# boundaries — including, sometimes, on a literal space. Concatenating
# decoded frame bodies must yield the EXACT original message including
# inter-frame spaces. The non-greedy + ``\s*$`` form would consume
# trailing whitespace and break multi-frame buffered MSG reassembly
# (observed on-air with "TEST STORAGE MESSAGE STORED " split across
# frames — the trailing space on frame N got eaten, frame N+1's "ON"
# concatenated as "STOREDON", CRC mismatch, message lost).

# Known directed commands, classified into QUERY (ends with ?) vs COMMAND.
_QUERY_COMMANDS = frozenset({
    "?", "AGN?", "INFO?", "SNR?", "GRID?", "QTH?", "STATUS?",
    "QUERY", "QUERY?", "MSG?", "MSGS?",
})
_KNOWN_COMMANDS = frozenset({
    "ACK", "AGN", "INFO", "SNR", "GRID", "QTH", "STATUS",
    "MSG", "MSG TO:", "STORE", "STORE TO:", "QUERY MSGS", "QUERY CALL",
    "HEARING", "RR", "HW CPY?", "QSL?",
})


def parse(
    decoded: DecodedFrame,
    our_callsign: Optional[str],
    our_groups: Optional[tuple[str, ...]] = None,
) -> ParsedFrame:
    """Classify a decoded frame.

    ``our_callsign`` is None when station identity isn't configured
    (so ``is_for_us`` is always False). When configured, we match
    case-insensitively because the protocol normalizes everything
    to uppercase but operator config might be lowercase.

    ``our_groups`` is the operator-configured set of JS8Call group
    callsigns (e.g. ``("@EMCOMM", "@ARESGA")``). A directed frame
    addressed to any of these groups is treated as ``is_for_us=True``
    — group members process group-directed traffic the same way they
    process personally-directed traffic, per JS8Call Guide v2.2 p.10.
    Passing None defaults to the empty set (no group memberships;
    only personally-directed frames are for us).
    """
    # Build the set of addresses we answer to. Always uppercase to
    # match the wire convention. The empty case (unconfigured) ends
    # up with an empty set and ``is_for_us`` is False throughout.
    if our_callsign:
        our_addresses = {our_callsign.upper()}
    else:
        our_addresses = set()
    if our_groups:
        for g in our_groups:
            if g:
                our_addresses.add(g.upper())
    # Preserve trailing whitespace from gfsk8's huffDecode output —
    # multi-frame buffered MSG reassembly depends on inter-frame
    # spaces being preserved at frame boundaries. Only strip leading
    # whitespace (which gfsk8 doesn't typically produce, but defensive).
    text = (decoded.text or "").lstrip()

    # 1) Modern heartbeat broadcast — most common form on current air.
    m = _HB_MODERN_RE.match(text)
    if m:
        return ParsedFrame(
            decoded=decoded,
            kind=FrameKind.HEARTBEAT,
            from_call=m.group("call"),
            to_call="@HB",
            grid=m.group("grid"),
            body="",
            is_for_us=False,
        )

    # 2) Legacy heartbeat broadcast (older JS8Call / alternate clients).
    m = _HB_LEGACY_RE.match(text)
    if m:
        return ParsedFrame(
            decoded=decoded,
            kind=FrameKind.HEARTBEAT,
            from_call=m.group("call"),
            to_call="@HB",
            grid=m.group("grid"),
            body="",
            is_for_us=False,
        )

    # 3) Heartbeat reply — "<from>: <to> HEARTBEAT SNR <db>".
    # Classify as HEARTBEAT (the sending station is active) but no grid.
    m = _HB_REPLY_RE.match(text)
    if m:
        from_call = m.group("frm")
        to = m.group("to")
        # Strip the from/to prefix to leave just "HEARTBEAT SNR -10".
        body_start = m.end("to")
        body = text[body_start:].strip()
        # "for us" if the heartbeat reply was acknowledging our HB,
        # OR was directed at a group we belong to. Heartbeat replies
        # are normally personal (one station replying to another),
        # but JS8Call permits `@GROUP HEARTBEAT SNR ...` too.
        is_for_us = to.upper() in our_addresses
        return ParsedFrame(
            decoded=decoded,
            kind=FrameKind.HEARTBEAT,
            from_call=from_call,
            to_call=to,
            grid=None,
            body=body,
            is_for_us=is_for_us,
        )

    # 4) CQ — colon, then "CQ" or "CQ CQ" or "@ALLCALL CQ".
    m = _CQ_RE.match(text)
    if m and ("CQ" in text):
        return ParsedFrame(
            decoded=decoded,
            kind=FrameKind.CQ,
            from_call=m.group("call"),
            to_call="@ALLCALL",
            grid=m.group("grid"),
            body=(m.group("extra") or "").strip(),
            is_for_us=False,
        )

    # 5) Generic directed: "<from>: <to> <body>"
    m = _DIRECTED_RE.match(text)
    if m:
        from_call = m.group("frm")
        to = m.group("to")
        # Preserve trailing whitespace: see _DIRECTED_RE comment for
        # rationale. The kind classifier and is_for_us check both
        # tolerate trailing whitespace.
        body = m.group("body")

        # @ALLCALL prefix on the to-field is a broadcast, not a directed message.
        if to == "@ALLCALL":
            return ParsedFrame(
                decoded=decoded,
                kind=FrameKind.ALLCALL,
                from_call=from_call,
                to_call=to,
                grid=None,
                body=body,
                is_for_us=False,
            )

        is_for_us = to.upper() in our_addresses

        # Classify the body's first token as a known command/query
        # to set the FrameKind accurately.
        kind = _classify_directed_body(body)

        return ParsedFrame(
            decoded=decoded,
            kind=kind,
            from_call=from_call,
            to_call=to,
            grid=None,
            body=body,
            is_for_us=is_for_us,
        )

    # 6) Couldn't parse — keep the raw text but mark unknown.
    return ParsedFrame(
        decoded=decoded,
        kind=FrameKind.UNKNOWN,
        from_call=None,
        to_call=None,
        grid=None,
        body=text,
        is_for_us=False,
    )


def _classify_directed_body(body: str) -> FrameKind:
    """Pick FrameKind for a directed-message body.

    Order matters: ACK is its own kind, queries end in '?', everything
    else with a recognized first token is a COMMAND, otherwise a plain
    DIRECTED_MESSAGE.

    Defensive against whitespace-only bodies — Python's
    ``str.split(None)`` returns ``[]`` (not ``[""]``) when called on
    a string of only whitespace characters, so the prior
    ``upper.split(None, 1)[0] if upper else ""`` form raised
    IndexError when ``body`` was e.g. ``"   "`` (truthy, but yields
    no tokens). Observed in the wild when JS8 frames carried only
    a continuation/padding character that decoded to whitespace.
    Use ``upper.strip()`` for the truthy test so empty-after-strip
    bodies short-circuit to ``first = ""`` and we fall through to
    DIRECTED_MESSAGE — the catch-all classification.
    """
    upper = body.upper()
    first = upper.split(None, 1)[0] if upper.strip() else ""

    if first.startswith("ACK"):
        return FrameKind.ACK
    # MSG TO: is a COMMAND, not a free-text DIRECTED_MESSAGE — same
    # for QUERY MSGS / QUERY MSG <id>. They drive the inbox state
    # machine in app.py, so we want them classified as COMMAND so
    # they don't appear in the inbox UI as "received messages."
    if upper.startswith("MSG TO:"):
        return FrameKind.DIRECTED_COMMAND
    if upper.startswith("QUERY MSGS") or upper.startswith("QUERY MSG "):
        return FrameKind.DIRECTED_COMMAND
    if first in _QUERY_COMMANDS or upper.endswith("?"):
        return FrameKind.DIRECTED_QUERY
    # Plain "MSG <body>" (no TO:) is a "store in YOUR inbox for you
    # to read" request — it's a directed message addressed to us
    # asking us to file it. Per JS8Call protocol it auto-ACKs and
    # populates the recipient's inbox. Classify as DIRECTED_MESSAGE
    # so the existing inbox-routing path in app.py picks it up.
    if first == "MSG":
        return FrameKind.DIRECTED_MESSAGE
    if first in _KNOWN_COMMANDS:
        return FrameKind.DIRECTED_COMMAND
    return FrameKind.DIRECTED_MESSAGE


# ── Inbox / mailbox body inspectors ─────────────────────────────────
#
# These helpers operate on the ``body`` of a ParsedFrame — i.e., the
# text after the ``<from>: <to>`` prefix — and tell app.py what kind
# of inbox-relevant action a directed frame represents. We keep them
# at this level (not in MailboxStore) so the inbox state machine
# stays in app.py and the storage layer stays a dumb persistence
# layer.
#
# Each inspector returns:
#   - None if the body doesn't match the pattern
#   - For MSG / MSG TO:, a tuple of (recipient_or_none, message_text)
#   - For QUERY MSGS, True / False
#   - For QUERY MSG <id>, the integer id
#
# Body is matched case-insensitively for the verb but text is
# preserved as-is for storage.


# Match "MSG TO:<callsign> <text>". The colon may have surrounding
# whitespace per real-world traffic. Recipient must look like a
# callsign (3-7 alphanumerics with optional /SUFFIX). Body is at
# least one character.
_MSG_TO_RE = re.compile(
    rf"^\s*MSG\s+TO\s*:\s*(?P<dest>{_CALL})\s+(?P<text>.+?)\s*$",
    re.IGNORECASE,
)

# Match "MSG <text>" — the simple-mailbox-store form. The body must
# NOT match MSG TO: (we check that separately first). This regex is
# greedy-matched on the body so multi-word text passes through.
_MSG_RE = re.compile(
    r"^\s*MSG\s+(?P<text>.+?)\s*$",
    re.IGNORECASE,
)

# Match exactly "QUERY MSGS" — no payload, the whole body. Whitespace
# tolerated at start/end.
_QUERY_MSGS_RE = re.compile(r"^\s*QUERY\s+MSGS\s*$", re.IGNORECASE)

# Match "QUERY MSG <id>" where id is a positive integer. Bound to
# one or more digits so we don't accept "QUERY MSG abc" as valid.
_QUERY_MSG_ID_RE = re.compile(
    r"^\s*QUERY\s+MSG\s+(?P<id>\d+)\s*$",
    re.IGNORECASE,
)


def parse_msg_to(body: str) -> Optional[tuple[str, str]]:
    """Parse "MSG TO:<recipient> <text>" — returns (recipient, text) or None.

    Used by app.py when it sees a DIRECTED_COMMAND frame addressed to
    us; if this returns non-None the frame is a hold-for-recipient
    request and the daemon stores a STORE row.

    Recipient is uppercased before return because the rest of the
    inbox layer keys on uppercase callsigns (matches JS8Call
    convention).
    """
    m = _MSG_TO_RE.match(body)
    if not m:
        return None
    return (m.group("dest").upper(), m.group("text"))


def parse_msg(body: str) -> Optional[str]:
    """Parse "MSG <text>" — returns text body, or None if not MSG.

    Important: this rejects bodies that match MSG TO: first (those
    are a different command). Caller MUST try parse_msg_to() before
    parse_msg(), or the regex will incorrectly match
    "MSG TO:CALL ..." as a plain MSG with text "TO:CALL ...".
    """
    # Defensive: if the body looks like MSG TO:, refuse to match
    # even though _MSG_RE would technically accept it. This decouples
    # caller order-dependence from correctness.
    if _MSG_TO_RE.match(body):
        return None
    m = _MSG_RE.match(body)
    if not m:
        return None
    return m.group("text")


def is_query_msgs(body: str) -> bool:
    """Whole-body match for "QUERY MSGS" — the asker wants any
    pending mail we hold for them. Strict whole-body match (no extra
    arguments) so we don't false-trigger on QUERY MSG (without S)
    or QUERY MSGS WHATEVER.
    """
    return bool(_QUERY_MSGS_RE.match(body))


def parse_query_msg_id(body: str) -> Optional[int]:
    """Parse "QUERY MSG <id>" — returns the int id, or None.

    The integer is bounded by positive non-zero (>= 1) since SQLite
    AUTOINCREMENT starts at 1. Negative or zero is treated as a
    parse failure rather than a not-found id.
    """
    m = _QUERY_MSG_ID_RE.match(body)
    if not m:
        return None
    try:
        n = int(m.group("id"))
    except ValueError:
        return None
    return n if n >= 1 else None

