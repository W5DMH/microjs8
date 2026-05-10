"""Tests for app._compose_send_sync — the router callback that builds
the wire string and enqueues the compose for TX.

Covers wire-format construction, queue interaction, and degraded-path
behavior (no queue, queue full, builder-rejection).
"""
from __future__ import annotations

from pathlib import Path
import pytest

from microjs8.app import MicroJS8App
from microjs8.config import Config, StationConfig
from microjs8.ui.state import ComposeCmd


class _FakeOutboundQueue:
    """Minimal outbound-queue stub. Records enqueue_for_encoding calls
    and returns successive integer ids (or None to simulate full)."""

    def __init__(self, return_ids=None):
        self._return_ids = list(return_ids) if return_ids else None
        self._next_id = 1
        self.calls: list[tuple] = []

    def enqueue_for_encoding(self, text, kind=None, to_call=None):
        self.calls.append((text, kind, to_call))
        if self._return_ids is not None:
            return self._return_ids.pop(0) if self._return_ids else None
        rid = self._next_id
        self._next_id += 1
        return rid


def _make_app(grid: str = "EN83") -> MicroJS8App:
    """Construct a minimal app for unit-testing _compose_send_sync."""
    cfg = Config(station=StationConfig(callsign="W5DMH", grid=grid))
    app = MicroJS8App(cfg, headless=True)
    app._outbound_queue = _FakeOutboundQueue()
    return app


# ── Successful enqueues ───────────────────────────────────────────────


def test_compose_send_free_text_enqueues_to_and_text():
    app = _make_app()
    ok = app._compose_send_sync("K1ABC", ComposeCmd.FREE, "hello dave")
    assert ok is True
    assert app._outbound_queue.calls == [("K1ABC hello dave", None, "K1ABC")]


def test_compose_send_msg_inserts_verb():
    app = _make_app()
    ok = app._compose_send_sync("K1ABC", ComposeCmd.MSG, "hello")
    assert ok is True
    assert app._outbound_queue.calls == [("K1ABC MSG hello", None, "K1ABC")]


def test_compose_send_verb_only_command_skips_text():
    app = _make_app()
    ok = app._compose_send_sync("K1ABC", ComposeCmd.SNR_Q, "")
    assert ok is True
    assert app._outbound_queue.calls == [("K1ABC SNR?", None, "K1ABC")]


def test_compose_send_myloc_uses_station_grid():
    """MYLOC expands to 'TO GRID <my_grid>' using the station's
    configured grid — operator doesn't have to type it."""
    app = _make_app(grid="FN42")
    ok = app._compose_send_sync("K1ABC", ComposeCmd.MYLOC, "")
    assert ok is True
    assert app._outbound_queue.calls == [("K1ABC GRID FN42", None, "K1ABC")]


def test_compose_send_query_with_msgs_argument():
    app = _make_app()
    ok = app._compose_send_sync("K1ABC", ComposeCmd.QUERY, "MSGS")
    assert ok is True
    assert app._outbound_queue.calls == [("K1ABC QUERY MSGS", None, "K1ABC")]


# ── Rejection paths ───────────────────────────────────────────────────


def test_compose_send_rejects_empty_to():
    """Empty TO callsign → builder returns None → callback returns
    False without touching the queue."""
    app = _make_app()
    ok = app._compose_send_sync("", ComposeCmd.FREE, "hi")
    assert ok is False
    assert app._outbound_queue.calls == []


def test_compose_send_rejects_empty_body_for_body_cmds():
    app = _make_app()
    for cmd in (ComposeCmd.FREE, ComposeCmd.MSG, ComposeCmd.STORE, ComposeCmd.QUERY):
        ok = app._compose_send_sync("K1ABC", cmd, "")
        assert ok is False, cmd
    assert app._outbound_queue.calls == []


def test_compose_send_rejects_myloc_with_unconfigured_grid():
    """If station has no grid, MYLOC can't fire — better to reject
    than to broadcast a malformed 'GRID '."""
    app = _make_app(grid="")
    ok = app._compose_send_sync("K1ABC", ComposeCmd.MYLOC, "")
    assert ok is False
    assert app._outbound_queue.calls == []


# ── Degraded paths ────────────────────────────────────────────────────


def test_compose_send_returns_false_when_no_outbound_queue():
    """During early startup or in headless tests the queue may not
    yet be initialized. The callback must return False, not crash."""
    app = _make_app()
    app._outbound_queue = None
    ok = app._compose_send_sync("K1ABC", ComposeCmd.FREE, "hi")
    assert ok is False


def test_compose_send_returns_false_when_queue_full():
    """Queue full → enqueue_for_encoding returns None → callback
    returns False so the router/UI can react if it ever wants to."""
    app = _make_app()
    app._outbound_queue = _FakeOutboundQueue(return_ids=[None])
    ok = app._compose_send_sync("K1ABC", ComposeCmd.FREE, "hi")
    assert ok is False


def test_compose_send_swallows_queue_exception():
    """If enqueue_for_encoding raises, the callback returns False
    rather than crashing the input thread."""
    app = _make_app()

    class _Failing:
        def enqueue_for_encoding(self, text, kind=None, to_call=None):
            raise RuntimeError("simulated db error")
    app._outbound_queue = _Failing()
    ok = app._compose_send_sync("K1ABC", ComposeCmd.FREE, "hi")
    assert ok is False


# ── Self-target rejection ─────────────────────────────────────────────


def test_compose_send_rejects_to_equal_self():
    """TO == our own callsign → builder returns None → enqueue
    skipped. Prevents the gfsk8 AUTO_REMOVE_MYCALL malformed-frame
    bug (Varicode.cpp::buildMessageFrames strips leading call when
    it matches ours, which would silently drop the to-callsign)."""
    app = _make_app()
    # Station call is W5DMH (set by _make_app). Send to W5DMH.
    ok = app._compose_send_sync("W5DMH", ComposeCmd.MSG, "hi")
    assert ok is False
    assert app._outbound_queue.calls == []


def test_compose_send_rejects_to_equal_self_case_insensitive():
    app = _make_app()
    ok = app._compose_send_sync("w5dmh", ComposeCmd.FREE, "hi")
    assert ok is False
    assert app._outbound_queue.calls == []
