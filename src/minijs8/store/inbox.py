"""JS8Call-compatible inbox / mailbox store.

This module manages the operator's mailbox — a persistent store of
inbound directed messages (UNREAD/READ), held mail we're storing for
other stations (STORE), and delivered held mail (DELIVERED). Stored
in a separate SQLite database (``/var/minijs8/inbox.db``) from the
decode log so the two have independent lifetimes and retention rules.

Schema design philosophy
========================

We adopt JS8Call's actual schema verbatim — one polymorphic table
``inbox_v1`` with a JSON blob discriminated by ``$.type``, plus
expression indices on the JSON paths the application queries by.
Reasons for matching JS8Call exactly rather than a Python-native
typed-column design:

* **Interoperability.** If we ever expose the JS8Call TCP API, the
  blob shape is what the API speaks. Same-shape storage means clients
  can talk to our station the same way they talk to JS8Call.
* **Protocol IDs are simpler.** The row's ``AUTOINCREMENT id`` IS the
  JS8 protocol message id. We transmit it directly in
  ``QUERY MSG <id>`` and HB-ACK piggyback (`MSG <id>`). No mapping
  layer.
* **Future-proofness.** Adding new states is a new ``type`` value, not
  a schema migration.
* **Reference compatibility.** A JS8Call inbox.db3 dropped onto our
  system can be read directly. Less of a footgun for operators
  migrating between stacks.

The downside — JSON-in-text isn't idiomatic Python — is mitigated by
the JSON-path indices being indexed by SQLite ≥ 3.38 (Pi Bookworm
ships 3.40). Lookups by FROM/TO/type are O(log n).

Type lifecycle
==============

::

   UNREAD ────[mark_read]───→ READ ────[delete]───→ (gone)
                                  ↑
                     [delete]─────┘
   UNREAD ────[delete]──────────────────────────→ (gone)

   STORE ─────[mark_delivered]──→ DELIVERED ────[delete]──→ (gone)
   STORE ─────[delete]──────────────────────────────────→ (gone)

* ``UNREAD`` — addressed to us, decoded as ``<from>: <us> MSG <body>``.
  Auto-ACK was sent at decode time (in app.py); the row just sits
  here waiting for the operator to read it.
* ``READ`` — operator viewed the detail-view of a UNREAD row.
* ``STORE`` — held mail. Either the operator stored it via the
  "Store Locally for <CALLSIGN>" action (FROM=our_call), OR a remote
  station gave us ``MSG TO:`` to hold (FROM=remote_sender). The TO
  field is always the eventual recipient. When that recipient sends
  ``QUERY MSGS`` and we deliver, we transition to DELIVERED on their
  ACK.
* ``DELIVERED`` — held mail that's been retrieved and ACK'd. Kept
  for operator audit but no longer offered in QUERY MSGS responses.

Blob shape (matches JS8Call's params layout)
============================================

::

   {
     "type": "UNREAD" | "READ" | "STORE" | "DELIVERED",
     "params": {
       "FROM":   "<callsign>",     # sender of the message
       "TO":     "<callsign>",     # destination (us for UNREAD/READ;
                                   #              recipient for STORE/DELIVERED)
       "TEXT":   "<body>",         # message text only (verb stripped)
       "UTC":    "ISO8601",        # when we received/stored it
       "OFFSET": <int>,            # audio frequency Hz at receipt; null for local-store
       "SNR":    <int>             # SNR at receipt; null for local-store
     }
   }

Concurrency model
=================

The decode pipeline is the writer (one thread, calling
``add_unread`` / ``add_remote_store``). The asyncio thread is the
reader (UI snapshot, QUERY MSGS lookup) and a writer for state
transitions (``mark_read``, ``mark_delivered``, ``delete``,
``add_local_store``). SQLite WAL mode supports concurrent
reader-while-writer; we open with ``check_same_thread=False`` so the
asyncio thread can call methods directly. All public methods are
short — no ``to_thread`` wrapping needed for the tiny per-row
operations involved.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_log = logging.getLogger(__name__)


# JS8Call's exact schema text. Indexed by the JSON paths the
# application queries on (type, FROM, TO). We add a UTC index too for
# ORDER BY date — newest-first inbox rendering would otherwise be a
# full table scan after the table grows.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inbox_v1 (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    blob TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_v1__type
    ON inbox_v1(json_extract(blob, '$.type'));
CREATE INDEX IF NOT EXISTS idx_inbox_v1__params_from
    ON inbox_v1(json_extract(blob, '$.params.FROM'));
CREATE INDEX IF NOT EXISTS idx_inbox_v1__params_to
    ON inbox_v1(json_extract(blob, '$.params.TO'));
CREATE INDEX IF NOT EXISTS idx_inbox_v1__params_utc
    ON inbox_v1(json_extract(blob, '$.params.UTC'));

-- Companion table from JS8Call: tracks which group-callsign
-- recipients have already received a particular held message. We
-- create the table for compatibility but don't populate it in this
-- phase (no group-callsign STORE behavior yet); rows here are added
-- only when a group-targeted message is delivered to a specific
-- subscriber. Keeps our schema readable by JS8Call without needing
-- a migration later.
CREATE TABLE IF NOT EXISTS inbox_group_recip_v1 (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id   INTEGER,
    callsign VARCHAR(255),
    FOREIGN KEY(msg_id) REFERENCES inbox_v1(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_inbox_group_recip_v1__callsign
    ON inbox_group_recip_v1(callsign);
"""

