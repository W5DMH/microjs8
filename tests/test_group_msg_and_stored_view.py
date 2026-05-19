"""Tests for the May 2026 group-MSG + STORE-viewer fixes.

Three independent fixes shipped in one drop:

1. ``infer_outbound_kind`` returns ``REPLY`` for any ``@``-prefixed
   recipient — fire-and-forget for group blasts (no retransmit loop).

2. ``_compose_store_sync`` (and equivalent at the mailbox layer)
   rejects ``@``-prefixed TO callsigns — STOREs require a personal
   destination since QUERY MSGS is callsign-keyed.

3. The INBOX screen now shows STORE rows interleaved with UNREAD/READ
   rows (Option C — combined list), tagged visually so the operator
   can distinguish "for me" from "held for somebody else". Delete
   in detail view removes any row type uniformly.
"""

from __future__ import annotations

import pytest

from microjs8.tx.queue import OutboundKind, infer_outbound_kind
from microjs8.ui.state import InboxRow, Screen, UIState


# ── Fix 1: Group MSG fire-and-forget ─────────────────────────────────


def test_group_msg_classified_as_reply_not_directed():
    """The bug we shipped: ``@EMCOMM MSG body`` was classified as
    DIRECTED → scheduler entered WAIT_ACK → retransmitted because no
    single station's ACK matched the queued recipient. The fix:
    @-prefixed recipients always REPLY regardless of verb."""
    assert (
        infer_outbound_kind("@EMCOMM MSG hello world") is OutboundKind.REPLY
    )


def test_personal_msg_still_directed():
    """The fix must not regress the personal-MSG case — personal MSG
    DOES expect a single-station ACK and we want WAIT_ACK semantics."""
    assert (
        infer_outbound_kind("K1ABC MSG hello world") is OutboundKind.DIRECTED
    )


def test_msg_to_relay_still_directed():
    """``K1ABC MSG TO:KD8PGB body`` is a relay-store request to a
    specific intermediate — it auto-ACKs from the relay. Stays
    DIRECTED."""
    assert (
        infer_outbound_kind("K1ABC MSG TO:KD8PGB body") is OutboundKind.DIRECTED
    )


def test_group_msg_various_groups_all_reply():
    for wire in [
        "@EMCOMM MSG body",
        "@SKYWARN MSG body",
        "@DX/NA MSG body",
        "@REGION/1 MSG body",
        "@JS8NET MSG body",
        "@ARES MSG body",
    ]:
        assert infer_outbound_kind(wire) is OutboundKind.REPLY, wire


def test_group_non_msg_verbs_unchanged_reply():
    """Group queries / replies were already REPLY before the fix —
    confirm the fix didn't accidentally change their behavior."""
    assert infer_outbound_kind("@EMCOMM SNR?") is OutboundKind.REPLY
    assert infer_outbound_kind("@EMCOMM GRID?") is OutboundKind.REPLY
    assert infer_outbound_kind("@EMCOMM QUERY MSGS") is OutboundKind.REPLY


def test_allcall_and_hb_msg_also_reply():
    """The implicit broadcasts (@ALLCALL, @HB) should also fire-and-
    forget for any verb. Most paths set OutboundKind explicitly for
    these, but the inference must not loop on them either."""
    assert infer_outbound_kind("@ALLCALL MSG body") is OutboundKind.REPLY
    assert infer_outbound_kind("@HB MSG body") is OutboundKind.REPLY


def test_empty_and_malformed_wires_safe_reply():
    """The fix shouldn't change safe-default behavior on garbage in."""
    assert infer_outbound_kind("") is OutboundKind.REPLY
    assert infer_outbound_kind("   ") is OutboundKind.REPLY
    assert infer_outbound_kind("ONLY_RECIPIENT") is OutboundKind.REPLY


# ── Fix 2: STORE validation rejects @-prefixed TO ────────────────────


