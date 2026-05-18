"""Regression tests for the May 15 2026 GPS null-island bug.

Bug history (W5DMH bench, May 2026): operator built a fresh image
and tested the EMERGENCY screen. With the home page showing
``3D fix (6 sat)``, the emergency page showed ``+0.0000, +0.0000``
for lat/lon — bogus coordinates instead of either the real position
or an "acquiring" indicator.

Root causes:

  1. ``_tpv_to_fix`` passed through gpsd's null-island sentinel —
     when gpsd has the time/ephemeris parts to call a fix mode-3
     but hasn't yet computed coordinates, it emits ``lat=0, lon=0``.
     We forwarded those zeros to the UI which faithfully rendered
     +0.0000 +0.0000.

  2. The HOME ``_gps_status_label`` only inspected ``kind`` and
     ``satellites_used``, declaring "3D fix (6 sat)" even when the
     fix had no usable position. Operators saw mixed signals: HOME
     said we were location-ready, EMERGENCY said we weren't.

Both fixes restore consistency: the parser nullifies null-island
coordinates, and the home label promotes "3D fix" only when
``has_position`` is True (else amber "acquiring").
"""

from __future__ import annotations

import pytest

from microjs8.gps.gpsd_client import _tpv_to_fix
from microjs8.gps.types import FixKind, GpsFix
from microjs8.ui.screens import _gps_status_label
from microjs8.ui.state import Screen, UIState


# ── _tpv_to_fix null-island filter ────────────────────────────────


def test_tpv_to_fix_filters_null_island_coordinates():
    """gpsd sometimes reports ``mode=3, lat=0, lon=0`` during fix
    acquisition. The parser must drop these placeholder zeros so
    downstream consumers don't render bogus coordinates."""
    tpv = {"mode": 3, "lat": 0.0, "lon": 0.0}
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=6)
    # kind is preserved (gpsd told us mode=3, that's what gpsd said)
    assert fix.kind is FixKind.FIX_3D
    assert fix.satellites_used == 6
    # But lat/lon are nullified so has_position correctly reports False
    assert fix.lat is None
    assert fix.lon is None
    assert fix.has_position is False


def test_tpv_to_fix_preserves_real_position():
    tpv = {"mode": 3, "lat": 42.3601, "lon": -71.0589}
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=8)
    assert fix.lat == 42.3601
    assert fix.lon == -71.0589
    assert fix.has_position is True


def test_tpv_to_fix_missing_lat_lon_keys():
    """gpsd often sends TPV reports without lat/lon during initial
    acquisition or between position updates. Missing keys → None."""
    tpv = {"mode": 3}
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=6)
    assert fix.lat is None
    assert fix.lon is None
    assert fix.has_position is False


def test_tpv_to_fix_preserves_position_on_equator_off_prime_meridian():
    """Legitimate position with lat=0 but lon!=0 (the equator)
    must NOT be filtered — the null-island filter requires BOTH
    coordinates to be exactly zero."""
    # Galapagos Islands sit on the equator at ~-78° longitude.
    tpv = {"mode": 3, "lat": 0.0, "lon": -78.0}
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=6)
    assert fix.lat == 0.0
    assert fix.lon == -78.0
    assert fix.has_position is True


def test_tpv_to_fix_preserves_position_on_prime_meridian_off_equator():
    """Conversely: legitimate position with lon=0 but lat!=0
    (the prime meridian, e.g., London). Must NOT be filtered."""
    # London Greenwich Observatory: lat ≈ 51.48, lon ≈ 0
    tpv = {"mode": 3, "lat": 51.48, "lon": 0.0}
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=6)
    assert fix.lat == 51.48
    assert fix.lon == 0.0
    assert fix.has_position is True


def test_tpv_to_fix_null_island_with_no_fix_mode():
    """Edge case: gpsd reports lat=0, lon=0 even with mode=NO_FIX.
    Filter still applies — there's nothing to display in either
    case, and consistency is cleanest."""
    tpv = {"mode": 1, "lat": 0.0, "lon": 0.0}
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=None)
    assert fix.kind is FixKind.NO_FIX
    assert fix.lat is None
    assert fix.lon is None


# ── _gps_status_label HOME consistency with has_position ──────────


def _state_with(fix: GpsFix) -> "UISnapshot":
    s = UIState("W5DMH", "EN83ih", True, "miles")
    s.set_gps(fix)
    return s.snapshot()


