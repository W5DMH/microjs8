"""Outbound message queue + retry state machine.

The queue is the single source of truth for "messages we want to TX".
Backed by a SQLite ``outbound`` table so queued messages survive a
daemon restart — a request typed by the operator at 22:00 will still
be queued at 22:01 even if the daemon crashed in between.

State machine
-------------

::

  QUEUED ──[scheduler picks me up, attempt #1]──> SENDING
  SENDING ──[TxBackend says success]─> WAIT_ACK / DELIVERED
  WAIT_ACK ──[ACK arrived]──> DELIVERED
  WAIT_ACK ──[90s elapsed, attempts < 3]──> QUEUED  (re-queue for retry)
  WAIT_ACK ──[90s elapsed, attempts >= 3]──> ABANDONED
  SENDING ──[TxBackend said no]──> QUEUED  (retry)

States are durable. The scheduler walks the table once per slot,
finds the next eligible row (oldest QUEUED), and asks TxBackend to
send it. State transitions are recorded in the same row.

Special handling per message kind:

  - HEARTBEAT, CQ, ALLCALL broadcasts have no specific recipient → never
    WAIT_ACK, go straight from SENDING to DELIVERED on a successful TX.
  - REPLY messages (auto-ACK to a received MSG, QUERY MSGS notification
    replies like "<asker> NO" or "<asker> MSG <id>") are directed at a
    specific callsign but per JS8Call protocol the recipient does NOT
    auto-ACK them — they're terminal in the protocol exchange. Treated
    like broadcasts for state-machine purposes: SENDING → DELIVERED.
  - DIRECTED messages (operator-typed messages, QUERY MSG <id> body
    deliveries that use the MSG verb) wait 90 s for an ACK after each
    TX attempt. The recipient's station auto-ACKs these.

Queue depth is capped at 3 (per spec) — additional ``enqueue()``
calls when full return False so the UI can flag "queue full".

ACK matching uses ``to_call`` + first-token-match-ish checks. The
scheduler feeds ACK frames to ``record_ack()`` whenever the protocol
parser produces ``FrameKind.ACK``; we match by from-call (the ACK's
sender is who we sent to).
"""

from __future__ import annotations

import enum
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger(__name__)


# Queue depth cap — operator-facing "you can have at most N pending".
QUEUE_DEPTH = 3

# How long to wait for an ACK after each TX attempt before retrying.
# 6 JS8 slots × 15 s = 90 s. Long enough for the recipient's slot
# rotation to actually fire, short enough that the queue moves.
ACK_TIMEOUT_S = 90.0

# Total attempts per message (initial + retries). Per Step 6 spec.
MAX_ATTEMPTS = 3


class OutboundState(str, enum.Enum):
    """Lifecycle of an outbound message."""
    ENCODING = "ENCODING"        # encoder worker is preparing audio (or queued for it)
    QUEUED = "QUEUED"           # audio ready, waiting for scheduler to pick up
    SENDING = "SENDING"          # currently in TxBackend.transmit()
    WAIT_ACK = "WAIT_ACK"        # transmitted, waiting for ACK
    DELIVERED = "DELIVERED"      # ACK received OR broadcast successful
    ABANDONED = "ABANDONED"      # retries exhausted


class OutboundKind(str, enum.Enum):
    """High-level classification of what's being sent.

    Used to decide whether ACK-tracking applies. Broadcasts skip
    WAIT_ACK and go straight to DELIVERED on successful TX.
    """
    HEARTBEAT = "HEARTBEAT"      # @HB broadcast (no ACK expected)
    CQ = "CQ"                    # CQ broadcast (no ACK expected)
    ALLCALL = "ALLCALL"          # @ALLCALL broadcast (no ACK expected)
    DIRECTED = "DIRECTED"        # to a specific callsign (ACK expected)
    REPLY = "REPLY"              # directed reply that does NOT expect an ACK
                                 # back (auto-ACKs to received MSGs, "NO"/"MSG <id>"
                                 # notifications in response to QUERY MSGS).
                                 # Per JS8Call protocol these are terminal in the
                                 # exchange — treating them as DIRECTED would loop
                                 # forever (we'd retransmit our ACK every 90s
                                 # waiting for an ACK to our ACK).


# ── OutboundKind inference ─────────────────────────────────────────


