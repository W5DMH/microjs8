"""In-memory log of directed protocol activity.

Tracks the recent back-and-forth between our station and other stations
for protocol-level (non-mail) directed exchanges — SNR?, INFO, GRID?,
QUERY MSGS, QUERY MSG <id>, ACK, etc. The DIRECTED screen renders this
as a chat-style chronological view so the operator can see the full
round-trip of each query.

Why in-memory and not persisted
================================

The decision (this session, with the operator) was to use a bounded
in-memory ring buffer rather than a SQLite table. Reasoning:

  - Directed traffic is high-volume and ephemeral; the operator cares
    about "what's been happening lately", not "what happened last
    week." Three days from now nobody needs to know that KC1WDO asked
    for our SNR.
  - Persistence buys us nothing on a daemon-restart timeline (you'd
    typically be restarting because something broke; you don't need
    pre-restart chat history to debug).
  - A SQLite schema is more code, more tests, more migrations, more
    surface for bugs. A deque is 80 lines and trivially correct.

If we ever want history retention, swap the storage backend without
changing the call sites — the API surface here is intentionally small.

Bounds
======

Max 200 entries. At a typical ~5 directed exchanges per active hour
that's ~40 hours of working memory. Tunable via the constructor for
tests; production constructs it with the default.

Concurrency
===========

The decode handler runs on the asyncio loop thread, but TX logging
fires from the same thread that calls ``_enqueue_directed_reply``
(also asyncio loop). So in practice we're single-threaded. We lock
defensively anyway because the cost is negligible (~100 ns per call)
and the failure mode without it (snapshot reading mid-append) would
be a heisenbug we'd never reproduce.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# Default cap. Sized so the deque uses well under 100 KB even with
# long bodies — fine on a Pi Zero 2W's 512 MB.
DEFAULT_MAX_ENTRIES = 200


class Direction(str, Enum):
    """Whether the entry is something we received or something we sent."""

    IN = "IN"     # received from a remote station, addressed to us
    OUT = "OUT"   # we transmitted, addressed to a remote station


@dataclass(frozen=True)
class DirectedActivityEntry:
    """One row in the directed-activity log.

    Frozen so consumers can't mutate snapshot rows. The renderer reads
    ``at_unix`` to format a HH:MM timestamp; ``other_call`` is always
    the OTHER station (their from_call on inbound, our to_call on
    outbound) so the chat view consistently shows "who we're talking
    to" regardless of direction.

    ``snr_db`` and ``freq_hz`` are populated for inbound entries from
    the decoded frame; for outbound they're None (we didn't measure
    anything — we just sent). The renderer should branch on direction
    to decide which metadata to show.
    """

    at_unix: float                  # time.time() when recorded
    direction: Direction
    other_call: str                 # callsign of the OTHER party
    verb: str                       # the protocol verb / first token (uppercase, e.g. "ACK", "SNR?", "QUERY MSGS")
    body: str                       # remaining text after the verb (may be empty)
    snr_db: Optional[int] = None    # inbound only
    freq_hz: Optional[float] = None # inbound only


class DirectedActivityLog:
    """Bounded thread-safe ring buffer of directed-activity entries.

    Newest entry is at the END of the snapshot tuple. Iterate in
    reverse for newest-first rendering (matches the operator's
    natural reading order on a phone-style chat screen).
    """

    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._buf: deque[DirectedActivityEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        # Expose the cap so tests can verify the deque actually
        # truncates at the configured boundary without reading
        # private attributes.
        self._max_entries = max_entries

    # ── Public API ───────────────────────────────────────────────────

    def record_in(
        self,
        *,
        from_call: str,
        verb: str,
        body: str = "",
        snr_db: Optional[int] = None,
        freq_hz: Optional[float] = None,
        at_unix: Optional[float] = None,
    ) -> DirectedActivityEntry:
        """Record an inbound directed frame (from a remote station to us).

        Returns the entry that was appended (caller can ignore the
        return; it's there mainly for tests).

        ``at_unix=None`` (the default) timestamps with ``time.time()``;
        tests pass a fixed value for determinism.
        """
        entry = DirectedActivityEntry(
            at_unix=at_unix if at_unix is not None else time.time(),
            direction=Direction.IN,
            other_call=from_call.upper() if from_call else "",
            verb=verb.upper() if verb else "",
            body=body,
            snr_db=snr_db,
            freq_hz=freq_hz,
        )
        with self._lock:
            self._buf.append(entry)
        return entry

    def record_out(
        self,
        *,
        to_call: str,
        verb: str,
        body: str = "",
        at_unix: Optional[float] = None,
    ) -> DirectedActivityEntry:
        """Record an outbound directed frame (us → remote station).

        We don't record SNR/freq for outbound entries because they're
        meaningless on the TX side (we'd just be quoting our own
        carrier frequency, which the operator already knows from the
        radio).
        """
        entry = DirectedActivityEntry(
            at_unix=at_unix if at_unix is not None else time.time(),
            direction=Direction.OUT,
            other_call=to_call.upper() if to_call else "",
            verb=verb.upper() if verb else "",
            body=body,
            snr_db=None,
            freq_hz=None,
        )
        with self._lock:
            self._buf.append(entry)
        return entry

    def snapshot(self) -> tuple[DirectedActivityEntry, ...]:
        """Return an immutable point-in-time snapshot.

        Snapshots are independent of the live buffer — appending to
        the log after taking a snapshot does NOT change the snapshot.
        Used by the UI thread to render the screen without holding
        the lock while drawing.
        """
        with self._lock:
            return tuple(self._buf)

    def clear(self) -> None:
        """Drop all entries.

        Used during operator-initiated reset (e.g. callsign change in
        Setup) so the new operator doesn't inherit the old operator's
        chat history.
        """
        with self._lock:
            self._buf.clear()

    @property
    def max_entries(self) -> int:
        """Configured cap — useful for assertions in tests."""
        return self._max_entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)
