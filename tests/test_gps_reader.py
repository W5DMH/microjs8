"""Tests for minijs8.gps.reader.GpsReader.

Like the keyboard tests, we don't connect to a real gpsd. We inject a
fake client whose stream() yields scripted fixes, so we can validate
the thread's reconnect logic and stop semantics in isolation.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Iterator, Optional

import pytest

from minijs8.gps.gpsd_client import GpsdClient
from minijs8.gps.reader import GpsReader
from minijs8.gps.types import FixKind, GpsFix


def _make_fix(kind: FixKind, lat: float = 42.0, lon: float = -83.0) -> GpsFix:
    return GpsFix(
        kind=kind, lat=lat, lon=lon, altitude_m=None,
        speed_mps=None, track_deg=None, hdop=None,
        fix_time=None, satellites_used=None,
        received_at=time.monotonic(),
    )


class _FakeClient:
    """Implements the GpsdClient surface we depend on."""

    def __init__(self, fixes_per_session: list[list[GpsFix]]) -> None:
        # A list of sessions; each session is a list of fixes to
        # produce before the connection "drops".
        self._sessions = list(fixes_per_session)
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if not self._sessions:
            # No more sessions — simulate gpsd not available.
            raise OSError("connection refused")

    def close(self) -> None:
        self.close_calls += 1

    def stream(self, stop_event) -> Iterator[GpsFix]:
        if not self._sessions:
            return
        session = self._sessions.pop(0)
        for fix in session:
            if stop_event.is_set():
                return
            yield fix


def _drive_reader(
    client: _FakeClient,
    *,
    timeout_s: float = 1.0,
) -> list[GpsFix]:
    """Run a GpsReader against the fake client; capture emitted fixes."""
    captured: list[GpsFix] = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def on_fix(fix: GpsFix) -> None:
        captured.append(fix)

    reader = GpsReader(loop, on_fix, client_factory=lambda: client)
    reader.start()

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        loop.call_soon(loop.stop)
        loop.run_forever()
        time.sleep(0.05)

    reader.stop()
    reader.join(timeout=2.0)
    assert not reader.is_alive(), "GpsReader did not stop within 2s"
    loop.close()
    return captured


# ── Streaming ───────────────────────────────────────────────────────


def test_emits_fixes_from_one_session():
    fixes = [
        _make_fix(FixKind.NO_FIX),
        _make_fix(FixKind.FIX_2D),
        _make_fix(FixKind.FIX_3D),
    ]
    client = _FakeClient([fixes])
    captured = _drive_reader(client)
    # Note: when the session exhausts, the reader emits a synthetic
    # NO_FIX. So we expect at least 3 + 1 = 4.
    kinds = [f.kind for f in captured]
    # First three fixes (in order) plus the synthetic NO_FIX after disconnect.
    assert kinds[:3] == [FixKind.NO_FIX, FixKind.FIX_2D, FixKind.FIX_3D]
    assert FixKind.NO_FIX in kinds[3:]


def test_emits_synthetic_no_fix_on_disconnect():
    """When a session ends, the reader must emit a synthetic NO_FIX
    so the UI shows 'GPS lost' instead of holding the last fix."""
    fixes = [_make_fix(FixKind.FIX_3D)]
    client = _FakeClient([fixes])  # only one session
    captured = _drive_reader(client, timeout_s=0.5)
    kinds = [f.kind for f in captured]
    # First a 3D fix, then synthetic NO_FIX.
    assert FixKind.FIX_3D in kinds
    no_fix_idx = next(i for i, k in enumerate(kinds) if k == FixKind.NO_FIX)
    fix_idx = next(i for i, k in enumerate(kinds) if k == FixKind.FIX_3D)
    assert no_fix_idx > fix_idx, "synthetic NO_FIX should follow the 3D fix"


def test_reconnects_across_multiple_sessions():
    """Two sessions back-to-back: the reader must reconnect."""
    s1 = [_make_fix(FixKind.FIX_3D, lat=42.0, lon=-83.0)]
    s2 = [_make_fix(FixKind.FIX_3D, lat=42.5, lon=-83.5)]
    client = _FakeClient([s1, s2])
    captured = _drive_reader(client, timeout_s=3.0)
    # Two real fixes plus possibly synthetic NO_FIX between them.
    real = [f for f in captured if f.kind == FixKind.FIX_3D]
    assert len(real) == 2
    assert real[0].lat == 42.0
    assert real[1].lat == 42.5
    assert client.connect_calls >= 2


# ── Lifecycle ───────────────────────────────────────────────────────


def test_stop_during_connect_loop_when_gpsd_down():
    """If gpsd is unreachable, the reader sleeps in the reconnect loop.
    stop() must wake it within ~2 s (the reconnect delay)."""
    captured: list[GpsFix] = []
    loop = asyncio.new_event_loop()

    def factory():
        c = GpsdClient()
        # Replace connect() with a method that always fails.
        def bad_connect():
            raise OSError("connection refused")
        c.connect = bad_connect  # type: ignore[method-assign]
        return c

    reader = GpsReader(loop, captured.append, client_factory=factory)
    reader.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    reader.stop()
    reader.join(timeout=3.0)
    elapsed = time.monotonic() - t0
    assert not reader.is_alive(), "GpsReader did not stop within 3s"
    assert elapsed < 3.0, f"stop() took too long: {elapsed:.2f}s"
    loop.close()


def test_stop_during_streaming():
    """stop() during an active stream must end promptly."""
    # Long stream so we're definitely in the middle of it when stop fires.
    fixes = [_make_fix(FixKind.FIX_3D)] * 1000
    client = _FakeClient([fixes])
    captured: list[GpsFix] = []
    loop = asyncio.new_event_loop()
    reader = GpsReader(loop, captured.append, client_factory=lambda: client)
    reader.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    reader.stop()
    reader.join(timeout=2.0)
    elapsed = time.monotonic() - t0
    assert not reader.is_alive()
    assert elapsed < 2.0
    loop.close()