def infer_outbound_kind(text: str) -> OutboundKind:
    """Classify outbound text by examining its verb after the recipient.

    Used as a fallback when callers of ``enqueue`` / ``enqueue_for_encoding``
    don't supply an explicit ``kind``. Encodes the protocol-correct
    convention so manual tooling and future Compose-UI code can't
    accidentally re-introduce the "DIRECTED-kind on a query loops"
    bug we hit on-air.

    The convention
    --------------
    Outbound directed text follows the form ``"<recipient> <verb>
    [<body>]"``. The ``verb`` token determines whether the recipient's
    JS8Call will auto-ACK us:

      DIRECTED   ← ``MSG`` or ``MSG TO:`` — these are buffered mail
                   commands and the recipient's JS8Call auto-ACKs
                   the body delivery. The scheduler MUST put these
                   in WAIT_ACK so the inbound ACK closes the loop.

      REPLY      ← anything else: ``ACK``, ``NO``, ``YES``, ``QUERY
                   MSGS``, ``QUERY MSG <id>``, ``QUERY CALL``,
                   ``SNR?``, ``INFO``, ``GRID?``, ``STATUS``,
                   ``HEARING?``, etc. The recipient does NOT auto-ACK
                   these; their answer message (if any) IS the
                   protocol response. Putting these in WAIT_ACK
                   creates an infinite retransmit loop because no
                   ACK ever arrives.

    Broadcasts (HEARTBEAT, CQ, ALLCALL) are NOT inferred from text —
    callers must specify those explicitly. We can't reliably tell
    "@HB" from a normal directed message just by looking at the text.

    Edge cases
    ----------
    - Empty text → REPLY (safe default; will fail downstream anyway)
    - Text without any whitespace ("ACK" alone) → REPLY (treated as
      verb-only)
    - "MSG TO:" with no body or recipient → still DIRECTED (treated
      conservatively; the scheduler will see no ACK and abandon
      after retries, which is correct degradation)
    - Case-insensitive verb matching (JS8Call normalizes to upper
      case on TX, so "msg" matches MSG).
    """
    if not text:
        return OutboundKind.REPLY

    s = text.strip()
    if not s:
        return OutboundKind.REPLY

    # Format: "<recipient> <verb> [<body>]". Tokenize after the
    # recipient — we need the second token AND we need to peek at
    # whether it's followed by "TO:" for the multi-word "MSG TO:" case.
    parts = s.split(None, 2)
    if len(parts) < 2:
        # Just a recipient with no verb — invalid form, but pick a
        # safe kind. REPLY won't loop on a malformed TX.
        return OutboundKind.REPLY

    verb = parts[1].upper()

    # MSG TO:<recipient> ... — the verb spans two whitespace-separated
    # tokens. Check this BEFORE plain MSG so we don't false-match.
    if verb == "MSG" and len(parts) >= 3 and parts[2].upper().startswith("TO:"):
        return OutboundKind.DIRECTED

    # Plain MSG <body>
    if verb == "MSG":
        return OutboundKind.DIRECTED

    # Everything else: REPLY. This is the safe default — it won't
    # cause loops, and the only cost of mis-classifying a future
    # buffered-MSG-equivalent as REPLY is that we'd lose ACK-tracking
    # for it. When new buffered verbs are added (rare), this function
    # gets one new branch.
    return OutboundKind.REPLY


@dataclass(frozen=True)
class OutboundMessage:
    """One row in the outbound queue."""
    id: int
    kind: OutboundKind
    text: str                   # the actual JS8 frame body, ≤ 12 chars
    to_call: Optional[str]       # destination, None for broadcasts
    state: OutboundState
    attempts: int                # number of TX attempts so far
    enqueued_at: float           # unix epoch, for FIFO ordering
    last_tx_at: Optional[float]  # unix epoch of most recent attempt
    error: Optional[str]         # last failure reason (set on retry/abandon)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbound (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    to_call      TEXT,
    state        TEXT    NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    enqueued_at  REAL    NOT NULL,
    last_tx_at   REAL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbound_state ON outbound(state);
