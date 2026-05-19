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
from microjs8.ui.state import HbMode


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


# ── Phase 13: Heartbeat-mode lifecycle ───────────────────────────

# ── Heartbeat-mode lifecycle ────────────────────────────────────────


def test_on_hb_mode_change_off_does_nothing_when_no_beacon():
    """Setting OFF with no active beacon is a no-op (the default
    state). Must not crash."""
    app = _make_app()
    assert app._hb_beacon is None
    app._on_hb_mode_change(HbMode.OFF)
    assert app._hb_beacon is None


def test_on_hb_mode_change_twenty_min_starts_beacon():
    """Setting TWENTY_MIN constructs and starts a beacon thread
    with the correct interval."""
    app = _make_app()
    app._on_hb_mode_change(HbMode.TWENTY_MIN)
    try:
        assert app._hb_beacon is not None
        assert app._hb_beacon.is_alive()
        assert app._hb_beacon._interval_s == 20 * 60
        # Wait for the immediate-on-start fire.
        for _ in range(20):
            if app._hb_beacon.fire_count >= 1:
                break
            time.sleep(0.05)
        assert app._hb_beacon.fire_count >= 1
    finally:
        if app._hb_beacon is not None:
            app._hb_beacon.stop()
            app._hb_beacon.join(timeout=2.0)


def test_on_hb_mode_change_one_hr_uses_3600s_interval():
    app = _make_app()
    app._on_hb_mode_change(HbMode.ONE_HR)
    try:
        assert app._hb_beacon._interval_s == 60 * 60
    finally:
        if app._hb_beacon is not None:
            app._hb_beacon.stop()
            app._hb_beacon.join(timeout=2.0)


def test_on_hb_mode_change_off_stops_running_beacon():
    """Transitioning OFF must stop the active beacon thread and
    clear the reference."""
    app = _make_app()
    app._on_hb_mode_change(HbMode.TWENTY_MIN)
    assert app._hb_beacon is not None and app._hb_beacon.is_alive()
    app._on_hb_mode_change(HbMode.OFF)
    assert app._hb_beacon is None


def test_on_hb_mode_change_replaces_existing_beacon():
    """Changing from one repeating mode to another stops the old
    beacon and starts a new one with the new interval."""
    app = _make_app()
    app._on_hb_mode_change(HbMode.TWENTY_MIN)
    old = app._hb_beacon
    assert old is not None
    app._on_hb_mode_change(HbMode.ONE_HR)
    try:
        # Old beacon stopped.
        assert not old.is_alive()
        # New beacon different + alive + has 1HR interval.
        assert app._hb_beacon is not old
        assert app._hb_beacon.is_alive()
        assert app._hb_beacon._interval_s == 60 * 60
    finally:
        if app._hb_beacon is not None:
            app._hb_beacon.stop()
            app._hb_beacon.join(timeout=2.0)


def test_hb_identity_returns_call_and_4char_grid():
    """The beacon's identity-factory returns (callsign, grid[:4])."""
    app = _make_app(grid="EN83ih")
    assert app._hb_identity() == ("W5DMH", "EN83")


def test_hb_identity_returns_none_when_callsign_unset():
    """No callsign → no identity → beacon skips that fire cycle."""
    cfg = Config(station=StationConfig(callsign="", grid="EN83"))
    app = MicroJS8App(cfg, headless=True)
    assert app._hb_identity() is None


def test_hb_identity_returns_none_when_callsign_is_n0call():
    cfg = Config(station=StationConfig(callsign="N0CALL", grid="EN83"))
    app = MicroJS8App(cfg, headless=True)
    assert app._hb_identity() is None


def test_hb_identity_returns_none_when_grid_empty():
    app = _make_app(grid="")
    assert app._hb_identity() is None
