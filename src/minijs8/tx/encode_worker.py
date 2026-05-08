"""Encode worker — renders TX audio off the slot-aligned hot path.

The gfsk8 modulator + 12kHz→48kHz polyphase resample takes ~1 second
PER FRAME on a Pi Zero 2W. A typical 3-frame message is therefore
~3 seconds of CPU work. Doing that work synchronously when the
scheduler picks up a QUEUED message blows the slot-alignment budget:
the scheduler wakes at slot+10 ms, encodes for 3 s, then transmits
frame 1 — which lands at slot+3000 ms (the JS8Call round-up formula
shifts to the slot+3500ms boundary). Reference-station decoders see
that as a misaligned (often un-decodable) frame.

This module moves the encode work to a dedicated background thread
that wakes whenever a row is in ENCODING state. Audio is cached in
memory keyed by message id. The scheduler later reads from the
cache instead of encoding inline:

  enqueue(text)                                       # ENCODING state
  EncodeWorker picks it up, encodes, caches audio,    # → QUEUED state
    transitions to QUEUED
  Scheduler picks the QUEUED row at slot boundary,
    reads pre-encoded audio from cache (microseconds), # → SENDING state
    starts the burst at slot+12 ms

In-memory cache only (per design — no on-disk persistence). After a
daemon restart, the cache is empty and any rows in QUEUED or ENCODING
state get reset to ENCODING by the queue's ``reset_unencoded_to_encoding()``
method, called once at startup. The worker re-encodes on its next
iteration.

Why no on-disk persistence:
  * Simplicity: no schema migration, no orphan-file GC, no cross-
    reference between filesystem and queue table
  * Crash safety is "good enough": worst case, an in-flight message
    waits ~3 s after restart before its audio is ready again — the
    operator perceives this as the daemon coming up slowly, no data
    is lost
  * Memory cost: ~3.6 MB per 3-frame message at 48kHz (3 × 606720
    samples × 2 bytes). With QUEUE_DEPTH=10 this caps at ~36 MB —
    fine on a Pi Zero 2W with 512 MB RAM

Cache lifecycle and retry behavior:
  * cache.put(id, frames)         — worker stores after successful encode
  * cache.get(id)                 — scheduler READS, audio remains in
                                   cache for retry attempts
  * cache.discard(id)             — caller explicitly removes when row
                                   reaches a terminal state (DELIVERED,
                                   ABANDONED, EXPIRED). Scheduler calls
                                   this in mark_delivered / fail_message.

Thread safety: lock-protected dict, simple put/get/discard. No
condition variable or queue — the queue table itself is the
synchronization point (worker polls for ENCODING rows). Polling
interval is 250 ms which is fast enough for "operator-perceives-
immediate" but light enough not to wake the Pi Zero 2W's CPU
unnecessarily.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import numpy as np

    from minijs8.tx.queue import OutboundQueue
    from minijs8.tx.tx_backend import TxBackend

from minijs8.modem.encoder import EncoderError

_log = logging.getLogger(__name__)

# How often the worker polls for new ENCODING rows. 250 ms balances
# responsiveness ("operator hits enter, audio starts encoding within
# a quarter second") against CPU wake-ups on the Pi Zero 2W.
_POLL_INTERVAL_S = 0.25


class EncodedAudioCache:
    """Thread-safe in-memory cache of pre-encoded audio per message id.

    The worker calls ``put()`` after a successful encode, the
    scheduler calls ``get()`` at TX time, and the scheduler calls
    ``discard()`` when the message reaches a terminal state.

    Each cache entry is a ``list[np.ndarray]`` — one int16 buffer
    per frame, at 48 kHz, with the protocol's 500 ms silence prefix
    intact. Same shape the encoder.encode() previously returned
    inline.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: "dict[int, list[np.ndarray]]" = {}

    def put(self, message_id: int, audio_frames: "list[np.ndarray]") -> None:
        """Store the encoded audio for a message. Overwrites any prior
        value for the same id (which shouldn't happen in normal flow
        but is harmless if it does)."""
        with self._lock:
            self._cache[message_id] = audio_frames

    def get(self, message_id: int) -> "Optional[list[np.ndarray]]":
        """Return the cached audio for a message, or None if not
        cached. Does NOT remove the entry — retry attempts can read
        the same audio multiple times. Caller must call ``discard()``
        when the message reaches a terminal state."""
        with self._lock:
            return self._cache.get(message_id)

    def discard(self, message_id: int) -> None:
        """Remove the cached audio for a message. Idempotent — calling
        twice is fine. Called when the message reaches DELIVERED,
        ABANDONED, or EXPIRED."""
        with self._lock:
            self._cache.pop(message_id, None)

    def has(self, message_id: int) -> bool:
        """Whether the cache has an entry for ``message_id``."""
        with self._lock:
            return message_id in self._cache

    def size(self) -> int:
        """Number of cached entries (for diagnostics)."""
        with self._lock:
            return len(self._cache)