CREATE INDEX IF NOT EXISTS idx_outbound_enqueued ON outbound(enqueued_at);
"""


class OutboundQueue:
    """SQLite-backed queue of pending outbound messages.

    Uses the same connection as the existing MessageStore (passed in
    via constructor) so we don't open a second SQLite handle on the
    same file — WAL mode supports many readers but only one writer,
    and serialization is simpler with a shared connection.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)

    # ── Enqueue ──────────────────────────────────────────────────────

    def enqueue(
        self,
        text: str,
        kind: Optional[OutboundKind] = None,
        to_call: Optional[str] = None,
    ) -> Optional[int]:
        """Add a message directly to QUEUED state.

        Used by callers that already have audio cached (or want the
        legacy synchronous-encode-at-TX-time behavior). Most production
        code should use ``enqueue_for_encoding()`` instead, which
        creates the row in ENCODING state so the EncodeWorker renders
        audio off the slot-aligned hot path.

        Parameters
        ----------
        kind : OutboundKind or None
            If None (the default), inferred from ``text`` via
            ``infer_outbound_kind``. Explicit values override the
            inference — callers that know their kind (broadcasts;
            tests; tooling) should pass it. The auto-inference
            ensures manual tooling can't accidentally queue an
            outbound query as DIRECTED and trigger the WAIT_ACK
            retransmit loop.

        Returns the row id on success, None if the queue is full
        (depth >= QUEUE_DEPTH for ENCODING + QUEUED + SENDING + WAIT_ACK rows).
        """
        if kind is None:
            kind = infer_outbound_kind(text)
        # Count rows that haven't reached terminal state. ENCODING is
        # included so the queue-full check is honored even while
        # messages are still being encoded.
        active_states = (
            OutboundState.ENCODING.value,
            OutboundState.QUEUED.value,
            OutboundState.SENDING.value,
            OutboundState.WAIT_ACK.value,
        )
        placeholders = ",".join(["?"] * len(active_states))
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM outbound WHERE state IN ({placeholders})",
            active_states,
        ).fetchone()
        active = row[0] if row else 0

        if active >= QUEUE_DEPTH:
            _log.warning(
                "outbound queue full (%d/%d active); rejecting %r",
                active, QUEUE_DEPTH, text,
            )
            return None

        cur = self._conn.execute(
            "INSERT INTO outbound("
            "  kind, text, to_call, state, attempts, enqueued_at"
            ") VALUES (?, ?, ?, ?, 0, ?)",
            (
                kind.value,
                text,
                to_call,
                OutboundState.QUEUED.value,
                time.time(),
            ),
        )
        new_id = cur.lastrowid or 0
        _log.info("queued outbound id=%d kind=%s to=%s text=%r",
                  new_id, kind.value, to_call, text)
        return new_id

    def enqueue_for_encoding(
        self,
        text: str,
        kind: Optional[OutboundKind] = None,
        to_call: Optional[str] = None,
    ) -> Optional[int]:
        """Enqueue a row in ENCODING state for the EncodeWorker.

        New rows go to ENCODING state. The encode worker picks them
        up, renders audio, and transitions to QUEUED. The scheduler
        only picks QUEUED rows — never ENCODING — so a row is never
        TX'd before its audio is ready.

        This is the production path. Use ``enqueue()`` only when you
        need the row to skip directly to QUEUED (legacy / tests).

        Parameters
        ----------
        kind : OutboundKind or None
            If None (the default), inferred from ``text`` via
            ``infer_outbound_kind``. See ``enqueue()`` for the
            rationale — same auto-classification applies here, and
            this is the path most production callers (and any future
            Compose / CLI / REPL tooling) will use.

        Returns the row id on success, None if the queue is full.
        """
        if kind is None:
            kind = infer_outbound_kind(text)
        active_states = (
            OutboundState.ENCODING.value,
            OutboundState.QUEUED.value,
            OutboundState.SENDING.value,
            OutboundState.WAIT_ACK.value,
        )
        placeholders = ",".join(["?"] * len(active_states))
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM outbound WHERE state IN ({placeholders})",
            active_states,
        ).fetchone()
        active = row[0] if row else 0

        if active >= QUEUE_DEPTH:
            _log.warning(
                "outbound queue full (%d/%d active); rejecting %r",
                active, QUEUE_DEPTH, text,
            )
            return None

        cur = self._conn.execute(
            "INSERT INTO outbound("
            "  kind, text, to_call, state, attempts, enqueued_at"
            ") VALUES (?, ?, ?, ?, 0, ?)",
            (
                kind.value,
                text,
                to_call,
                OutboundState.ENCODING.value,
                time.time(),
            ),
        )
        new_id = cur.lastrowid or 0
        _log.info(
            "queued outbound id=%d kind=%s to=%s text=%r (for encoding)",
            new_id, kind.value, to_call, text,
        )
        return new_id

    # ── Pick next eligible message for TX ────────────────────────────

    def pick_next(self) -> Optional[OutboundMessage]:
        """Return the next QUEUED message, FIFO order.

        Does NOT mark it as SENDING — that's the scheduler's job
        once it's about to actually transmit. Returns None if no
        messages are queued.
        """
        row = self._conn.execute(
            "SELECT * FROM outbound WHERE state=? "
            "ORDER BY enqueued_at ASC LIMIT 1",
            (OutboundState.QUEUED.value,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_message(row)

    # ── State transitions ────────────────────────────────────────────

    def pick_next_encoding(self) -> Optional[OutboundMessage]:
        """Return the oldest ENCODING message (FIFO).

        Used by the EncodeWorker to find work. Returns None when no
        messages need encoding. Does NOT change state — the worker
        encodes the audio then calls ``mark_encoded()`` on success
        or ``mark_abandoned()`` on permanent failure.
        """
        row = self._conn.execute(
            "SELECT * FROM outbound WHERE state=? "
            "ORDER BY enqueued_at ASC LIMIT 1",
            (OutboundState.ENCODING.value,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_message(row)

    def mark_encoded(self, message_id: int) -> None:
        """Transition ENCODING → QUEUED.

        Called by the EncodeWorker after audio has been rendered and
        cached. Once a row reaches QUEUED state, the scheduler will
        pick it up on the next slot tick.
        """
        self._conn.execute(
            "UPDATE outbound SET state=? WHERE id=? AND state=?",
            (
                OutboundState.QUEUED.value,
                message_id,
                OutboundState.ENCODING.value,
            ),
        )

    def reset_unencoded_to_encoding(self) -> int:
        """Recover from daemon restart.

        The encoded-audio cache is in-memory only (per design). After
        a restart, any rows in QUEUED state have lost their cached
        audio, and any rows in ENCODING state never finished. Both
        need to be re-encoded by the worker — push them back to (or
        keep them at) ENCODING so the worker picks them up.

        Called once during app startup, BEFORE the encode worker or
        scheduler start. Returns the count of rows reset (for logging).
        """
        cur = self._conn.execute(
            "UPDATE outbound SET state=? "
            "WHERE state IN (?, ?)",
            (
                OutboundState.ENCODING.value,
                OutboundState.QUEUED.value,
                OutboundState.ENCODING.value,
            ),
        )
        return cur.rowcount

    def mark_sending(self, message_id: int) -> None:
        """Transition QUEUED → SENDING. Increments attempts."""
        self._conn.execute(
            "UPDATE outbound "
            "SET state=?, attempts=attempts+1, last_tx_at=? "
            "WHERE id=?",
            (OutboundState.SENDING.value, time.time(), message_id),
        )

    def mark_wait_ack(self, message_id: int) -> None:
        """Transition SENDING → WAIT_ACK (directed messages only)."""
        self._conn.execute(
            "UPDATE outbound SET state=? WHERE id=?",
            (OutboundState.WAIT_ACK.value, message_id),
        )

    def mark_delivered(self, message_id: int) -> None:
        """Transition any state → DELIVERED.

        Used both for broadcasts (succeed immediately on TX) and for
        directed messages whose ACK arrived.
        """
        self._conn.execute(
            "UPDATE outbound SET state=?, error=NULL WHERE id=?",
            (OutboundState.DELIVERED.value, message_id),
        )

    def mark_retry(self, message_id: int, error: str) -> None:
        """SENDING → QUEUED (TX failed, will retry on next slot).

        attempts is NOT decremented — it was incremented in mark_sending.
        Caller checks attempts vs MAX_ATTEMPTS before calling this.
        """
        self._conn.execute(
            "UPDATE outbound SET state=?, error=? WHERE id=?",
            (OutboundState.QUEUED.value, error, message_id),
        )

    def mark_abandoned(self, message_id: int, error: str) -> None:
        """Final failure state. Stays in the table for forensics."""
        self._conn.execute(
            "UPDATE outbound SET state=?, error=? WHERE id=?",
            (OutboundState.ABANDONED.value, error, message_id),
        )

    def abandon_stale_sending(self, error: str) -> int:
        """Sweep any rows still in SENDING and mark them ABANDONED.

        Called once at scheduler startup to clean up rows that were
        mid-TX when the daemon last shut down. Multi-frame messages
        that didn't finish are undecodable from the receiver's
        perspective (frames received aren't a complete JS8 message),
        so there's nothing useful to recover. Marking them ABANDONED
        rather than re-queuing avoids unintended re-TX of partially-
        sent content; rather than DELETE-ing, we keep the row for the
        Outbound view's audit trail (operator sees "NO RESPONSE ✗"
        with the supplied error message).

        Returns the number of rows that were transitioned.
        """
        cur = self._conn.execute(
            "UPDATE outbound SET state=?, error=? WHERE state=?",
            (
                OutboundState.ABANDONED.value,
                error,
                OutboundState.SENDING.value,
            ),
        )
        return cur.rowcount

    # ── ACK matching ─────────────────────────────────────────────────

    def record_ack(self, ack_from_call: str) -> Optional[int]:
        """Match an incoming ACK against WAIT_ACK rows.

        Returns the matched message id (now DELIVERED) or None.

        Matching: most recent WAIT_ACK row whose to_call matches the
        ACK's sender (case-insensitive). "Most recent" because the
        same callsign might have multiple pending QSOs in unusual
        circumstances; the most recent is the one most likely to be
        ACK-able.
        """
        if not ack_from_call:
            return None
        row = self._conn.execute(
            "SELECT id FROM outbound "
            "WHERE state=? AND UPPER(to_call)=? "
            "ORDER BY last_tx_at DESC LIMIT 1",
            (OutboundState.WAIT_ACK.value, ack_from_call.upper()),
        ).fetchone()
        if row is None:
            return None
        msg_id = row[0]
        self.mark_delivered(msg_id)
        _log.info("ACK from %s matched outbound id=%d", ack_from_call, msg_id)
        return msg_id

    # ── Retry FSM dispatch (called by scheduler) ─────────────────────

    def find_timed_out_acks(self, now: float) -> list[OutboundMessage]:
        """Return WAIT_ACK rows whose timeout has elapsed.

        The scheduler calls this once per slot. For each row, it
        decides: re-queue for retry (if attempts < MAX), or abandon.
        """
        cutoff = now - ACK_TIMEOUT_S
        rows = self._conn.execute(
            "SELECT * FROM outbound "
            "WHERE state=? AND last_tx_at < ?",
            (OutboundState.WAIT_ACK.value, cutoff),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    # ── Inspection (UI / diagnostics) ────────────────────────────────

    def get(self, message_id: int) -> Optional[OutboundMessage]:
        row = self._conn.execute(
            "SELECT * FROM outbound WHERE id=?", (message_id,),
        ).fetchone()
        return self._row_to_message(row) if row else None

    def all_active(self) -> list[OutboundMessage]:
        """Rows that aren't in a terminal state, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM outbound "
            "WHERE state IN (?, ?, ?) "
            "ORDER BY enqueued_at DESC",
            (
                OutboundState.QUEUED.value,
                OutboundState.SENDING.value,
                OutboundState.WAIT_ACK.value,
            ),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def all_abandoned(self, limit: int = 50) -> list[OutboundMessage]:
        rows = self._conn.execute(
            "SELECT * FROM outbound WHERE state=? "
            "ORDER BY last_tx_at DESC LIMIT ?",
            (OutboundState.ABANDONED.value, limit),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def active_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM outbound WHERE state IN (?, ?, ?)",
            (
                OutboundState.QUEUED.value,
                OutboundState.SENDING.value,
                OutboundState.WAIT_ACK.value,
            ),
        ).fetchone()
        return row[0] if row else 0

    # ── Internal ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_message(row) -> OutboundMessage:
        # row is sqlite3.Row (the connection has row_factory set).
        return OutboundMessage(
            id=row["id"],
            kind=OutboundKind(row["kind"]),
            text=row["text"],
            to_call=row["to_call"],
            state=OutboundState(row["state"]),
            attempts=row["attempts"],
            enqueued_at=row["enqueued_at"],
            last_tx_at=row["last_tx_at"],
            error=row["error"],
        )
