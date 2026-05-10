"""SQLite-backed append-only message store.

Schema:

  decodes      : every parsed frame, ever. Foundation for retention,
                 statistics, and forensic logging. WAL-mode SQLite for
                 concurrent read while the asyncio loop writes.

  heard_stations: most-recent-sighting cache (one row per callsign).
                  Updated on every decode. Read by the Heard List
                  screen at every redraw — kept small and indexed
                  by callsign so the read is O(log N) at most.

Design notes:

  - We use the stdlib ``sqlite3`` module directly. No ``aiosqlite``
    dep — its only benefit is non-blocking I/O, but our write rate is
    measured in *frames per second at most* (a few per slot), and the
    asyncio loop wraps each write in ``asyncio.to_thread`` so the
    event loop never blocks.

  - PRAGMA journal_mode=WAL gives us reads-during-writes without
    locking the entire database. PRAGMA synchronous=NORMAL is the
    SD-card-friendly choice — slightly less crash-resistant than FULL,
    much less SD wear.

  - Retention runs once per hour: delete decodes older than the
    configured number of days (30 by default). Heard-station rows are
    NOT pruned by retention; they're a top-N cache and are evicted
    only when a callsign hasn't been heard in a long time AND the
    table has grown large.

The Step 5 surface is small: ``insert_decode``, ``upsert_heard_station``,
``recent_decodes``, ``heard_stations``, ``prune_older_than``. Step 6
will add outgoing-message tracking.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from microjs8.protocol.types import HeardStation, ParsedFrame

_log = logging.getLogger(__name__)

# How many heard-station rows to keep at most. The screen shows ~11.
# 200 gives us comfortable headroom for "scroll back through a busy
# afternoon" without unbounded growth on a long-running daemon.
_HEARD_CAP = 200


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     REAL NOT NULL,            -- unix epoch seconds
    snr_db          INTEGER NOT NULL,
    frequency_hz    REAL NOT NULL,
    submode         INTEGER NOT NULL,
    quality         INTEGER NOT NULL,
    frame_type      INTEGER NOT NULL,
    kind            TEXT NOT NULL,            -- FrameKind.value
    from_call       TEXT,
    to_call         TEXT,
    grid            TEXT,
    body            TEXT,
    raw             TEXT,                      -- 12-char raw payload
    text            TEXT NOT NULL              -- Varicode-unpacked
);
CREATE INDEX IF NOT EXISTS idx_decodes_received_at ON decodes(received_at);
CREATE INDEX IF NOT EXISTS idx_decodes_from        ON decodes(from_call);
CREATE INDEX IF NOT EXISTS idx_decodes_to          ON decodes(to_call);
CREATE INDEX IF NOT EXISTS idx_decodes_kind        ON decodes(kind);

CREATE TABLE IF NOT EXISTS heard_stations (
    callsign        TEXT PRIMARY KEY,
    last_heard      REAL NOT NULL,
    snr_db          INTEGER NOT NULL,
    grid            TEXT,
    frequency_hz    REAL NOT NULL,
    distance_mi     REAL,                      -- NULL if our grid unknown
    bearing_deg     REAL
);
CREATE INDEX IF NOT EXISTS idx_heard_last ON heard_stations(last_heard DESC);
"""


