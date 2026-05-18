"""Tests for the ALLCALL action callbacks and the heartbeat-mode
lifecycle handler in MicroJS8App.

Covers:
  - _allcall_query_msgs_sync wire + kind
  - _allcall_cq_sync wire + kind + grid-required behavior
  - _on_hb_mode_change starts/stops/restarts the beacon thread to
    match the selected mode

The beacon-thread tests use very short intervals to verify lifecycle
without burning real wall-clock time.
"""
from __future__ import annotations

import time

import pytest

from microjs8.app import MicroJS8App
from microjs8.config import Config, StationConfig
from microjs8.tx.queue import OutboundKind
# HbMode import deferred to Phase 13 (Heartbeat lifecycle tests).


class _FakeOutboundQueue:
    """Same stub used elsewhere — records enqueue_for_encoding calls."""

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


def _make_app(grid: str = "EN83ih") -> MicroJS8App:
    cfg = Config(station=StationConfig(callsign="W5DMH", grid=grid))
    app = MicroJS8App(cfg, headless=True)
    app._outbound_queue = _FakeOutboundQueue()
    return app


# ── _allcall_query_msgs_sync ────────────────────────────────────────


def test_allcall_query_msgs_enqueues_correct_wire_and_kind():
    """Wire format: '@ALLCALL QUERY MSGS'. Kind: ALLCALL."""
    app = _make_app()
    ok = app._allcall_query_msgs_sync()
    assert ok is True
    assert app._outbound_queue.calls == [
        ("@ALLCALL QUERY MSGS", OutboundKind.ALLCALL, None),
    ]


def test_allcall_query_msgs_returns_false_when_queue_full():
    app = _make_app()
    app._outbound_queue = _FakeOutboundQueue(return_ids=[None])
    ok = app._allcall_query_msgs_sync()
    assert ok is False


def test_allcall_query_msgs_returns_false_when_no_queue():
    app = _make_app()
    app._outbound_queue = None
    ok = app._allcall_query_msgs_sync()
    assert ok is False


# ── _allcall_cq_sync ────────────────────────────────────────────────


def test_allcall_cq_enqueues_correct_wire_and_kind():
    """Wire format: 'CQ CQ CQ <4-char-grid>'. Kind: CQ. The grid
    is truncated to 4 characters from the configured locator."""
    app = _make_app(grid="EN83ih")
    ok = app._allcall_cq_sync()
    assert ok is True
    assert app._outbound_queue.calls == [
        ("CQ CQ CQ EN83", OutboundKind.CQ, None),
    ]


def test_allcall_cq_uses_full_grid_when_4_or_fewer_chars():
    """4-char grid passes through unchanged. Shorter shouldn't
    happen in practice but be defensive."""
    app = _make_app(grid="EN83")
    app._allcall_cq_sync()
    assert app._outbound_queue.calls == [
        ("CQ CQ CQ EN83", OutboundKind.CQ, None),
    ]


def test_allcall_cq_returns_false_when_grid_is_empty():
    """No grid → CQ is meaningless. Skip."""
    app = _make_app(grid="")
    ok = app._allcall_cq_sync()
    assert ok is False
    assert app._outbound_queue.calls == []


def test_allcall_cq_returns_false_when_no_queue():
    app = _make_app()
    app._outbound_queue = None
    ok = app._allcall_cq_sync()
    assert ok is False


# Heartbeat-mode lifecycle tests deferred to Phase 13.
