"""JS8 multi-frame message reassembly.

JS8 transmits messages longer than ~13 characters across multiple
back-to-back 15-second slots. The receive-side decoder hands us each
frame independently — gfsk8.Decoder produces one ``Decoded`` per
frame, with no automatic concatenation. This module reassembles
those frame sequences into complete messages so the inbox dispatch
layer can act on them.

Protocol shape
==============

The first frame of a multi-frame message carries the directed-
message envelope: ``"<from>: <to> <CMD>"`` where CMD is one of the
buffered commands (MSG, MSG TO:, QUERY MSGS, QUERY CALL, QUERY,
relay ``>``, CMD). The body text starts after the verb. If the body
fits in the first frame's remaining space, the message is single-
frame. Otherwise it spills into subsequent frames as pure text
continuations — no envelope, no callsign, just the next chunk of
the body.

Continuation frames decode to ``FrameKind.UNKNOWN`` in our parser
because they don't have the directed-message envelope. We identify
them by audio offset (within tolerance) and proximity in time to a
recently-started buffer.

End-of-message triggers
=======================

We declare a buffered message complete when ANY of these fires:

1. **Checksum validates** (preferred): the assembled body ends with
   a valid 3-char CRC-16/KERMIT checksum. This is the protocol-
   correct signal — JS8Call uses exactly this to decide when to
   ACK. Cheap to check on every continuation frame because the CRC
   table is precomputed and bodies are short.

2. **Timeout** (~30 s): no continuation frame at the buffer's audio
   offset for several slot-widths. The assembler emits whatever it
   has — but flagged as ``incomplete`` so the dispatcher knows not
   to ACK. Timeout is twice the slot width plus margin so a single
   missed decode in the middle of a 4-frame TX doesn't kill the
   buffer.

3. **End-of-transmission char** (``\\x04``): JS8Call appends EOT to
   the LAST frame after the checksum. We treat its presence as a
   completion signal even if the trailing text didn't validate —
   useful when the operator's checksum is corrupt but they still
   want the message displayed (we'd surface as incomplete).

Buffer key
==========

Each buffer is keyed by ``(from_call, to_call, audio_offset_hz)``
where ``audio_offset_hz`` is rounded to the nearest 25 Hz to absorb
the tiny per-decode jitter we see in practice. Two stations TX'ing
on different audio offsets can interleave without buffer collision,
and two stations on the SAME offset (collision) get correctly
discriminated by from-call + to-call.

Concurrency
===========

This module is NOT thread-safe by design — the only consumer is
the asyncio decode handler in app.py, which is single-threaded. If
that ever changes we'd add a Lock around the buffer dict, but for
now keeping it lock-free saves us a layer of complexity.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from minijs8.protocol.checksum import verify_checksum16
from minijs8.protocol.types import FrameKind, ParsedFrame


_log = logging.getLogger(__name__)


# Buffered command verbs (cmd-id ∈ {5, 9, 10, 11, 12, 13, 24} per
# gfsk8/JS8Call). GRID (15) is buffered but not checksummed; we
# don't include it here because GRID is a status broadcast, not an
# inbox concern. Adding GRID later is a one-line change.
_BUFFERED_VERBS: frozenset[str] = frozenset({
    "MSG",         # cmd 9   — store in destination's inbox
    "MSG TO:",     # cmd 10  — store at intermediate for recipient
    "QUERY",       # cmd 11  — generic query (rare)
    "QUERY MSGS",  # cmd 12  — list messages held for asker
    "QUERY CALL",  # cmd 13  — can you reach <callsign>?
    "CMD",         # cmd 24  — generic command
    ">",           # cmd 5   — relay
})

# Round audio offset to this granularity when keying buffers. The
# decoder reports a float Hz (e.g. 1616.1), and consecutive frames
# from the same TX usually land within ±10 Hz. Bucketing at 25 Hz
# absorbs that jitter while still discriminating two stations that
# TX'd 50 Hz apart.
_OFFSET_BUCKET_HZ = 25.0

# Default timeout for buffered-command buffers. JS8 slot is 15 s; a
# 4-frame message takes ~60 s to TX. We use a per-frame timeout of
# 2×slot so a single missed continuation doesn't kill an in-flight
# buffer awaiting checksum validation.
DEFAULT_FRAME_TIMEOUT_S = 30.0

# Shorter timeout for non-buffered directed messages (YES/NO/INFO/
# STATUS/HEARING/etc. that don't carry a CRC). Without a checksum,
# completion is signaled by either a new starter at the same audio
# offset OR by timeout. ~20 s = one JS8 slot (15 s) + 5 s grace,
# matching the protocol's "if you didn't hear another frame in the
# next slot, assume single-frame" intuition.
NON_BUFFERED_TIMEOUT_S = 20.0

# End-of-transmission character JS8Call appends to the last frame on
# the wire. **gfsk8's huffDecode consumes EOT and replaces it with a
# trailing space in the decoded text** — confirmed by reading
# Varicode.cpp::huffDecode in the gfsk8 source. So in practice we
# almost never see the literal \\x04 in a parsed body; we keep the
# constant + check defensively in case a future decoder passes it
# through, but we DON'T rely on EOT as the primary completion signal.
_EOT_CHAR = "\x04"


@dataclass(frozen=True)
class AssembledMessage:
    """A complete (or timed-out) multi-frame message.

    Returned by ``MessageAssembler.feed`` whenever a buffer reaches
    a terminal state. The dispatcher uses ``checksum_valid`` to
    decide whether to auto-ACK — we never ACK an incomplete or
    checksum-failed message.

    ``was_buffered_command`` distinguishes between:

      True  — A buffered JS8 command (MSG, MSG TO:, QUERY MSGS,
              QUERY MSG <id>, QUERY CALL, CMD, relay '>'). Those
              have CRC-16/KERMIT checksums; ``checksum_valid``
              reflects whether the CRC matched. Dispatch goes
              through inbox handlers + protocol-level reply logic.

      False — A non-buffered directed message (YES, NO, INFO, GRID,
              STATUS, HEARING, free-text directed message, etc.).
              No checksum. ``checksum_valid`` is conventionally
              True on completion (timeout or preempt) — there's
              nothing to validate. Dispatch goes ONLY to the
              directed-activity log; no inbox or reply logic runs.
    """

    from_call: str
    to_call: str
    verb: str               # the directed-command verb (MSG, etc.) — first body token, uppercased
    body: str               # message body WITHOUT the checksum suffix (or full body if non-buffered)
    checksum_valid: bool    # True if checksum verified or non-buffered complete; False if buffered-timeout
    raw_text: str           # the assembled raw text (for logging/debug)
    offset_hz: float        # the buffer's audio-offset bucket (rounded)
    started_at: float       # when the first frame arrived
    completed_at: float     # when the buffer became terminal
    frame_count: int        # how many frames were assembled
    was_buffered_command: bool = True  # see class docstring


# Internal buffer state for one in-flight multi-frame message.
@dataclass
class _Buffer:
    from_call: str
    to_call: str
    verb: str
    bucket_hz: float        # rounded audio offset (the key, not the raw freq)
    raw_offset_hz: float    # last-seen raw freq (for debug/logging)
    body: str               # accumulated text after the verb
    started_at: float
    last_frame_at: float
    frame_count: int = 1
    # True for buffered JS8 commands (MSG, MSG TO:, QUERY*, CMD,
    # relay) — those have a CRC-16/KERMIT checksum and complete on
    # checksum-validates. False for any other directed message
    # (YES, NO, INFO, GRID, STATUS, HEARING, free text). Those have
    # no checksum and complete on timeout or preempt.
    is_buffered_command: bool = True


def _bucket_offset(freq_hz: float) -> float:
    """Snap a raw audio frequency to the buffer-key bucket.

    Rounding is to the nearest _OFFSET_BUCKET_HZ. Returns a float
    so the same arithmetic can be applied symmetrically when the
    caller looks up a key.
    """
    return round(freq_hz / _OFFSET_BUCKET_HZ) * _OFFSET_BUCKET_HZ


def _classify_first_frame(parsed: ParsedFrame) -> Optional[tuple[str, str]]:
    """If this is the first frame of a buffered command, return (verb, body_start).

    Returns None if the frame isn't a buffered-command starter.

    A "first frame" means a directed frame whose body begins with
    one of the buffered verbs. The body may or may not include
    text after the verb — both are valid (single-frame and multi-
    frame messages start the same way). We extract:

      verb       — exactly one of the BUFFERED_VERBS, in
                   canonical case (MSG, MSG TO:, etc.)
      body_start — the rest of the body after the verb, or "" if
                   the verb sits alone in this frame.

    The classifier is deliberately permissive: directed-COMMAND
    and directed-MESSAGE both qualify, since "MSG hello" classifies
    as DIRECTED_MESSAGE while "MSG TO:..." classifies as
    DIRECTED_COMMAND. Both need reassembly.
    """
    if parsed.kind not in (FrameKind.DIRECTED_MESSAGE, FrameKind.DIRECTED_COMMAND):
        return None
    # Preserve trailing whitespace: when the verb is followed by body
    # content that ends with a space (because the next frame's content
    # starts mid-word), we need that space to survive into ``rest`` so
    # multi-frame concatenation works. Only lstrip (the regex match
    # already consumed leading whitespace, so this is mostly defensive).
    body = (parsed.body or "").lstrip()
    if not body:
        return None

    upper_body = body.upper()

    # MSG TO: must be checked before MSG so the latter doesn't
    # eat the longer prefix.
    if upper_body.startswith("MSG TO:"):
        verb = "MSG TO:"
        rest = body[len("MSG TO:"):].lstrip()
        return (verb, rest)
    if upper_body.startswith("MSG"):
        # Distinguish "MSG" from "MSGS" — the trailing char must be
        # whitespace or end-of-string.
        if len(body) == 3 or body[3].isspace():
            verb = "MSG"
            rest = body[3:].lstrip()
            return (verb, rest)
    if upper_body.startswith("QUERY MSGS"):
        verb = "QUERY MSGS"
        rest = body[len("QUERY MSGS"):].lstrip()
        return (verb, rest)
    if upper_body.startswith("QUERY CALL"):
        verb = "QUERY CALL"
        rest = body[len("QUERY CALL"):].lstrip()
        return (verb, rest)
    if upper_body.startswith("QUERY") and (
        len(body) == 5 or body[5].isspace()
    ):
        verb = "QUERY"
        rest = body[5:].lstrip()
        return (verb, rest)
    if upper_body.startswith("CMD") and (
        len(body) == 3 or body[3].isspace()
    ):
        verb = "CMD"
        rest = body[3:].lstrip()
        return (verb, rest)
    if body.startswith(">"):
        verb = ">"
        rest = body[1:].lstrip()
        return (verb, rest)
    return None


def _checksum_required(verb: str) -> bool:
    """Does this verb's body need a CRC-16/KERMIT checksum?

    Per gfsk8/JS8Call: all buffered commands EXCEPT GRID (15) and
    @APRSIS-targeted messages are 16-bit-checksummed. Our
    _BUFFERED_VERBS set already excludes GRID; the @APRSIS bypass
    is handled at the dispatcher level (we just don't reassemble
    @APRSIS at all in this phase).
    """
    return verb in _BUFFERED_VERBS


class MessageAssembler:
    """Stateful reassembler for multi-frame JS8 buffered commands.

    Construct once per daemon. Feed every parsed decoded frame via
    ``feed()`` — the assembler decides whether the frame is a new-
    buffer starter, a continuation, or unrelated. Returns an
    ``AssembledMessage`` when a buffer reaches a terminal state.

    Single-frame messages (where the body fits with checksum in the
    very first frame) are dispatched immediately on the same
    ``feed()`` call — no waiting for a continuation that won't come.
    """

    def __init__(
        self,
        *,
        frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S,
        non_buffered_timeout_s: float = NON_BUFFERED_TIMEOUT_S,
        clock=time.time,
    ) -> None:
        # Buffer key is (from_call, to_call_upper, bucket_hz). We
        # uppercase the to-call so MSG to "@HB" vs "@hb" go to the
        # same bucket, and so a station that decodes its own callsign
        # in lowercase doesn't fragment its inbound stream.
        self._buffers: dict[tuple[str, str, float], _Buffer] = {}
        self._frame_timeout_s = frame_timeout_s
        self._non_buffered_timeout_s = non_buffered_timeout_s
        self._clock = clock

    # ── Public API ─────────────────────────────────────────────────

    def feed(self, parsed: ParsedFrame) -> list[AssembledMessage]:
        """Process one parsed frame; return any newly-completed messages.

        Returns a list — typically zero or one element, but possibly
        more when the new frame's processing causes BOTH a stale
        non-buffered buffer to time out AND the current frame to
        complete a different buffer. Callers must iterate the list
        and dispatch each completion in order.

        Decision tree:

        1. **Stale-buffer sweep first.** Any non-buffered buffer that
           hasn't seen a frame in ``non_buffered_timeout_s`` (default
           20s, ~one slot) is treated as complete and emitted with
           ``checksum_valid=True`` — the message is "done" by
           protocol convention. Buffered buffers that timeout are
           NOT auto-emitted here (they go through ``sweep_timeouts``
           with checksum_valid=False so the dispatcher can decide
           whether to surface or drop them).

        2. **Buffered-command starter.** Frame is a directed message
           whose first verb-token is in the buffered_cmds set. Emit
           any existing same-key buffer first (preempt), then handle
           via ``_on_buffered_starter``. May complete in one frame
           (single-frame body+checksum) or kick off multi-frame
           waiting.

        3. **Non-buffered directed starter.** Frame is DIRECTED_*
           with a real callsign envelope but the verb is NOT in the
           buffered set. Emit any same-key buffer first, then start
           a fresh non-buffered buffer. Won't complete until timeout
           or new starter at same offset.

        4. **Continuation.** Frame is UNKNOWN with a body. Append to
           the most-recently-started buffer at the same audio
           offset. If buffered → maybe-validate-checksum. If non-
           buffered → no completion check; wait for timeout.

        5. **Unrelated.** Heartbeat, CQ, ACK, broadcast, etc. — no
           buffer interaction.
        """
        now = self._clock()

        # 1) Sweep stale non-buffered buffers FIRST. Their timeout
        # being elapsed means "this buffer is complete" — emit them
        # before processing the current frame so the caller sees them
        # in arrival order.
        out: list[AssembledMessage] = self._evict_stale_non_buffered(now)

        # Also let buffered buffers expire silently (no emit) — the
        # dispatcher can call sweep_timeouts() if it wants the
        # checksum-failed reports for partial-receive surfacing.
        self._evict_stale_buffered(now)

        # 2) Buffered-command starter (existing semantics).
        starter = _classify_first_frame(parsed)
        if starter is not None and parsed.from_call:
            verb, rest = starter
            result = self._on_buffered_starter(parsed, verb, rest, now)
            if result is not None:
                out.append(result)
            return out

        # 3) Non-buffered directed starter (NEW). Recognize a frame
        # that has a directed-envelope (real from_call + to_call) and
        # a body, but whose verb isn't a buffered command. We buffer
        # these so multi-frame replies (e.g., "YES MSG ID 57") get
        # reassembled rather than dropping the continuation.
        if (
            parsed.kind in (
                FrameKind.DIRECTED_MESSAGE,
                FrameKind.DIRECTED_COMMAND,
                FrameKind.DIRECTED_QUERY,
            )
            and parsed.from_call
            and parsed.body
        ):
            non_buffered_emit, started_buf = self._on_non_buffered_starter(
                parsed, now,
            )
            if non_buffered_emit is not None:
                out.append(non_buffered_emit)
            return out

        # 4) Continuation.
        if parsed.kind is FrameKind.UNKNOWN and parsed.body:
            result = self._on_continuation(parsed, now)
            if result is not None:
                out.append(result)
            return out

        # 5) Unrelated frame.
        return out

    def sweep_completed(self) -> list[AssembledMessage]:
        """Drain any completion-eligible buffers without a new frame.

        Returns timed-out non-buffered buffers as ``checksum_valid=
        True`` (single-frame messages whose grace period expired).
        Buffered buffers that timed out are NOT included — they're
        emitted by ``sweep_timeouts`` with checksum_valid=False so
        the caller can distinguish "delivery succeeded" from "we
        gave up waiting".

        Useful for periodic ticks when no new frames are coming in.
        Calling this is optional: ``feed()`` already drains stale
        non-buffered buffers on every call, so callers that always
        have frames flowing don't need it.
        """
        now = self._clock()
        return self._evict_stale_non_buffered(now)

    def sweep_timeouts(self) -> list[AssembledMessage]:
        """Force-emit any timed-out buffers as incomplete messages.

        Returned list is in arrival order (oldest started_at first).
        Called by the dispatcher during periodic maintenance — for
        example every 5 seconds — to surface stalled receives so the
        operator at least sees the partial body, even though it
        won't be ACK'd.
        """
        now = self._clock()
        return self._evict_stale(now, return_evicted=True)

    def reset(self) -> None:
        """Drop all in-flight buffers. Used when our identity changes
        (operator updated callsign in Setup) so we don't carry over
        partial state that's keyed to the old call."""
        self._buffers.clear()

    @property
    def buffer_count(self) -> int:
        """Number of in-flight buffers — for diagnostic logging."""
        return len(self._buffers)

    # ── Internals ──────────────────────────────────────────────────

    def _on_buffered_starter(
        self,
        parsed: ParsedFrame,
        verb: str,
        rest: str,
        now: float,
    ) -> Optional[AssembledMessage]:
        """Handle a buffered-command starter frame.

        Buffered commands (MSG, MSG TO:, QUERY*, CMD, relay) carry
        a CRC-16/KERMIT checksum on the body. We start a buffer and
        wait for continuations until the checksum validates (or
        timeout / EOT triggers a fallback emit).

        If ``rest`` already contains a valid checksum, emit
        immediately as a single-frame complete message. Otherwise
        create a buffer and wait for continuations.
        """
        from_call = parsed.from_call or ""
        to_call = (parsed.to_call or "").upper()
        bucket = _bucket_offset(parsed.decoded.frequency_hz)
        key = (from_call, to_call, bucket)

        # Single-frame attempt: if rest already looks valid, no
        # point creating a buffer.
        if rest and _checksum_required(verb):
            stripped = _strip_eot(rest)
            validated = verify_checksum16(stripped)
            if validated is not None:
                # Single-frame complete message.
                self._buffers.pop(key, None)
                _log.debug(
                    "reassembly: single-frame %s from=%s to=%s body=%r",
                    verb, from_call, to_call, validated[:40],
                )
                return AssembledMessage(
                    from_call=from_call,
                    to_call=to_call,
                    verb=verb,
                    body=validated,
                    checksum_valid=True,
                    raw_text=stripped,
                    offset_hz=bucket,
                    started_at=now,
                    completed_at=now,
                    frame_count=1,
                )

        # Verb-only complete message (no body to checksum).
        #
        # JS8Call's send-side only computes a checksum if the body
        # ("line") is non-empty after lstrip:
        #
        #     if (isCommandBuffered(dirCmd) && !line.empty()) {
        #         line = line + " " + checksum16(line);
        #     }
        #
        # So a verb-only frame is technically protocol-valid for any
        # buffered verb. BUT: in practice, almost all buffered verbs
        # require a body to mean anything (MSG with no text, MSG TO:
        # with no recipient, QUERY with no argument — all malformed).
        # The one exception is **QUERY MSGS**: it's a complete
        # protocol command on its own ("do you have messages for me?").
        # That's the common, real, on-air case.
        #
        # If we emit-immediately for all verb-only frames, we'd
        # mis-handle the canonical multi-frame case where frame 1 is
        # the verb and frame 2+ carry the body — exactly the KD8PGB
        # bench-test scenario. So we restrict the immediate-emit
        # path to QUERY MSGS only. Other verb-only frames start a
        # buffer; if continuations don't arrive within 30 s the
        # buffer evicts silently (which is the right behavior for
        # truly malformed verb-only TXs).
        if not rest and verb == "QUERY MSGS":
            self._buffers.pop(key, None)
            _log.debug(
                "reassembly: verb-only single-frame %s from=%s to=%s",
                verb, from_call, to_call,
            )
            return AssembledMessage(
                from_call=from_call,
                to_call=to_call,
                verb=verb,
                body="",
                checksum_valid=True,   # no checksum required → trivially valid
                raw_text="",
                offset_hz=bucket,
                started_at=now,
                completed_at=now,
                frame_count=1,
            )

        # Otherwise start (or restart) the buffer for this key.
        # Restart is correct on collision: if we somehow get a fresh
        # MSG starter while an old one was in-flight at the same key,
        # the old one is abandoned (its checksum probably won't ever
        # validate now that our cursor moved on).
        prior = self._buffers.pop(key, None)
        if prior is not None:
            _log.info(
                "reassembly: discarding stale buffer for key=%s (overwritten by new starter)",
                key,
            )
        self._buffers[key] = _Buffer(
            from_call=from_call,
            to_call=to_call,
            verb=verb,
            bucket_hz=bucket,
            raw_offset_hz=parsed.decoded.frequency_hz,
            body=rest,
            started_at=now,
            last_frame_at=now,
            frame_count=1,
            is_buffered_command=True,
        )
        _log.debug(
            "reassembly: started buffered buffer key=%s verb=%s body_so_far=%r",
            key, verb, rest[:40],
        )
        return None

    def _on_non_buffered_starter(
        self,
        parsed: ParsedFrame,
        now: float,
    ) -> tuple[Optional[AssembledMessage], bool]:
        """Handle a directed frame whose verb is NOT a buffered command.

        Examples: KD8PGB sends "W5DMH YES" (frame 1 of "YES MSG ID 57"),
        or "W5DMH SNR -10" (single-frame), or "W5DMH STATUS All quiet"
        (potentially multi-frame).

        Behavior:

        1. If a buffer already exists at the same (from, to, offset)
           key, **emit it first as preempted** (operator's seeing a
           new TX from the same station at the same offset, the old
           buffer is conceptually finished). Then start fresh.

        2. The new buffer captures the FULL body of frame 1
           (verb-token included), because non-buffered messages
           don't have a separate "verb prefix" + "rest" structure
           the way buffered commands do — the body is just text,
           and the operator wants to see all of it.

        3. We do NOT emit the new buffer immediately. Even though
           it might be a single-frame TX, JS8Call protocol allows
           continuations in the next slot. We wait for either
           timeout (one slot + grace) or a new same-offset starter.

        Returns
        -------
        (preempted_emit, started_new_buffer)
            ``preempted_emit`` is None unless we displaced an
            existing buffer (in which case it's that buffer's
            AssembledMessage with checksum_valid=True).
            ``started_new_buffer`` is always True after this method
            (we always create a new buffer for the incoming frame).
        """
        from_call = parsed.from_call or ""
        to_call = (parsed.to_call or "").upper()
        bucket = _bucket_offset(parsed.decoded.frequency_hz)
        key = (from_call, to_call, bucket)

        # Take the verb (first whitespace-delimited token of the
        # body) but KEEP THE WHOLE BODY in the buffer. We surface
        # the verb separately so the renderer can color it; the
        # body field carries the full text including the verb so
        # the operator sees the literal frame contents reassembled.
        body = (parsed.body or "").rstrip()
        # Strip any incidental EOT characters (defensive — gfsk8
        # normally consumes them but be safe).
        body = _strip_eot(body)
        first_split = body.split(None, 1)
        verb = first_split[0].upper() if first_split else ""

        # If there's an existing buffer at this key, preempt it —
        # the operator's hearing a new TX at the same offset, so
        # the old buffer's content is "what was said before".
        # Emit the preempted buffer and start a new one for the
        # current frame.
        preempted_emit: Optional[AssembledMessage] = None
        prior = self._buffers.pop(key, None)
        if prior is not None:
            _log.info(
                "reassembly: preempting non-buffered buffer key=%s (new starter at same offset)",
                key,
            )
            preempted_emit = AssembledMessage(
                from_call=prior.from_call,
                to_call=prior.to_call,
                verb=prior.verb,
                body=prior.body,
                checksum_valid=True,  # non-buffered: trivially "complete"
                raw_text=prior.body,
                offset_hz=prior.bucket_hz,
                started_at=prior.started_at,
                completed_at=now,
                frame_count=prior.frame_count,
                was_buffered_command=prior.is_buffered_command,
            )

        self._buffers[key] = _Buffer(
            from_call=from_call,
            to_call=to_call,
            verb=verb,
            bucket_hz=bucket,
            raw_offset_hz=parsed.decoded.frequency_hz,
            body=body,
            started_at=now,
            last_frame_at=now,
            frame_count=1,
            is_buffered_command=False,
        )
        _log.debug(
            "reassembly: started non-buffered buffer key=%s verb=%s body=%r",
            key, verb, body[:40],
        )
        return preempted_emit, True

    def _on_continuation(
        self,
        parsed: ParsedFrame,
        now: float,
    ) -> Optional[AssembledMessage]:
        """Handle an UNKNOWN frame that might be a continuation.

        Match by audio-offset bucket. We don't filter on from-call
        (continuation frames don't carry one) or to-call. If
        multiple buffers happen to share the same audio bucket, the
        LATEST (most-recently-started) wins — this matches operator
        intuition: they hear the latest TX continuing, not a stale
        one from an hour ago.

        For buffered buffers: try CRC validation; emit if valid, or
        if EOT seen emit with checksum_valid=False.

        For non-buffered buffers: just append. Completion is timeout-
        or preempt-driven, never inline. The non-buffered buffer
        sits in self._buffers until ``_evict_stale_non_buffered``
        emits it.
        """
        bucket = _bucket_offset(parsed.decoded.frequency_hz)
        # Find candidate buffers at this audio offset bucket.
        candidates = [
            (k, b) for k, b in self._buffers.items()
            if k[2] == bucket
        ]
        if not candidates:
            return None
        # Most recently started wins — sorted by started_at desc.
        candidates.sort(key=lambda kv: kv[1].started_at, reverse=True)
        key, buf = candidates[0]

        # Don't strip the addition — gfsk8 preserves inter-frame
        # whitespace (huffEncode packs whole codes per frame, see
        # Varicode.cpp::packHuffMessage L1935). Stripping here would
        # destroy boundary spaces and break checksum validation for
        # any buffered MSG whose split happens to fall at a word
        # boundary. Observed on-air with KD8PGB's "TEST STORAGE
        # MESSAGE STORED " + "ON REFERENCE..." being concatenated as
        # "STOREDON" without the space.
        addition = parsed.body or ""
        # Newer JS8Call versions append EOT (\x04) to the very last
        # frame on the wire. gfsk8's huffDecode converts it to a
        # trailing space (Varicode.cpp::huffDecode L757-760), so we
        # don't typically see literal \x04. Check defensively in case
        # a future decoder passes it through — for buffered buffers
        # we use it as a fallback emit signal.
        eot_seen = _EOT_CHAR in addition
        addition = addition.replace(_EOT_CHAR, "")

        # JS8 packs continuation frames bit-for-bit but at character
        # boundaries (Varicode.cpp packs whole huffman codes only —
        # if a code wouldn't fit, it's deferred to the next frame and
        # the bits are padded). So inter-frame concatenation NEVER
        # needs separator insertion: the original characters' bits
        # are preserved exactly across the boundary, and any inter-
        # word space is encoded as a literal space character in
        # whichever frame's bits it happens to fit. Concat-with-no-
        # separator yields the exact original body.
        if buf.is_buffered_command:
            buf.body = buf.body + addition
        else:
            # Non-buffered: same JS8Call protocol contract applies —
            # whole-character packing means no separator needed.
            buf.body = buf.body + addition
        buf.last_frame_at = now
        buf.raw_offset_hz = parsed.decoded.frequency_hz
        buf.frame_count += 1

        _log.debug(
            "reassembly: continuation appended key=%s buffered=%s "
            "frame=%d body_len=%d eot=%s",
            key, buf.is_buffered_command,
            buf.frame_count, len(buf.body), eot_seen,
        )

        # Non-buffered: never inline-emit on continuation. Wait for
        # timeout (caller will pick it up via _evict_stale_non_buffered)
        # or for a new same-offset starter to preempt.
        if not buf.is_buffered_command:
            return None

        # Buffered: try CRC validation.
        if _checksum_required(buf.verb):
            validated = verify_checksum16(buf.body)
            if validated is not None:
                self._buffers.pop(key, None)
                return AssembledMessage(
                    from_call=buf.from_call,
                    to_call=buf.to_call,
                    verb=buf.verb,
                    body=validated,
                    checksum_valid=True,
                    raw_text=buf.body,
                    offset_hz=buf.bucket_hz,
                    started_at=buf.started_at,
                    completed_at=now,
                    frame_count=buf.frame_count,
                    was_buffered_command=True,
                )

        # EOT fallback for buffered with bad checksum.
        if eot_seen:
            self._buffers.pop(key, None)
            return AssembledMessage(
                from_call=buf.from_call,
                to_call=buf.to_call,
                verb=buf.verb,
                body=buf.body,
                checksum_valid=False,
                raw_text=buf.body,
                offset_hz=buf.bucket_hz,
                started_at=buf.started_at,
                completed_at=now,
                frame_count=buf.frame_count,
                was_buffered_command=True,
            )

        return None

    def _evict_stale_non_buffered(self, now: float) -> list[AssembledMessage]:
        """Emit any non-buffered buffer that's exceeded its timeout.

        Non-buffered messages don't have a checksum, so timeout =
        "no more frames are coming, this message is complete".
        Emit with ``checksum_valid=True`` so the dispatcher
        surfaces the message normally.
        """
        emitted: list[AssembledMessage] = []
        deadline = now - self._non_buffered_timeout_s
        for key, buf in list(self._buffers.items()):
            if buf.is_buffered_command:
                continue
            if buf.last_frame_at < deadline:
                self._buffers.pop(key, None)
                _log.info(
                    "reassembly: non-buffered timeout from=%s to=%s offset=%.0fHz "
                    "after %ds (%d frames, body_len=%d) — emitting",
                    buf.from_call, buf.to_call, buf.bucket_hz,
                    int(now - buf.started_at), buf.frame_count, len(buf.body),
                )
                emitted.append(AssembledMessage(
                    from_call=buf.from_call,
                    to_call=buf.to_call,
                    verb=buf.verb,
                    body=buf.body,
                    checksum_valid=True,
                    raw_text=buf.body,
                    offset_hz=buf.bucket_hz,
                    started_at=buf.started_at,
                    completed_at=now,
                    frame_count=buf.frame_count,
                    was_buffered_command=False,
                ))
        return emitted

    def _evict_stale_buffered(self, now: float) -> None:
        """Silently drop buffered buffers past their timeout.

        Buffered timeouts are NOT auto-emitted from feed() — they're
        retrieved via the public ``sweep_timeouts`` API which marks
        them ``checksum_valid=False`` so the dispatcher can decide
        whether to surface or drop them. This method is invoked
        on every feed() call to keep the buffer dict tidy when no
        sweep is happening.
        """
        deadline = now - self._frame_timeout_s
        for key, buf in list(self._buffers.items()):
            if not buf.is_buffered_command:
                continue
            if buf.last_frame_at < deadline:
                self._buffers.pop(key, None)
                _log.info(
                    "reassembly: timed out buffered buffer from=%s to=%s "
                    "offset=%.0fHz after %ds (%d frames, body_len=%d)",
                    buf.from_call, buf.to_call, buf.bucket_hz,
                    int(now - buf.started_at), buf.frame_count, len(buf.body),
                )

    def _evict_stale(
        self, now: float, *, return_evicted: bool = False,
    ) -> list[AssembledMessage]:
        """Legacy combined evictor — used only by ``sweep_timeouts``.

        Returns BUFFERED-only timeouts as checksum_valid=False
        AssembledMessages when ``return_evicted=True``. Non-buffered
        timeouts go through ``_evict_stale_non_buffered`` (always
        emitted with checksum_valid=True).
        """
        evicted: list[AssembledMessage] = []
        for key, buf in list(self._buffers.items()):
            if not buf.is_buffered_command:
                continue
            if buf.last_frame_at < now - self._frame_timeout_s:
                self._buffers.pop(key, None)
                if return_evicted:
                    evicted.append(AssembledMessage(
                        from_call=buf.from_call,
                        to_call=buf.to_call,
                        verb=buf.verb,
                        body=buf.body,
                        checksum_valid=False,
                        raw_text=buf.body,
                        offset_hz=buf.bucket_hz,
                        started_at=buf.started_at,
                        completed_at=now,
                        frame_count=buf.frame_count,
                        was_buffered_command=True,
                    ))
        return evicted


