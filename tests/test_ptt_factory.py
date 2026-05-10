"""Tests for microjs8.cat.ptt_factory.build_ptt_service."""

from __future__ import annotations

import pytest

from microjs8.cat import (
    DIGIRIG_RTS_ONLY,
    QDX,
    XIEGU_G90_DIGIRIG,
    CatService,
    RadioDef,
    RtsPttService,
    build_ptt_service,
)


# ── Branching: cat_required → CatService, else RtsPttService ────────


def test_qdx_yields_cat_service():
    """QDX uses CAT (PTT command via TS-480 emulation)."""
    svc = build_ptt_service(QDX)
    assert isinstance(svc, CatService)


def test_g90_digirig_yields_cat_service():
    """G90+DigiRig still uses CatService — rigctld handles RTS-PTT
    internally on the same port; the application sees the same TCP
    interface as QDX. cat_required=True is what matters here."""
    svc = build_ptt_service(XIEGU_G90_DIGIRIG)
    assert isinstance(svc, CatService)


def test_digirig_rts_only_yields_rts_service():
    """No CAT — direct pyserial RTS toggle."""
    svc = build_ptt_service(DIGIRIG_RTS_ONLY)
    assert isinstance(svc, RtsPttService)


# ── Validation: RTS-only without serial path is a programming error ─


def test_rts_only_without_path_raises():
    """A radio with cat_required=False MUST have a
    preferred_serial_path. The factory raises rather than guessing."""
    bad = RadioDef(
        id="bad",
        display_name="Bad",
        description="missing serial path",
        hamlib_id=1,
        baud_rate=9600,
        cat_required=False,
        ptt_method="RTS",
        ptt_on_delay_ms=300,
        ptt_off_delay_ms=200,
        # preferred_serial_path left as default None
    )
    with pytest.raises(ValueError, match="preferred_serial_path"):
        build_ptt_service(bad)


# ── Status callback wiring ──────────────────────────────────────────


def test_callback_passed_to_cat_service():
    """The on_status_change callback is forwarded to the underlying
    service. Tests both branches: CatService and RtsPttService.
    """
    states_seen: list[bool] = []
    svc = build_ptt_service(
        QDX, on_status_change=lambda c: states_seen.append(c),
    )
    # Without start()ing, we can at least verify the callback
    # reference was stored. Implementation-specific access; if it
    # changes, this test will need updating.
    assert svc._on_status_change is not None  # type: ignore[attr-defined]


def test_callback_passed_to_rts_service():
    states_seen: list[bool] = []
    svc = build_ptt_service(
        DIGIRIG_RTS_ONLY,
        on_status_change=lambda c: states_seen.append(c),
    )
    assert svc._on_status_change is not None  # type: ignore[attr-defined]


# ── Construction does not start the service ─────────────────────────


def test_factory_does_not_call_start():
    """The factory constructs but does NOT call start() — that's
    the application's job. This lets tests examine the un-started
    service and lets app.py decide WHEN to begin the connection.
    """
    svc = build_ptt_service(DIGIRIG_RTS_ONLY)
    # An RtsPttService that hasn't started has no reconnect thread.
    assert svc._reconnect_thread is None  # type: ignore[attr-defined]
