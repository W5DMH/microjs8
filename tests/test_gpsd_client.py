"""Tests for microjs8.gps.gpsd_client.

We don't need a real gpsd. We construct a GpsdClient, swap its
``_sock`` attribute for a fake socket that replays a scripted byte
stream, and assert on the GpsFix records produced.
"""

from __future__ import annotations

import socket
import threading
from typing import Iterator

import pytest

from microjs8.gps.gpsd_client import GpsdClient
from microjs8.gps.types import FixKind


class _FakeSocket:
    """A socket-like whose recv() yields scripted byte chunks.

    The chunks are returned in order; once exhausted, recv() returns
    b'' (EOF), which the client treats as a clean disconnect.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def recv(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) <= n:
            return chunk
        # Split if the test chunk is bigger than the requested size.
        self._chunks.insert(0, chunk[n:])
        return chunk[:n]

    def close(self) -> None:
        pass


def _make_client_with_chunks(chunks: list[bytes]) -> GpsdClient:
    client = GpsdClient()
    client._sock = _FakeSocket(chunks)  # type: ignore[assignment]
    return client


def _drain(client: GpsdClient) -> list:
    """Stream until EOF, return all yielded fixes."""
    stop = threading.Event()
    return list(client.stream(stop))


# ── TPV decoding ────────────────────────────────────────────────────


def test_tpv_3d_fix_decoded():
    chunks = [
        b'{"class":"TPV","mode":3,"lat":42.3314,"lon":-83.0458,"altMSL":190.5,"time":"2026-04-28T18:00:00.000Z"}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    assert len(fixes) == 1
    f = fixes[0]
    assert f.kind == FixKind.FIX_3D
    assert f.lat == pytest.approx(42.3314)
    assert f.lon == pytest.approx(-83.0458)
    assert f.altitude_m == pytest.approx(190.5)
    assert f.has_position
    assert f.fix_time is not None
    # 2026-04-28T18:00:00Z = 1777399200.0 in Unix epoch seconds
    assert f.fix_time == pytest.approx(1777399200.0, abs=1.0)


def test_tpv_no_fix_decoded():
    chunks = [
        b'{"class":"TPV","mode":1}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    assert len(fixes) == 1
    assert fixes[0].kind == FixKind.NO_FIX
    assert not fixes[0].has_position


def test_tpv_2d_fix_decoded():
    chunks = [
        b'{"class":"TPV","mode":2,"lat":42.0,"lon":-83.0}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    assert len(fixes) == 1
    assert fixes[0].kind == FixKind.FIX_2D
    assert fixes[0].has_position


# ── SKY (satellite count) ───────────────────────────────────────────


def test_sky_updates_satellites_used():
    """A SKY message before a TPV must populate sat count on subsequent fixes."""
    chunks = [
        # 4 satellites total, 3 used.
        b'{"class":"SKY","satellites":[{"used":true},{"used":true},{"used":true},{"used":false}]}\n',
        b'{"class":"TPV","mode":3,"lat":42.0,"lon":-83.0}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    # SKY doesn't yield a fix; only TPV does.
    assert len(fixes) == 1
    assert fixes[0].satellites_used == 3


# ── Robustness ──────────────────────────────────────────────────────


def test_split_lines_across_chunks():
    """A JSON line split across two recv()s must still be parsed."""
    chunks = [
        b'{"class":"TPV","mode":3,',
        b'"lat":42.0,"lon":-83.0}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    assert len(fixes) == 1
    assert fixes[0].lat == 42.0


def test_multiple_lines_in_one_chunk():
    chunks = [
        b'{"class":"TPV","mode":1}\n{"class":"TPV","mode":3,"lat":42.0,"lon":-83.0}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    assert len(fixes) == 2
    assert fixes[0].kind == FixKind.NO_FIX
    assert fixes[1].kind == FixKind.FIX_3D


def test_malformed_json_does_not_break_stream():
    """A bad line must be dropped, subsequent lines still parsed."""
    chunks = [
        b'this is not json\n',
        b'{"class":"TPV","mode":3,"lat":42.0,"lon":-83.0}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    assert len(fixes) == 1
    assert fixes[0].lat == 42.0


def test_unknown_class_ignored():
    """VERSION / DEVICES messages from gpsd must not produce fixes."""
    chunks = [
        b'{"class":"VERSION","release":"3.22"}\n',
        b'{"class":"DEVICES","devices":[]}\n',
        b'{"class":"WATCH","enable":true}\n',
    ]
    fixes = _drain(_make_client_with_chunks(chunks))
    assert fixes == []


def test_stop_event_breaks_stream():
    """Setting the stop event mid-stream must end the iterator."""
    client = _make_client_with_chunks([
        b'{"class":"TPV","mode":3,"lat":42.0,"lon":-83.0}\n',
    ] * 100)
    stop = threading.Event()

    received = []
    for f in client.stream(stop):
        received.append(f)
        if len(received) >= 3:
            stop.set()
    assert len(received) >= 3
    # Stream ended after stop set; we did not consume all 100.
    assert len(received) < 100


def test_eof_ends_stream_cleanly():
    """When the socket returns b'' the iterator stops without raising."""
    client = _make_client_with_chunks([
        b'{"class":"TPV","mode":3,"lat":42.0,"lon":-83.0}\n',
    ])
    fixes = _drain(client)
    assert len(fixes) == 1
