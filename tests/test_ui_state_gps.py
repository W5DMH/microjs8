"""Tests for UIState.set_gps and the gps_grid recomputation."""

from __future__ import annotations

import time

from microjs8.gps.types import FixKind, GpsFix
from microjs8.ui.state import UIState


def _fix(kind: FixKind, lat: float = 42.3, lon: float = -83.0,
         sats: int | None = None) -> GpsFix:
    return GpsFix(
        kind=kind, lat=lat, lon=lon, altitude_m=None,
        speed_mps=None, track_deg=None, hdop=None,
        fix_time=None, satellites_used=sats,
        received_at=time.monotonic(),
    )


def _state() -> UIState:
    return UIState("K1ABC", "FN42", True, "miles")


def test_initial_state_has_no_fix():
    s = _state()
    snap = s.snapshot()
    assert snap.gps.kind == FixKind.NO_FIX
    assert snap.gps_grid is None


def test_3d_fix_populates_gps_grid():
    s = _state()
    s.consume_dirty()
    s.set_gps(_fix(FixKind.FIX_3D, lat=42.3314, lon=-83.0458))
    snap = s.snapshot()
    assert snap.gps.kind == FixKind.FIX_3D
    assert snap.gps_grid is not None
    assert snap.gps_grid.startswith("EN82")  # Detroit
    assert s.consume_dirty()


def test_no_fix_clears_gps_grid():
    s = _state()
    s.set_gps(_fix(FixKind.FIX_3D))
    s.consume_dirty()
    s.set_gps(_fix(FixKind.NO_FIX))
    snap = s.snapshot()
    assert snap.gps.kind == FixKind.NO_FIX
    assert snap.gps_grid is None
    assert s.consume_dirty()


def test_position_change_within_same_grid_does_not_dirty():
    """Moving 100 m within the same 6-char subsquare must NOT redraw —
    the displayed grid is identical and we don't want NMEA's 1 Hz rate
    to keep marking the screen dirty."""
    s = _state()
    s.set_gps(_fix(FixKind.FIX_3D, lat=42.3314, lon=-83.0458))
    # First fix sets grid; consume the dirty flag.
    s.consume_dirty()
    # Tiny position change — well within a 6-char subsquare.
    s.set_gps(_fix(FixKind.FIX_3D, lat=42.3315, lon=-83.0459))
    assert not s.consume_dirty()


def test_satellite_count_change_dirties():
    s = _state()
    s.set_gps(_fix(FixKind.FIX_3D, sats=4))
    s.consume_dirty()
    s.set_gps(_fix(FixKind.FIX_3D, sats=7))
    assert s.consume_dirty()


def test_grid_change_dirties():
    s = _state()
    s.set_gps(_fix(FixKind.FIX_3D, lat=42.3, lon=-83.0))
    s.consume_dirty()
    # Move to a different grid square.
    s.set_gps(_fix(FixKind.FIX_3D, lat=43.5, lon=-84.0))
    assert s.consume_dirty()
