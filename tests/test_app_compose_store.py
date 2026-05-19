"""Tests for app.py's ``_compose_store_sync`` — the STORE command's
local-mailbox write path.

STORE on COMPOSE doesn't transmit anything. It writes a row to
``inbox.db`` keyed for the TO callsign. When that station later
sends us ``QUERY MSGS`` directed at our call, the existing inbound
handler delivers the body. This file exercises just the write side;
the inbound-deliver path is exercised in test_app_inbox_dispatch.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from microjs8.app import MicroJS8App
from microjs8.config import Config, StationConfig
from microjs8.store.inbox import MailboxStore, TYPE_STORE


def _make_app(
    tmp_path: Path,
    callsign: str = "W5DMH",
    grid: str = "EN83",
) -> MicroJS8App:
    cfg = Config(station=StationConfig(callsign=callsign, grid=grid))
    app = MicroJS8App(cfg, headless=True)
    app._mailbox = MailboxStore(tmp_path / "inbox.db")
    return app


# ── Happy path ────────────────────────────────────────────────────


def test_compose_store_writes_local_store_row(tmp_path):
    """STORE on COMPOSE writes a STORE row keyed for the TO callsign,
    with FROM=our_call. Nothing transmits."""
    app = _make_app(tmp_path)
    ok = app._compose_store_sync(to="K1ABC", text="ping when you see this")
    assert ok is True
    rows = app._mailbox.list_holding_for("K1ABC")
    assert len(rows) == 1
    row = rows[0]
    assert row.type == TYPE_STORE
    assert row.from_call == "W5DMH"
    assert row.to_call == "K1ABC"
    assert row.text == "ping when you see this"


def test_compose_store_uppercases_to_callsign(tmp_path):
    """Operator-typed lowercase callsigns are normalised on write so
    the inbox lookup (by uppercase) finds them later."""
    app = _make_app(tmp_path)
    app._compose_store_sync(to="k1abc", text="body")
    rows = app._mailbox.list_holding_for("K1ABC")
    assert len(rows) == 1
    assert rows[0].to_call == "K1ABC"


def test_compose_store_strips_whitespace(tmp_path):
    app = _make_app(tmp_path)
    app._compose_store_sync(to="  K1ABC  ", text="  body with edges  ")
    rows = app._mailbox.list_holding_for("K1ABC")
    assert len(rows) == 1
    assert rows[0].to_call == "K1ABC"
    assert rows[0].text == "body with edges"


def test_compose_store_allows_self_call(tmp_path):
    """STORE doesn't transmit, so gfsk8's AUTO_REMOVE_MYCALL strip
    doesn't apply. Storing a note 'for myself' is harmless."""
    app = _make_app(tmp_path, callsign="W5DMH")
    ok = app._compose_store_sync(to="W5DMH", text="remind future-me")
    assert ok is True
    rows = app._mailbox.list_holding_for("W5DMH")
    assert len(rows) == 1
    assert rows[0].from_call == "W5DMH"


# ── Validation rejections ─────────────────────────────────────────


def test_compose_store_rejects_empty_to(tmp_path):
    app = _make_app(tmp_path)
    ok = app._compose_store_sync(to="", text="body")
    assert ok is False
    assert app._mailbox.list_holding_for("K1ABC") == []


def test_compose_store_rejects_whitespace_only_to(tmp_path):
    app = _make_app(tmp_path)
    ok = app._compose_store_sync(to="   ", text="body")
    assert ok is False
    assert app._mailbox.list_holding_for("K1ABC") == []


def test_compose_store_rejects_empty_text(tmp_path):
    app = _make_app(tmp_path)
    ok = app._compose_store_sync(to="K1ABC", text="")
    assert ok is False
    assert app._mailbox.list_holding_for("K1ABC") == []


def test_compose_store_rejects_whitespace_only_text(tmp_path):
    app = _make_app(tmp_path)
    ok = app._compose_store_sync(to="K1ABC", text="   ")
    assert ok is False
    assert app._mailbox.list_holding_for("K1ABC") == []


def test_compose_store_rejects_unconfigured_station(tmp_path):
    """STORE rows must be attributable to a real originator. If the
    operator has not yet set their callsign, we reject."""
    cfg = Config(station=StationConfig(callsign="N0CALL", grid=""))
    app = MicroJS8App(cfg, headless=True)
    app._mailbox = MailboxStore(tmp_path / "inbox.db")
    ok = app._compose_store_sync(to="K1ABC", text="body")
    assert ok is False
    assert app._mailbox.list_holding_for("K1ABC") == []


def test_compose_store_returns_false_when_no_mailbox(tmp_path):
    """Test-harness / early-startup defensive case: no mailbox →
    return False without crashing."""
    cfg = Config(station=StationConfig(callsign="W5DMH", grid="EN83"))
    app = MicroJS8App(cfg, headless=True)
    app._mailbox = None
    ok = app._compose_store_sync(to="K1ABC", text="body")
    assert ok is False


# ── Multiple STOREs accumulate ────────────────────────────────────


def test_compose_store_multiple_rows_accumulate(tmp_path):
    """Storing multiple messages for different recipients keeps each
    row distinct — the recipient queries against their own callsign."""
    app = _make_app(tmp_path)
    app._compose_store_sync(to="K1ABC", text="for alice")
    app._compose_store_sync(to="KD8GIJ", text="for bob")
    app._compose_store_sync(to="K1ABC", text="for alice again")
    alice = app._mailbox.list_holding_for("K1ABC")
    bob = app._mailbox.list_holding_for("KD8GIJ")
    assert len(alice) == 2
    assert len(bob) == 1
    # Oldest-first ordering matches the QUERY MSGS reply contract.
    assert [r.text for r in alice] == ["for alice", "for alice again"]
    assert bob[0].text == "for bob"