class MessageStore:
    """Wraps the SQLite connection.

    All methods are synchronous — call them from the asyncio thread
    via ``asyncio.to_thread`` for the rare slow operations (retention
    sweeps, big queries). Single-connection: SQLite is fine with one
    writer + many readers, and we have one writer (decode pipeline)
    plus one reader (UI render) at most.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        # check_same_thread=False because we use the connection from
        # the asyncio thread AND from to_thread workers. We serialize
        # writes with a process-level lock if we ever add a second
        # writer; for now the workload is single-writer.
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we'll BEGIN manually for batches
        )
        self._conn.row_factory = sqlite3.Row
        self._init_pragmas()
        self._init_schema()

    def _init_pragmas(self) -> None:
        # WAL gives us reader-during-writer concurrency. NORMAL sync is
        # the SD-card-friendly compromise.
        self._conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA synchronous=NORMAL;"
            "PRAGMA temp_store=MEMORY;"
            "PRAGMA mmap_size=8388608;"  # 8 MB; modest, plenty for our row count
        )

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            _log.exception("error closing message store")

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for code that wants to share it.

        Used by the OutboundQueue (Step 6) so we don't open a second
        SQLite handle to the same database file. SQLite WAL allows
        concurrent readers but only one writer; sharing the
        connection eliminates write contention.
        """
        return self._conn

    # ── Inserts ─────────────────────────────────────────────────────

    def insert_decode(self, parsed: ParsedFrame) -> int:
        """Persist a decoded frame. Returns the new row id."""
        d = parsed.decoded
        cur = self._conn.execute(
            "INSERT INTO decodes("
            " received_at, snr_db, frequency_hz, submode, quality,"
            " frame_type, kind, from_call, to_call, grid, body, raw, text"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.received_at, d.snr_db, d.frequency_hz, d.submode,
                d.quality, d.frame_type, parsed.kind.value,
                parsed.from_call, parsed.to_call, parsed.grid,
                parsed.body, d.raw, d.text,
            ),
        )
        return cur.lastrowid or 0

    def upsert_heard_station(self, station: HeardStation) -> None:
        """Insert or replace the most-recent-sighting row.

        We use INSERT ... ON CONFLICT DO UPDATE rather than DELETE+INSERT
        so the index stays warm and so concurrent readers see exactly
        one row per callsign at all times.
        """
        self._conn.execute(
            "INSERT INTO heard_stations("
            " callsign, last_heard, snr_db, grid, frequency_hz,"
            " distance_mi, bearing_deg"
            ") VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(callsign) DO UPDATE SET"
            "  last_heard=excluded.last_heard,"
            "  snr_db=excluded.snr_db,"
            "  grid=COALESCE(excluded.grid, heard_stations.grid),"
            "  frequency_hz=excluded.frequency_hz,"
            "  distance_mi=COALESCE(excluded.distance_mi, heard_stations.distance_mi),"
            "  bearing_deg=COALESCE(excluded.bearing_deg, heard_stations.bearing_deg)",
            (
                station.callsign, station.last_heard, station.snr_db,
                station.grid, station.frequency_hz,
                station.distance_mi, station.bearing_deg,
            ),
        )
        # Cap the heard table — drop the oldest if we exceed _HEARD_CAP.
        self._conn.execute(
            "DELETE FROM heard_stations WHERE callsign IN ("
            "  SELECT callsign FROM heard_stations"
            "  ORDER BY last_heard ASC"
            "  LIMIT MAX(0, (SELECT COUNT(*) FROM heard_stations) - ?)"
            ")",
            (_HEARD_CAP,),
        )

    # ── Queries ─────────────────────────────────────────────────────

    def heard_stations(self, limit: int = 50) -> List[HeardStation]:
        """Return up to ``limit`` heard stations, most recent first."""
        rows = self._conn.execute(
            "SELECT callsign, last_heard, snr_db, grid, frequency_hz,"
            " distance_mi, bearing_deg"
            " FROM heard_stations ORDER BY last_heard DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [HeardStation(**dict(r)) for r in rows]

    def recent_decodes(self, limit: int = 50, kind: Optional[str] = None
                       ) -> List[sqlite3.Row]:
        """Return most-recent decodes, optionally filtered by kind."""
        if kind is None:
            rows = self._conn.execute(
                "SELECT * FROM decodes ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM decodes WHERE kind=? ORDER BY received_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return list(rows)

    def directed_to_us(self, our_callsign: str, limit: int = 50
                       ) -> List[sqlite3.Row]:
        """Decodes addressed to our callsign, newest first.

        Used by the Directed screen.
        """
        rows = self._conn.execute(
            "SELECT * FROM decodes WHERE to_call=? "
            "ORDER BY received_at DESC LIMIT ?",
            (our_callsign.upper(), limit),
        ).fetchall()
        return list(rows)

    # ── Retention ───────────────────────────────────────────────────

    def prune_older_than(self, retain_days: int) -> int:
        """Delete decodes older than ``retain_days``. Returns the count."""
        cutoff = time.time() - retain_days * 86_400
        cur = self._conn.execute(
            "DELETE FROM decodes WHERE received_at < ?", (cutoff,)
        )
        return cur.rowcount or 0
