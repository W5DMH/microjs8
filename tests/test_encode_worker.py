"""Tests for microjs8.tx.encode_worker.

Covers EncodedAudioCache (thread-safe in-memory dict) and EncodeWorker
(background thread that encodes ENCODING-state rows).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from microjs8.modem.encoder import EncoderError
from microjs8.tx.encode_worker import (
    EncodedAudioCache,
    EncodeWorker,
)
from microjs8.tx.queue import (
    OutboundKind,
    OutboundQueue,
    OutboundState,
)


# ── EncodedAudioCache ───────────────────────────────────────────────


def test_cache_get_returns_none_for_missing():
    cache = EncodedAudioCache()
    assert cache.get(42) is None


def test_cache_put_then_get():
    cache = EncodedAudioCache()
    audio = [np.zeros(100, dtype=np.int16)]
    cache.put(42, audio)
    got = cache.get(42)
    assert got is not None
    np.testing.assert_array_equal(got[0], audio[0])


def test_cache_get_does_not_remove():
    """Retry path needs to read the same audio multiple times. The
    cache is peek, not pop — only ``discard()`` removes."""
    cache = EncodedAudioCache()
    audio = [np.zeros(100, dtype=np.int16)]
    cache.put(42, audio)
    assert cache.get(42) is not None
    assert cache.get(42) is not None  # still there


def test_cache_discard_removes():
    cache = EncodedAudioCache()
    cache.put(42, [np.zeros(10, dtype=np.int16)])
    cache.discard(42)
    assert cache.get(42) is None


def test_cache_discard_idempotent():
    """Calling discard for an unseen id is fine."""
    cache = EncodedAudioCache()
    cache.discard(999)  # never put — must not raise
    cache.put(42, [np.zeros(10, dtype=np.int16)])
    cache.discard(42)
    cache.discard(42)  # second discard


def test_cache_has():
    cache = EncodedAudioCache()
    assert cache.has(42) is False
    cache.put(42, [np.zeros(10, dtype=np.int16)])
    assert cache.has(42) is True
    cache.discard(42)
    assert cache.has(42) is False


def test_cache_size():
    cache = EncodedAudioCache()
    assert cache.size() == 0
    cache.put(1, [np.zeros(10, dtype=np.int16)])
    cache.put(2, [np.zeros(10, dtype=np.int16)])
    assert cache.size() == 2
    cache.discard(1)
    assert cache.size() == 1


def test_cache_thread_safety():
    """Concurrent put/get/discard from multiple threads should not
    crash. Doesn't prove correctness under concurrency, but does
    ensure the lock is wired."""
    cache = EncodedAudioCache()
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            cache.put(i % 100, [np.zeros(10, dtype=np.int16)])
            i += 1

    def reader():
        i = 0
        while not stop.is_set():
            cache.get(i % 100)
            i += 1

    def discarder():
        i = 0
        while not stop.is_set():
            cache.discard(i % 100)
            i += 1

    threads = [
        threading.Thread(target=fn) for fn in (writer, reader, discarder)
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(timeout=1.0)
        assert not t.is_alive()


# ── EncodeWorker fixtures ───────────────────────────────────────────


@pytest.fixture
def conn(tmp_path: Path):
    """sqlite3 connection on a temp db."""
    db = sqlite3.connect(
        str(tmp_path / "msg.db"),
        check_same_thread=False,
        isolation_level=None,
    )
    db.row_factory = sqlite3.Row
    db.executescript(
        "PRAGMA journal_mode=WAL;"
        "PRAGMA synchronous=NORMAL;"
    )
    yield db
    db.close()


@pytest.fixture
def queue(conn):
    return OutboundQueue(conn)


class _FakeBackend:
    """Fake TxBackend with controllable encode behavior.

    encode() returns ``encoded_audio`` (list of arrays). If
    ``raise_on_encode`` is set, encode() raises that instead.
    Records every call in ``calls``.
    """

    def __init__(self):
        self.calls: list[str] = []
        self.encoded_audio: "list[np.ndarray]" = [
            np.full(100, 7, dtype=np.int16)
        ]
        self.raise_on_encode: "Exception | None" = None
        self.encode_delay_s: float = 0.0  # simulate encoder cost

    def encode(self, text: str) -> "list[np.ndarray]":
        self.calls.append(text)
        if self.encode_delay_s > 0:
            time.sleep(self.encode_delay_s)
        if self.raise_on_encode is not None:
            raise self.raise_on_encode
        return list(self.encoded_audio)


# ── EncodeWorker behavior ───────────────────────────────────────────


def test_worker_encodes_pending_row(queue):
    """A row enqueued for encoding should transition ENCODING → QUEUED
    and have its audio cached after the worker runs."""
    cache = EncodedAudioCache()
    backend = _FakeBackend()
    worker = EncodeWorker(queue, backend, cache)

    msg_id = queue.enqueue_for_encoding("HELLO", OutboundKind.ALLCALL)
    assert msg_id is not None
    assert queue.get(msg_id).state is OutboundState.ENCODING

    worker.start()
    try:
        # Wait for the worker to process the row.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if queue.get(msg_id).state is OutboundState.QUEUED:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    final = queue.get(msg_id)
    assert final.state is OutboundState.QUEUED
    assert cache.has(msg_id)
    assert backend.calls == ["HELLO"]
    assert worker.encoded_count() == 1
    assert worker.failed_count() == 0


def test_worker_handles_encoder_error(queue):
    """EncoderError → row marked ABANDONED, no cache entry."""
    cache = EncodedAudioCache()
    backend = _FakeBackend()
    backend.raise_on_encode = EncoderError("test failure")
    worker = EncodeWorker(queue, backend, cache)

    msg_id = queue.enqueue_for_encoding("BAD", OutboundKind.ALLCALL)
    assert msg_id is not None

    worker.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if queue.get(msg_id).state is OutboundState.ABANDONED:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    final = queue.get(msg_id)
    assert final.state is OutboundState.ABANDONED
    assert "encode failed" in (final.error or "")
    assert not cache.has(msg_id)
    assert worker.failed_count() == 1


def test_worker_handles_unexpected_exception(queue):
    """Non-EncoderError exception in encode() also marks the row
    ABANDONED but logs as 'unexpected'. The worker thread keeps
    running for any further rows."""
    cache = EncodedAudioCache()
    backend = _FakeBackend()
    backend.raise_on_encode = RuntimeError("kaboom")
    worker = EncodeWorker(queue, backend, cache)

    msg_id = queue.enqueue_for_encoding("BAD", OutboundKind.ALLCALL)
    assert msg_id is not None

    worker.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if queue.get(msg_id).state is OutboundState.ABANDONED:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    assert queue.get(msg_id).state is OutboundState.ABANDONED
    assert worker.failed_count() == 1


def test_worker_processes_multiple_rows_in_order(queue):
    """Multiple ENCODING rows are processed FIFO."""
    cache = EncodedAudioCache()
    backend = _FakeBackend()
    worker = EncodeWorker(queue, backend, cache)

    ids = [
        queue.enqueue_for_encoding(f"MSG{i}", OutboundKind.ALLCALL)
        for i in range(3)
    ]
    assert all(i is not None for i in ids)

    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            states = [queue.get(i).state for i in ids]
            if all(s is OutboundState.QUEUED for s in states):
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    assert worker.encoded_count() == 3
    # All rows should have cached audio.
    for msg_id in ids:
        assert cache.has(msg_id)
    # FIFO order — backend was called in enqueue order.
    assert backend.calls == ["MSG0", "MSG1", "MSG2"]


def test_worker_idle_when_no_rows(queue):
    """Worker should sit idle when queue has nothing in ENCODING."""
    cache = EncodedAudioCache()
    backend = _FakeBackend()
    worker = EncodeWorker(queue, backend, cache)

    worker.start()
    try:
        time.sleep(0.3)  # several poll intervals
    finally:
        worker.stop()

    assert backend.calls == []
    assert worker.encoded_count() == 0


def test_worker_picks_up_existing_encoding_rows_on_start(queue):
    """If there's already an ENCODING row when the worker starts
    (e.g., daemon restart), the worker processes it on its first
    poll iteration."""
    cache = EncodedAudioCache()
    backend = _FakeBackend()

    # Pre-populate a row, simulating restart-recovery.
    msg_id = queue.enqueue_for_encoding("RESTART", OutboundKind.ALLCALL)

    worker = EncodeWorker(queue, backend, cache)
    worker.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if queue.get(msg_id).state is OutboundState.QUEUED:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    assert queue.get(msg_id).state is OutboundState.QUEUED
    assert cache.has(msg_id)


def test_worker_stop_idempotent(queue):
    cache = EncodedAudioCache()
    backend = _FakeBackend()
    worker = EncodeWorker(queue, backend, cache)
    worker.start()
    worker.stop()
    worker.stop()  # second call must not crash


def test_worker_start_idempotent(queue):
    """Calling start() twice should not spawn a second thread."""
    cache = EncodedAudioCache()
    backend = _FakeBackend()
    worker = EncodeWorker(queue, backend, cache)
    worker.start()
    initial_thread = worker._thread
    worker.start()
    assert worker._thread is initial_thread  # same thread, not re-spawned
    worker.stop()


def test_worker_caches_before_marking_queued(queue):
    """Race condition guarantee: cache.put() runs BEFORE
    queue.mark_encoded(). If the worker crashed between put() and
    mark_encoded(), the row would still be in ENCODING (re-encoded
    on restart), but if mark_encoded() ran first the scheduler
    could pick up a QUEUED row with no cached audio.

    Verify this by interposing on cache.put — confirm the row is
    still ENCODING at the moment put() is called.
    """
    backend = _FakeBackend()
    state_at_put: list[OutboundState] = []

    class _ObservingCache(EncodedAudioCache):
        def put(self, msg_id, audio_frames):
            state_at_put.append(queue.get(msg_id).state)
            super().put(msg_id, audio_frames)

    cache = _ObservingCache()
    worker = EncodeWorker(queue, backend, cache)

    msg_id = queue.enqueue_for_encoding("HELLO", OutboundKind.ALLCALL)
    worker.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if queue.get(msg_id).state is OutboundState.QUEUED:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    # When put() ran, the row should still have been ENCODING.
    assert state_at_put == [OutboundState.ENCODING]