def _fix(kind: FixKind, *, lat=None, lon=None, sats=6) -> GpsFix:
    return GpsFix(
        kind=kind, lat=lat, lon=lon,
        altitude_m=None, speed_mps=None, track_deg=None, hdop=None,
        fix_time=None, satellites_used=sats, received_at=0.0,
    )


def test_home_label_3d_fix_with_real_position():
    """Mode-3 fix WITH valid lat/lon → 'N.D fix (X sat)' in green."""
    snap = _state_with(_fix(FixKind.FIX_3D, lat=42.3601, lon=-71.0589))
    label, color = _gps_status_label(snap)
    assert label == "3D fix (6 sat)"
    from microjs8.ui import theme
    assert color == theme.FG_GOOD


def test_home_label_3d_fix_without_real_position_shows_acquiring():
    """Mode-3 fix declared but lat/lon absent → 'acquiring' (amber).
    This is the bug fix: HOME no longer falsely advertises 3D fix
    when EMERGENCY would show no position."""
    snap = _state_with(_fix(FixKind.FIX_3D, lat=None, lon=None))
    label, color = _gps_status_label(snap)
    assert label == "acquiring (6 sat)"
    from microjs8.ui import theme
    assert color == theme.FG_WARN


def test_home_label_2d_fix_without_real_position_shows_acquiring():
    snap = _state_with(_fix(FixKind.FIX_2D, lat=None, lon=None))
    label, color = _gps_status_label(snap)
    assert label == "acquiring (6 sat)"


def test_home_label_no_fix_unchanged():
    snap = _state_with(_fix(FixKind.NO_FIX, sats=0))
    label, _ = _gps_status_label(snap)
    assert label == "no fix (0 sat)"


def test_home_label_2d_with_real_position_unchanged():
    """2D fix with real coords still displays as '2D fix' (warn
    color, because 2D is degraded from 3D)."""
    snap = _state_with(_fix(FixKind.FIX_2D, lat=42.3601, lon=-71.0589))
    label, _ = _gps_status_label(snap)
    assert label == "2D fix (6 sat)"


# ── End-to-end: gpsd null-island report consistent across screens ──


def test_null_island_report_renders_consistently_across_screens():
    """The full scenario: gpsd emits a null-island TPV; both HOME
    (acquiring) and EMERGENCY (configured-grid fallback) reflect
    the absence of a real position, in agreement with each other."""
    tpv = {"mode": 3, "lat": 0.0, "lon": 0.0}
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=6)
    s = UIState("W5DMH", "EN83ih", True, "miles")
    s.set_gps(fix)
    # HOME label
    label, _ = _gps_status_label(s.snapshot())
    assert "acquiring" in label
    # EMERGENCY: no GPS → falls back to configured grid (won't show
    # +0.0000, +0.0000). We confirm via the snapshot's gps.has_position
    # being False, which is the renderer's branching gate.
    assert s.snapshot().gps.has_position is False


# ── GpsdClient position cache (intermediate TPV continuity) ───────


def test_gpsd_client_caches_position_across_intermediate_tpv():
    """gpsd's TPV cadence exceeds the receiver's position-update
    rate. Many TPVs carry only mode/sat-count updates without
    lat/lon. The client caches the last good position and backfills
    intermediate TPVs so the UI sees continuous position rather
    than flickering between has_position=True/False.

    Without this caching, HOME flickered "acquiring (5 sat)" →
    "3D fix (6 sat)" → "acquiring (6 sat)" as consecutive TPVs
    arrived, even though the receiver was solidly locked the
    whole time. Observed W5DMH bench May 2026."""
    from microjs8.gps.gpsd_client import GpsdClient
    c = GpsdClient()
    c._satellites_used = 6

    # First TPV carries the real position. Cache fills.
    fix1 = c._parse_line(
        b'{"class":"TPV","mode":3,"lat":42.3601,"lon":-71.0589}'
    )
    assert fix1.has_position
    assert c._cached_lat == 42.3601
    assert c._cached_lon == -71.0589

    # Intermediate TPV — mode=3 but no lat/lon. Returned fix should
    # show the cached position so the UI doesn't lose it.
    fix2 = c._parse_line(b'{"class":"TPV","mode":3}')
    assert fix2.lat == 42.3601
    assert fix2.lon == -71.0589
    assert fix2.has_position
    # kind preserved from the new TPV.
    assert fix2.kind is FixKind.FIX_3D