# Recognized type values. We restrict writes to these so a typo in
# code doesn't corrupt the schema-discriminator column.
TYPE_UNREAD = "UNREAD"
TYPE_READ = "READ"
TYPE_STORE = "STORE"
TYPE_DELIVERED = "DELIVERED"

_VALID_TYPES = frozenset({TYPE_UNREAD, TYPE_READ, TYPE_STORE, TYPE_DELIVERED})


class MailboxError(Exception):
    """Raised when an inbox-store operation cannot complete."""


@dataclass(frozen=True)
class InboxRecord:
    """In-Python view of one row from inbox_v1.

    Frozen for thread safety — UI snapshots can hand these off
    directly without copying.
    """

    id: int
    type: str               # UNREAD / READ / STORE / DELIVERED
    from_call: str
    to_call: str
    text: str
    utc_iso: str            # ISO 8601 timestamp string
    offset_hz: Optional[int]
    snr_db: Optional[int]


def _now_iso() -> str:
    """ISO 8601 UTC timestamp at millisecond resolution.

    Format matches JS8Call's INBOX.MESSAGE replies (which use a
    timezone-aware ISO timestamp). Millisecond resolution is plenty —
    JS8 slot boundaries are 15 s apart, so any sub-second precision
    is just to keep ordering deterministic when two messages decode
    in the same slot.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _build_blob(
    *,
    msg_type: str,
    from_call: str,
    to_call: str,
    text: str,
    utc_iso: Optional[str] = None,
    offset_hz: Optional[int] = None,
    snr_db: Optional[int] = None,
) -> str:
    """Construct the JSON blob string we write into the table.

    Centralized so all writes use the same shape. JSON keys are
    upper-case to match JS8Call's params convention.
    """
    if msg_type not in _VALID_TYPES:
        raise MailboxError(
            f"unknown msg_type {msg_type!r}; "
            f"must be one of {sorted(_VALID_TYPES)}"
        )
    if not from_call:
        raise MailboxError("from_call must be non-empty")
    if not to_call:
        raise MailboxError("to_call must be non-empty")
    # text is allowed to be empty (e.g. an empty-body MSG is
    # technically a valid JS8 frame though semantically odd).

    payload = {
        "type": msg_type,
        "params": {
            "FROM": from_call,
            "TO": to_call,
            "TEXT": text,
            "UTC": utc_iso or _now_iso(),
            # OFFSET / SNR are nullable (None for local-store rows
            # since they were never on-air).
            "OFFSET": offset_hz,
            "SNR": snr_db,
        },
    }
    # ensure_ascii=False so unicode bodies (Latin-1 supplemental,
    # e.g. accented Spanish/French characters) survive intact.
    # separators=(",", ":") avoids whitespace bloat — at scale we'll
    # have hundreds of these blobs and every byte matters on SD card.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _record_from_row(row_id: int, blob: str) -> InboxRecord:
    """Parse a (id, blob) DB row into an InboxRecord.

    Raises MailboxError if the blob is malformed — should never
    happen since we control all writes, but defensive against
    manual SQL edits or DB corruption.
    """
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise MailboxError(
            f"row id={row_id} has malformed JSON blob: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise MailboxError(
            f"row id={row_id} blob is not a JSON object"
        )
    msg_type = payload.get("type", "")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise MailboxError(
            f"row id={row_id} has non-object params"
        )
    return InboxRecord(
        id=row_id,
        type=msg_type,
        from_call=params.get("FROM", "") or "",
        to_call=params.get("TO", "") or "",
        text=params.get("TEXT", "") or "",
        utc_iso=params.get("UTC", "") or "",
        offset_hz=params.get("OFFSET"),
        snr_db=params.get("SNR"),
    )


class MailboxStore:
    """Synchronous SQLite wrapper around the inbox_v1 schema.

    All public methods are short and safe to call from the asyncio
    thread directly — no ``asyncio.to_thread`` wrapping needed for
    the per-row operations.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        # check_same_thread=False because the decode thread (a
        # synchronous worker started by app.py) writes UNREAD/STORE
        # rows while the asyncio thread reads for UI snapshots and
        # writes state transitions. WAL mode below makes this
        # concurrency safe.
        self._conn = sqlite3.connect(
            str(db_path),
            isolation_level=None,  # autocommit; we manage our own txns
            check_same_thread=False,
        )
        self._lock = threading.Lock()
        self._init_schema()

    # ── Schema ─────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        """Run the schema-create SQL.

        Idempotent — IF NOT EXISTS clauses make it safe to call on
        every open. Also enables WAL mode and SD-friendly synchronous
        setting (matches db.py policy).
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")
                cur.execute("PRAGMA foreign_keys=ON;")
                # Multi-statement schema requires executescript.
                cur.executescript(_SCHEMA_SQL)
            finally:
                cur.close()

    def close(self) -> None:
        """Close the connection. Idempotent."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                _log.exception("MailboxStore close raised")

    # ── Insert helpers (one per type/source combination) ───────────

    def add_unread(
        self,
        *,
        from_call: str,
        text: str,
        utc_iso: Optional[str] = None,
        offset_hz: Optional[int] = None,
        snr_db: Optional[int] = None,
        our_call: str,
    ) -> int:
        """Record a directed-MSG message addressed to us.

        Called by the decode handler when an inbound directed frame
        is parsed as ``<from_call>: <our_call> MSG <text>``. The
        caller is responsible for the auto-ACK transmission — this
        method only persists the record.
        """
        blob = _build_blob(
            msg_type=TYPE_UNREAD,
            from_call=from_call,
            to_call=our_call,
            text=text,
            utc_iso=utc_iso,
            offset_hz=offset_hz,
            snr_db=snr_db,
        )
        return self._insert_blob(blob)

    def add_local_store(
        self,
        *,
        recipient_call: str,
        text: str,
        our_call: str,
    ) -> int:
        """Operator-initiated 'Store Locally for <CALLSIGN>'.

        Records a STORE row with FROM=our_call (we are the originator),
        TO=recipient_call. The recipient pulls this via QUERY MSGS
        directed at us.

        OFFSET/SNR are None — this row was never on-air.
        """
        blob = _build_blob(
            msg_type=TYPE_STORE,
            from_call=our_call,
            to_call=recipient_call,
            text=text,
            utc_iso=_now_iso(),
            offset_hz=None,
            snr_db=None,
        )
        return self._insert_blob(blob)

    def add_remote_store(
        self,
        *,
        sender_call: str,
        recipient_call: str,
        text: str,
        utc_iso: Optional[str] = None,
        offset_hz: Optional[int] = None,
        snr_db: Optional[int] = None,
    ) -> int:
        """Record an inbound MSG TO:<recipient> hold-request.

        Frame shape parsed by the grammar:
        ``<sender_call>: <our_call> MSG TO:<recipient_call> <text>``

        We auto-ACK the sender (handled in app.py) and store the
        message indefinitely. Later, when ``recipient_call`` sends us
        ``QUERY MSGS`` (or ``@<group> QUERY MSGS`` if we're in that
        group), we offer this row's id.
        """
        blob = _build_blob(
            msg_type=TYPE_STORE,
            from_call=sender_call,
            to_call=recipient_call,
            text=text,
            utc_iso=utc_iso,
            offset_hz=offset_hz,
            snr_db=snr_db,
        )
        return self._insert_blob(blob)

    def _insert_blob(self, blob: str) -> int:
        """Append a row, return its assigned id."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO inbox_v1 (blob) VALUES (?);",
                    (blob,),
                )
                row_id = cur.lastrowid
                if row_id is None:
                    raise MailboxError("INSERT returned no lastrowid")
                return row_id
            finally:
                cur.close()

    # ── State transitions ──────────────────────────────────────────

    def mark_read(self, row_id: int) -> bool:
        """Transition UNREAD → READ. No-op if already READ.

        Returns True if a UNREAD row was found and updated, False if
        the row doesn't exist or was in some other state. Used by the
        UI when the operator opens detail-view on an inbox message.
        """
        return self._transition_type(row_id, expect=TYPE_UNREAD, new=TYPE_READ)

    def mark_delivered(self, row_id: int) -> bool:
        """Transition STORE → DELIVERED.

        Called when the recipient ACKs our delivery of a held message.
        Returns True if a STORE row was found and updated.
        """
        return self._transition_type(row_id, expect=TYPE_STORE, new=TYPE_DELIVERED)

    def _transition_type(
        self, row_id: int, *, expect: str, new: str,
    ) -> bool:
        """Update a row's type if it matches ``expect``.

        Atomic via WHERE clause — if two threads race, only one wins.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                # json_set returns a NEW JSON string with $.type
                # replaced. The WHERE clause makes the update a no-op
                # if the row's current type doesn't match expectations.
                cur.execute(
                    "UPDATE inbox_v1 "
                    "SET blob = json_set(blob, '$.type', ?) "
                    "WHERE id = ? "
                    "AND json_extract(blob, '$.type') = ?;",
                    (new, row_id, expect),
                )
                return cur.rowcount > 0
            finally:
                cur.close()

    def delete(self, row_id: int) -> bool:
        """Permanently remove a row. Returns True if a row was deleted."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("DELETE FROM inbox_v1 WHERE id = ?;", (row_id,))
                return cur.rowcount > 0
            finally:
                cur.close()

    # ── Read API ───────────────────────────────────────────────────

    def get(self, row_id: int) -> Optional[InboxRecord]:
        """Fetch a single row by id, or None if not found."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT id, blob FROM inbox_v1 WHERE id = ? LIMIT 1;",
                    (row_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return _record_from_row(row[0], row[1])
            finally:
                cur.close()

    def list_inbox(self, *, limit: int = 100) -> list[InboxRecord]:
        """All UNREAD + READ rows, newest first.

        This is what the Inbox screen renders. Limit is a defensive
        cap so a corrupt DB or a long-running station doesn't blow
        out memory on a slow Pi Zero.
        """
        return self._list_by_types(
            (TYPE_UNREAD, TYPE_READ),
            limit=limit,
            newest_first=True,
        )

    def list_unread(self, *, limit: int = 100) -> list[InboxRecord]:
        """UNREAD rows only — for the home-screen unread count."""
        return self._list_by_types(
            (TYPE_UNREAD,),
            limit=limit,
            newest_first=True,
        )

    def list_holding_for(
        self, recipient_call: str, *, limit: int = 100,
    ) -> list[InboxRecord]:
        """STORE rows where TO matches ``recipient_call``.

        Used to answer ``QUERY MSGS`` from that station — the oldest
        pending row's id goes back as ``MSG <id>``. We return them
        oldest-first so callers can pick the head element.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT id, blob FROM inbox_v1 "
                    "WHERE json_extract(blob, '$.type') = ? "
                    "AND json_extract(blob, '$.params.TO') = ? "
                    "ORDER BY id ASC "  # oldest first
                    "LIMIT ?;",
                    (TYPE_STORE, recipient_call, limit),
                )
                rows = cur.fetchall()
            finally:
                cur.close()
        return [_record_from_row(r[0], r[1]) for r in rows]

    def count_holding(self) -> int:
        """Total count of STORE rows.

        Used by the Home-screen indicator — the operator wants to see
        at a glance "I'm holding 3 messages for other stations." Cheap
        because we have an index on $.type.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM inbox_v1 "
                    "WHERE json_extract(blob, '$.type') = ?;",
                    (TYPE_STORE,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
            finally:
                cur.close()

    def count_unread(self) -> int:
        """Number of UNREAD rows.

        Same purpose as count_holding but for the operator's own
        inbound mail. Cheap via index.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM inbox_v1 "
                    "WHERE json_extract(blob, '$.type') = ?;",
                    (TYPE_UNREAD,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
            finally:
                cur.close()

    def _list_by_types(
        self,
        types: tuple[str, ...],
        *,
        limit: int,
        newest_first: bool,
    ) -> list[InboxRecord]:
        """Internal: SELECT rows matching any of the given types.

        Uses parameter expansion — SQLite doesn't support array
        binding, so we build a placeholder list and bind each type
        individually. Indexed lookup via idx_inbox_v1__type.
        """
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        order = "DESC" if newest_first else "ASC"
        sql = (
            "SELECT id, blob FROM inbox_v1 "
            f"WHERE json_extract(blob, '$.type') IN ({placeholders}) "
            f"ORDER BY id {order} "
            "LIMIT ?;"
        )
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(sql, (*types, limit))
                rows = cur.fetchall()
            finally:
                cur.close()
        return [_record_from_row(r[0], r[1]) for r in rows]