class EncodeWorker:
    """Background thread that encodes ENCODING-state messages.

    Lifecycle:
      worker = EncodeWorker(queue, backend, cache)
      worker.start()  # spawns thread, returns immediately
      ... worker runs forever, processing ENCODING rows ...
      worker.stop()   # signals thread to exit, joins (timeout 5 s)

    The worker thread:
      1. Polls the queue every _POLL_INTERVAL_S for an ENCODING row
      2. If found, calls ``backend.encode(msg.text)`` (the slow path,
         ~3 s for a 3-frame message)
      3. On success: cache.put(audio), queue.mark_encoded() → QUEUED
      4. On EncoderError: queue.mark_abandoned() (permanent failure)
      5. Loops

    Any exception that escapes the encoder is treated as a permanent
    failure for that row (mark_abandoned with error="encode failed").
    The worker thread itself never dies — it logs the exception and
    keeps polling.
    """

    def __init__(
        self,
        queue: "OutboundQueue",
        backend: "TxBackend",
        cache: EncodedAudioCache,
    ) -> None:
        self._queue = queue
        self._backend = backend
        self._cache = cache
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Diagnostic counters — used by tests + stats endpoint.
        self._encoded_count = 0
        self._failed_count = 0

    def start(self) -> None:
        """Spawn the worker thread. Idempotent."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="minijs8-encode-worker",
            daemon=True,
        )
        self._thread.start()
        _log.info("encode worker started")

    def stop(self) -> None:
        """Signal the worker to stop. Waits up to 5 s for the thread.

        A long-running encode in progress will finish before the
        thread observes the stop event — the encoder is not
        cancellable. 5 s timeout covers normal encode (~3 s) plus
        margin.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                _log.warning(
                    "encode worker did not exit within 5 s; "
                    "may still be encoding"
                )
            self._thread = None
        _log.info("encode worker stopped")

    def encoded_count(self) -> int:
        """Total successful encodes since start (for diagnostics)."""
        return self._encoded_count

    def failed_count(self) -> int:
        """Total encode failures since start (for diagnostics)."""
        return self._failed_count

    # ── Internal: worker loop ───────────────────────────────────────

    def _loop(self) -> None:
        """Pick up ENCODING rows; encode; cache; mark QUEUED.

        Runs until ``_stop_event`` is set. Exceptions are caught and
        logged so the thread never dies — a corrupt row should not
        bring down the whole encoder.
        """
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                _log.exception(
                    "encode worker tick raised; continuing"
                )
            # Wait either the poll interval, or until stop is signaled,
            # whichever comes first.
            self._stop_event.wait(timeout=_POLL_INTERVAL_S)

    def _tick(self) -> None:
        """Process one row, if any are pending. Returns immediately
        if there's no work."""
        msg = self._queue.pick_next_encoding()
        if msg is None:
            return

        msg_id = msg.id
        text = msg.text
        _log.info(
            "encode worker: encoding msg id=%d (%s)", msg_id, text,
        )
        encode_start = time.monotonic()

        try:
            audio_frames = self._backend.encode(text)
        except EncoderError as exc:
            self._failed_count += 1
            _log.error(
                "encode failed permanently for msg id=%d: %s", msg_id, exc,
            )
            self._queue.mark_abandoned(
                msg_id, error=f"encode failed: {exc}",
            )
            return
        except Exception as exc:
            # Unexpected exception — treat as a permanent failure but
            # surface loudly. The worker thread keeps running.
            self._failed_count += 1
            _log.exception(
                "encode raised unexpected exception for msg id=%d", msg_id,
            )
            self._queue.mark_abandoned(
                msg_id, error=f"encode failed: {exc}",
            )
            return

        # Success. Cache the audio FIRST, then mark QUEUED.
        # Order matters: if we transitioned to QUEUED first and then
        # crashed before put(), the scheduler could pick up a
        # QUEUED row with no cached audio. The cache.put() →
        # queue.mark_encoded() order means a crash between leaves
        # the row in ENCODING and it gets re-encoded on restart —
        # wasted work but no incorrect TX.
        self._cache.put(msg_id, audio_frames)
        self._queue.mark_encoded(msg_id)
        self._encoded_count += 1
        encode_elapsed = time.monotonic() - encode_start
        _log.info(
            "encode worker: msg id=%d ready (%d frame(s), %.2f s encode)",
            msg_id, len(audio_frames), encode_elapsed,
        )