def test_gpsd_client_cache_backfills_null_island_tpv():
    """When gpsd sends the null-island placeholder while the receiver
    is actually locked, the cache backfills. Without this, EMERGENCY
    would show ``+0.0000, +0.0000`` (pre-null-island-filter) or fall
    back to grid (post-null-island-filter) — neither reflects the
    real lock state."""
    from microjs8.gps.gpsd_client import GpsdClient
    c = GpsdClient()
    c._satellites_used = 6

    # Establish a real position.
    c._parse_line(b'{"class":"TPV","mode":3,"lat":42.3601,"lon":-71.0589}')

    # gpsd sends a null-island TPV. After the parser's null-island
    # filter, lat/lon would be None — but the cache backfills.
    fix = c._parse_line(b'{"class":"TPV","mode":3,"lat":0.0,"lon":0.0}')
    assert fix.lat == 42.3601
    assert fix.lon == -71.0589


def test_gpsd_client_cache_clears_on_no_fix():
    """Receiver explicitly losing lock (mode=1) must drop the cache.
    Otherwise UI would keep showing a stale position after the
    operator drives into a tunnel or the antenna goes offline."""
    from microjs8.gps.gpsd_client import GpsdClient
    c = GpsdClient()
    c._satellites_used = 6
    c._parse_line(b'{"class":"TPV","mode":3,"lat":42.3601,"lon":-71.0589}')
    assert c._cached_lat == 42.3601

    # NO_FIX clears the cache.
    fix = c._parse_line(b'{"class":"TPV","mode":1}')
    assert fix.kind is FixKind.NO_FIX
    assert fix.lat is None
    assert fix.lon is None
    assert c._cached_lat is None
    assert c._cached_lon is None

    # Subsequent intermediate TPV (no position) gets no backfill —
    # the cache is empty so we report missing position correctly.
    fix = c._parse_line(b'{"class":"TPV","mode":3}')
    assert fix.lat is None
    assert fix.lon is None


def test_gpsd_client_cache_clears_on_close():
    """Reconnect to gpsd implies a fresh session — drop stale
    coordinates so reader.py's reconnect path doesn't carry a
    position from before the disconnect."""
    from microjs8.gps.gpsd_client import GpsdClient
    c = GpsdClient()
    c._satellites_used = 6
    c._parse_line(b'{"class":"TPV","mode":3,"lat":42.3601,"lon":-71.0589}')
    assert c._cached_lat == 42.3601
    c.close()
    assert c._cached_lat is None
    assert c._cached_lon is None


def test_gpsd_client_cache_position_updates_overwrite_cache():
    """When the receiver computes a NEW position (e.g., the operator
    moves), the cache must update — we shouldn't lock in the first
    position observed for the session."""
    from microjs8.gps.gpsd_client import GpsdClient
    c = GpsdClient()
    c._satellites_used = 6
    c._parse_line(b'{"class":"TPV","mode":3,"lat":42.3601,"lon":-71.0589}')
    c._parse_line(b'{"class":"TPV","mode":3,"lat":40.0000,"lon":-75.0000}')
    assert c._cached_lat == 40.0000
    assert c._cached_lon == -75.0000

    # Subsequent intermediate TPV uses the NEW position.
    fix = c._parse_line(b'{"class":"TPV","mode":3}')
    assert fix.lat == 40.0000
    assert fix.lon == -75.0000


# ── ECEF→LLA fallback for gpsd 3.22 / u-blox 7 PROTVER 14 bug ────


def test_ecef_to_lla_w5dmh_bench_real_data():
    """Exact ECEF triple from W5DMH bench gpspipe capture (May 2026):
       (539473.15, -4619509.83, 4350310.41)
    gpsmon's own LTP-Pos converter reports:
       (43.2794029°N, -83.3390751°W, 234.44 m)
    Our Bowring conversion must agree to within ~10 m — far better
    than GPS receiver noise — confirming we can substitute it when
    gpsd's broken lat/lon path returns zeros."""
    from microjs8.gps.gpsd_client import _ecef_to_lla
    lat, lon, alt = _ecef_to_lla(539473.15, -4619509.83, 4350310.41)
    # Tolerances: 0.0001° (≈10 m at this latitude) for position,
    # 5 m for altitude (HAE vs MSL distinction adds noise).
    assert abs(lat - 43.2794029) < 0.0001, f"lat off: {lat}"
    assert abs(lon - (-83.3390751)) < 0.0001, f"lon off: {lon}"
    # Altitude is HAE here; we don't assert against the MSL value.


