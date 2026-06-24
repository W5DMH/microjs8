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

Max 2000 entries (raised from 200 in v0.0.19 per operator request:
DIRECTED rows should stay visible until the session ends rather than
being silently FIFO'd off the bottom of the list during busy decode
periods). At a typical ~5 directed exchanges per active hour that's
~400 hours of working memory -- effectively "the whole session" for
any normal deployment. Memory cost is bounded (~400 KB at the cap
with average body lengths). Tunable via the constructor for tests.

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


# Default cap. v0.0.19: raised from 200 to 2000 per operator request
# ("DIRECTED rows should remain visible until the session ends").
# A bounded ring buffer is still the right shape -- a runaway decode
# loop should not OOM the daemon -- but 2000 is high enough that a
# normal full-day session never hits the bound. At max it uses well
# under 1 MB even with long bodies, fine on a Pi Zero 2W's 512 MB.
DEFAULT_MAX_ENTRIES = 2000


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
    # When this inbound entry was a frame addressed to a JS8Call
    # group we belong to (e.g. ``K1ABC: @ARESGA QSL?``), ``for_group``
    # holds the group name including the leading '@'. None for
    # personally-directed traffic. The DIRECTED screen renders
    # ``K1ABC@@ARESGA`` instead of just ``K1ABC`` when this is set,
    # so the operator immediately sees that the message was a
    # group blast rather than personal traffic.
    for_group: Optional[str] = None


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
        for_group: Optional[str] = None,
    ) -> DirectedActivityEntry:
        """Record an inbound directed frame (from a remote station to us).

        Returns the entry that was appended (caller can ignore the
        return; it's there mainly for tests).

        ``at_unix=None`` (the default) timestamps with ``time.time()``;
        tests pass a fixed value for determinism.

        ``for_group`` is set when the frame was addressed to a JS8Call
        group we belong to rather than to our callsign directly. The
        DIRECTED renderer uses this to show ``from@@group`` so the
        operator distinguishes group blasts from personal messages.
        """
        entry = DirectedActivityEntry(
            at_unix=at_unix if at_unix is not None else time.time(),
            direction=Direction.IN,
            other_call=from_call.upper() if from_call else "",
            verb=verb.upper() if verb else "",
            body=body,
            snr_db=snr_db,
            freq_hz=freq_hz,
            for_group=for_group.upper() if for_group else None,
        )
        with self._lock:
            self._buf.append(entry)
        return entry

    def record_in_supersede(
        self,
        *,
        from_call: str,
        verb: str,
        body: str = "",
        snr_db: Optional[int] = None,
        freq_hz: Optional[float] = None,
        at_unix: Optional[float] = None,
        recency_s: float = 60.0,
        for_group: Optional[str] = None,
    ) -> DirectedActivityEntry:
        """Record an inbound entry, REPLACING a prior matching entry
        when the new body extends it.

        Use this from the assembler's multi-frame emit path when the
        same wire message produced an earlier single-frame
        dispatch — instead of appending a duplicate-with-extension
        row, we replace the original entry in place so the operator
        sees ONE row that gets richer as continuations arrive.

        Match rule (must all hold for replacement):
          - prior entry is INBOUND from the same ``from_call``
          - prior entry's ``verb`` matches the new ``verb``
          - prior entry's ``at_unix`` is within ``recency_s`` seconds
            of the new entry (default 60 s — comfortably longer than
            the non-buffered reassembly timeout of 40 s)
          - the NEW ``body`` STARTS WITH (or equals) the prior body
            — i.e., the new body extends the old without contradicting

        Metadata preservation:
          - SNR and freq_hz from the new call override only when
            non-None. The reassembled emit doesn't carry SNR (frame-
            level metadata is lost during assembly), so it passes
            ``snr_db=None``; this clause keeps the SNR from the
            original single-frame dispatch.

        If no matching prior entry is found, behaves like
        ``record_in`` and returns the appended new entry.
        """
        now = at_unix if at_unix is not None else time.time()
        new_call = from_call.upper() if from_call else ""
        new_verb = verb.upper() if verb else ""
        new_group = for_group.upper() if for_group else None
        with self._lock:
            # Scan backwards through the ring buffer. Stop early when
            # entries fall outside the recency window — older entries
            # can't be a candidate match.
            for i in range(len(self._buf) - 1, -1, -1):
                e = self._buf[i]
                if now - e.at_unix > recency_s:
                    break
                if e.direction is not Direction.IN:
                    continue
                if e.other_call != new_call:
                    continue
                if e.verb != new_verb:
                    continue
                if not body.startswith(e.body):
                    continue
                replacement = DirectedActivityEntry(
                    at_unix=now,
                    direction=Direction.IN,
                    other_call=new_call,
                    verb=new_verb,
                    body=body,
                    snr_db=snr_db if snr_db is not None else e.snr_db,
                    freq_hz=freq_hz if freq_hz is not None else e.freq_hz,
                    for_group=new_group if new_group is not None else e.for_group,
                )
                self._buf[i] = replacement
                return replacement

            # No match — fall back to append-as-new.
            entry = DirectedActivityEntry(
                at_unix=now,
                direction=Direction.IN,
                other_call=new_call,
                verb=new_verb,
                body=body,
                snr_db=snr_db,
                freq_hz=freq_hz,
                for_group=new_group,
            )
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