class _FakeMailbox:
    """Minimal stub for _compose_store_sync — captures add_local_store
    calls so we can assert the validation gate kept us out."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def add_local_store(self, *, recipient_call, text, our_call):
        self.calls.append((recipient_call, text, our_call))
        return 1


def _make_app_stub_for_store(
    callsign: str = "W5DMH",
    grid: str = "EN83",
):
    """Build a minimal object with the attributes _compose_store_sync
    reads. Lets us test the validation without spinning a full App."""
    class _Cfg:
        class _Station:
            pass
    cfg = _Cfg()
    cfg.station = _Cfg._Station()
    cfg.station.callsign = callsign
    cfg.station.grid = grid

    class _App:
        def __init__(self):
            self._config = cfg
            self._mailbox = _FakeMailbox()
            self._refreshes = 0
        def _refresh_inbox_ui(self):
            self._refreshes += 1

    # Bind the real method to the stub so it uses our state.
    import types
    from microjs8.app import MicroJS8App
    app = _App()
    app._compose_store_sync = types.MethodType(
        MicroJS8App._compose_store_sync, app,
    )
    return app


def test_compose_store_rejects_group_destination():
    """@-prefixed TO must not produce a STORE row.

    Reasons:
      - Groups have no single QUERY MSGS asker → we can never know
        which station's query triggers delivery
      - Delivering to every group member separately would multiply
        the message count
      - Universal broadcasts (@ALLCALL/@HB) have semantic absurdity:
        "holding mail for everyone"
    """
    app = _make_app_stub_for_store()
    ok = app._compose_store_sync(to="@EMCOMM", text="hello group")
    assert ok is False
    assert app._mailbox.calls == [], (
        f"add_local_store should not have been called, got {app._mailbox.calls}"
    )


def test_compose_store_rejects_allcall():
    app = _make_app_stub_for_store()
    ok = app._compose_store_sync(to="@ALLCALL", text="body")
    assert ok is False
    assert app._mailbox.calls == []


def test_compose_store_rejects_hb():
    app = _make_app_stub_for_store()
    ok = app._compose_store_sync(to="@HB", text="body")
    assert ok is False


def test_compose_store_personal_callsign_succeeds():
    """Regression: validation must NOT block legitimate STOREs."""
    app = _make_app_stub_for_store()
    ok = app._compose_store_sync(to="KD8PGB", text="hello there")
    assert ok is True
    assert len(app._mailbox.calls) == 1
    recipient, text, our_call = app._mailbox.calls[0]
    assert recipient == "KD8PGB"
    assert text == "hello there"


def test_compose_store_lowercase_at_still_rejected():
    """The @ check happens after uppercasing — lowercase '@emcomm'
    is also rejected, not slipped through by case."""
    app = _make_app_stub_for_store()
    ok = app._compose_store_sync(to="@emcomm", text="body")
    assert ok is False


# ── Fix 3: STORE rows in unified INBOX list ──────────────────────────


def _make_record(
    row_id: int,
    msg_type: str,
    from_call: str,
    to_call: str,
    text: str,
    utc_iso: str = "2026-05-15T12:34:00+00:00",
    snr_db=None,
):
    """Build a duck-typed InboxRecord-shaped object for set_inbox."""
    class _Rec:
        pass
    r = _Rec()
    r.id = row_id
    r.type = msg_type
    r.from_call = from_call
    r.to_call = to_call
    r.text = text
    r.utc_iso = utc_iso
    r.snr_db = snr_db
    r.offset_hz = None
    return r


def test_set_inbox_distinguishes_store_rows():
    """STORE row gets ``is_stored=True`` and the recipient field
    populated. UNREAD/READ rows do not."""
    s = UIState("W5DMH", "EN83", True, "miles")
    s.set_inbox(
        records=(
            _make_record(1, "UNREAD", "K1ABC", "W5DMH", "incoming"),
            _make_record(2, "STORE",  "W5DMH", "KD8PGB", "held mail"),
            _make_record(3, "READ",   "LF0LFN", "W5DMH", "old read"),
        ),
        held_count=1,
        unread_count=1,
    )
    rows = s.snapshot().inbox_messages
    assert len(rows) == 3
    by_id = {r.id: r for r in rows}
    # Row 1: inbound UNREAD — not stored, no recipient field
    assert by_id[1].is_stored is False
    assert by_id[1].recipient is None
    assert by_id[1].is_read is False
    # Row 2: STORE — stored True, recipient populated
    assert by_id[2].is_stored is True
    assert by_id[2].recipient == "KD8PGB"
    # STORE rows are not "unread" — operator never needs to "open and
    # read" their own held mail. Rendering uses FG_DIM by default to
    # signal secondary content.
    assert by_id[2].is_read is True
    # Row 3: READ inbound
    assert by_id[3].is_stored is False
    assert by_id[3].is_read is True


def test_set_inbox_preserves_existing_inbox_only_behavior():
    """Existing callers (only passing UNREAD/READ) must see the
    unchanged ``is_read`` mapping — no surprise behavior."""
    s = UIState("W5DMH", "EN83", True, "miles")
    s.set_inbox(
        records=(
            _make_record(1, "UNREAD", "K1ABC", "W5DMH", "x"),
            _make_record(2, "READ",   "K2ABC", "W5DMH", "y"),
        ),
        held_count=0,
        unread_count=1,
    )
    rows = s.snapshot().inbox_messages
    assert all(r.is_stored is False for r in rows)
    assert all(r.recipient is None for r in rows)


def test_inboxrow_default_field_values():
    """Frozen-dataclass default safety: when the rest of the codebase
    constructs an InboxRow without explicitly setting is_stored or
    recipient, the defaults are False / None (= a normal inbox row).
    Regression-proofs older test fixtures."""
    r = InboxRow(
        id=1, from_call="K1ABC", body="x", utc_iso="",
        snr_db=None, is_read=False,
    )
    assert r.is_stored is False
    assert r.recipient is None


def test_inbox_message_count_excludes_stored_for_inbox_count():
    """The footer split: total list rows = inbox_count + stored_count.
    Verify the data structure supports that decomposition."""
    s = UIState("W5DMH", "EN83", True, "miles")
    s.set_inbox(
        records=(
            _make_record(1, "UNREAD", "K1ABC", "W5DMH", "x"),
            _make_record(2, "STORE",  "W5DMH", "KD8PGB", "held1"),
            _make_record(3, "STORE",  "W5DMH", "K3DEF", "held2"),
            _make_record(4, "READ",   "K1ABC", "W5DMH", "y"),
        ),
        held_count=2,
        unread_count=1,
    )
    rows = s.snapshot().inbox_messages
    stored_count = sum(1 for r in rows if r.is_stored)
    inbox_count = sum(1 for r in rows if not r.is_stored)
    assert stored_count == 2
    assert inbox_count == 2
    assert len(rows) == 4


# ── Inbox list rendering: STORE rows visible + tagged ────────────────


def test_render_inbox_with_stored_rows_smoke():
    """Smoke test: rendering shouldn't crash with mixed inbox + STORE
    rows. Pixel-perfect assertions on a PIL image are too brittle; we
    just verify the render returns an image of the expected size."""
    from microjs8.ui.screens import _render_inbox
    from microjs8.ui.fonts import load_fonts
    s = UIState("W5DMH", "EN83", True, "miles")
    s.set_screen(Screen.INBOX)
    s.set_inbox(
        records=(
            _make_record(1, "UNREAD", "K1ABC", "W5DMH", "hello"),
            _make_record(2, "STORE",  "W5DMH", "KD8PGB", "held for kd8pgb"),
        ),
        held_count=1,
        unread_count=1,
    )
    fonts = load_fonts()
    img = _render_inbox(s.snapshot(), fonts)
    # The render-canvas helper returns a 240x240 image.
    assert img.size == (320, 170)


def test_render_inbox_detail_for_stored_row_smoke():
    """The detail view for STORE rows should render without crashing
    (separate header text, separate field layout)."""
    from microjs8.ui.screens import _render_inbox_detail
    from microjs8.ui.fonts import load_fonts
    s = UIState("W5DMH", "EN83", True, "miles")
    s.set_screen(Screen.INBOX)
    s.set_inbox(
        records=(
            _make_record(7, "STORE", "W5DMH", "KD8PGB", "test body"),
        ),
        held_count=1,
        unread_count=0,
    )
    # Walk through the normal Enter flow to set inbox_detail_id.
    s.inbox_open_detail()
    fonts = load_fonts()
    img = _render_inbox_detail(s.snapshot(), fonts)
    assert img.size == (320, 170)


def test_render_inbox_detail_for_read_row_unchanged():
    """Regression: the inbox-only detail render path must still work
    (it predates the STORE viewer; we should not have broken it)."""
    from microjs8.ui.screens import _render_inbox_detail
    from microjs8.ui.fonts import load_fonts
    s = UIState("W5DMH", "EN83", True, "miles")
    s.set_screen(Screen.INBOX)
    s.set_inbox(
        records=(
            _make_record(1, "READ", "K1ABC", "W5DMH", "test body"),
        ),
        held_count=0,
        unread_count=0,
    )
    s.inbox_open_detail()
    fonts = load_fonts()
    img = _render_inbox_detail(s.snapshot(), fonts)
    assert img.size == (320, 170)


# ── Mailbox list_inbox_with_stored ───────────────────────────────────


@pytest.fixture
def mailbox(tmp_path):
    """Real on-disk MailboxStore in a tmp path."""
    from microjs8.store.inbox import MailboxStore
    db = tmp_path / "inbox.db"
    mb = MailboxStore(db)
    yield mb
    mb.close()


def test_mailbox_list_inbox_with_stored_returns_all_three_types(mailbox):
    """list_inbox_with_stored returns UNREAD + READ + STORE rows,
    newest first. DELIVERED stays excluded (it's history, not
    actionable)."""
    mailbox.add_unread(
        from_call="K1ABC", text="unread msg", our_call="W5DMH",
    )
    rid_read = mailbox.add_unread(
        from_call="K2ABC", text="read msg", our_call="W5DMH",
    )
    mailbox.mark_read(rid_read)
    mailbox.add_local_store(
        recipient_call="KD8PGB", text="held msg", our_call="W5DMH",
    )

    rows = mailbox.list_inbox_with_stored(limit=10)
    types = sorted({r.type for r in rows})
    assert "UNREAD" in types
    assert "READ" in types
    assert "STORE" in types


def test_mailbox_list_inbox_excludes_store(mailbox):
    """Regression: the non-stored list_inbox must NOT start returning
    STORE rows — existing callers (the inbox-only screens) rely on
    that filtering."""
    mailbox.add_unread(
        from_call="K1ABC", text="hi", our_call="W5DMH",
    )
    mailbox.add_local_store(
        recipient_call="KD8PGB", text="held", our_call="W5DMH",
    )

    rows = mailbox.list_inbox(limit=10)
    types = sorted({r.type for r in rows})
    assert "STORE" not in types
    assert "UNREAD" in types