def _strip_eot(text: str) -> str:
    """Remove EOT (\\x04) characters from anywhere in the string."""
    return text.replace(_EOT_CHAR, "")


def is_buffered_protocol_frame(parsed: ParsedFrame) -> bool:
    """Whether the assembler will eventually consume this frame.

    Used by the outer dispatcher to decide whether to log this frame
    immediately to the directed-activity feed, or defer until the
    assembler emits a complete message. Buffered commands (MSG, MSG
    TO:, QUERY*, CMD, relay) flow through the assembler — those get
    logged when assembled, not when received frame-by-frame, to
    avoid double-counting in the chat view.

    Returns True for:
      - Buffered-verb starters (DIRECTED_MESSAGE / DIRECTED_COMMAND
        whose first token is in the buffered_cmds set per JS8Call's
        Varicode.cpp).
      - UNKNOWN-kind frames with a body (continuation frames at a
        previously-seen audio offset — the assembler tries to
        attach these to existing buffers).

    Returns False for everything else: HEARTBEAT, CQ, ACK,
    DIRECTED_QUERY (SNR?, INFO?, GRID?, etc.), and DIRECTED_COMMAND
    with non-buffered verbs (INFO, GRID, STATUS, HEARING, etc.).
    Those single-frame protocol exchanges are logged immediately at
    the decode handler.
    """
    if _classify_first_frame(parsed) is not None:
        return True
    if parsed.kind is FrameKind.UNKNOWN and parsed.body:
        return True
    return False