def test_ecef_to_lla_equator_prime_meridian():
    """Surface point at (0, 0) latitude/longitude is at ECEF
    (a, 0, 0) where a is the WGS84 semi-major axis."""
    from microjs8.gps.gpsd_client import _ecef_to_lla, _WGS84_A
    lat, lon, alt = _ecef_to_lla(_WGS84_A, 0.0, 0.0)
    assert abs(lat) < 1e-9
    assert abs(lon) < 1e-9
    assert abs(alt) < 1e-6  # exactly on the ellipsoid


def test_ecef_to_lla_north_pole():
    """Pole edge case: lat=+90, lon undefined."""
    from microjs8.gps.gpsd_client import _ecef_to_lla, _WGS84_B
    lat, lon, alt = _ecef_to_lla(0.0, 0.0, _WGS84_B)
    assert abs(lat - 90.0) < 1e-9
    # lon is undefined at the pole — we return 0 by convention.
    assert lon == 0.0


def test_tpv_to_fix_falls_back_to_ecef_when_lat_lon_zero():
    """The gpsd 3.22 / u-blox 7 PROTVER 14 bug: TPV reports lat/lon
    as 0.0 but ECEF coords are real. _tpv_to_fix must transparently
    derive lat/lon from ECEF so downstream consumers see a real
    position."""
    tpv = {
        "class": "TPV", "mode": 3,
        "lat": 0.0, "lon": 0.0,
        "altMSL": -17.0,
        "ecefx": 539473.15, "ecefy": -4619509.83, "ecefz": 4350310.41,
    }
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=10)
    assert fix.kind is FixKind.FIX_3D
    assert fix.lat is not None
    assert fix.lon is not None
    assert abs(fix.lat - 43.2794) < 0.001
    assert abs(fix.lon - (-83.3391)) < 0.001
    assert fix.has_position


def test_tpv_to_fix_no_ecef_treats_as_no_position():
    """When BOTH lat/lon are zero AND there's no usable ECEF data
    (or ECEF is also zero), treat as genuinely missing — the
    fallback can't manufacture a position out of nothing."""
    # No ECEF keys at all.
    tpv1 = {"class": "TPV", "mode": 3, "lat": 0.0, "lon": 0.0}
    fix1 = _tpv_to_fix(tpv1, now=1000.0, satellites_used=10)
    assert fix1.lat is None
    assert fix1.lon is None

    # ECEF all zero (truly at the Earth's centre — impossible).
    tpv2 = {
        "class": "TPV", "mode": 3, "lat": 0.0, "lon": 0.0,
        "ecefx": 0.0, "ecefy": 0.0, "ecefz": 0.0,
    }
    fix2 = _tpv_to_fix(tpv2, now=1000.0, satellites_used=10)
    assert fix2.lat is None
    assert fix2.lon is None


def test_tpv_to_fix_real_lat_lon_unaffected_by_fallback():
    """When gpsd does its job correctly (most receiver/firmware
    combinations), the fallback path is skipped entirely and the
    real lat/lon flows through unchanged."""
    tpv = {
        "class": "TPV", "mode": 3,
        "lat": 42.3601, "lon": -71.0589,
        "altMSL": 10.0,
        "ecefx": 1538725.0, "ecefy": -4463920.0, "ecefz": 4275172.0,
    }
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=8)
    assert fix.lat == 42.3601
    assert fix.lon == -71.0589
    assert fix.altitude_m == 10.0


def test_tpv_to_fix_uses_ecef_altitude_when_gpsd_altmsl_nonsense():
    """gpsd 3.22's null-island bug also sets altMSL to a nonsense
    value (typically -17 m, the negation of the geoid separation
    when altHAE was incorrectly zero). When we substitute ECEF for
    position, use the ECEF-derived altitude too if gpsd's altMSL
    looks like the broken default."""
    tpv = {
        "class": "TPV", "mode": 3,
        "lat": 0.0, "lon": 0.0,
        "altMSL": -17.0,
        "ecefx": 539473.15, "ecefy": -4619509.83, "ecefz": 4350310.41,
    }
    fix = _tpv_to_fix(tpv, now=1000.0, satellites_used=10)
    # ECEF-derived altitude (~236 m HAE for this position) should
    # have replaced gpsd's nonsense -17 m value.
    assert fix.altitude_m is not None
    assert fix.altitude_m > 100.0  # reasonable surface altitude
