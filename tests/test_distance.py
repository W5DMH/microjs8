"""Tests for microjs8.protocol.distance.

Distances between known cities, bearings to verify direction-of-travel
math, and edge cases.
"""

from __future__ import annotations

import pytest

from microjs8.protocol.distance import (
    distance_and_bearing,
    grid_to_latlon_center,
    haversine_distance_km,
    haversine_distance_miles,
    initial_bearing_deg,
)


# ── grid_to_latlon_center ───────────────────────────────────────────


def test_grid_center_4char():
    """EN82 covers Detroit area; center is roughly 42.5N, 83W."""
    lat, lon = grid_to_latlon_center("EN82")
    assert 42.0 < lat < 43.0
    assert -84.0 < lon < -82.0


def test_grid_center_6char_more_precise():
    """6-char grid center should be inside the 4-char center's square."""
    lat4, lon4 = grid_to_latlon_center("EN82")
    lat6, lon6 = grid_to_latlon_center("EN82dj")
    # 6-char grid is some subsquare within EN82.
    assert abs(lat6 - lat4) < 0.5
    assert abs(lon6 - lon4) < 1.0


def test_grid_invalid_raises():
    with pytest.raises(ValueError):
        grid_to_latlon_center("X")
    with pytest.raises(ValueError):
        grid_to_latlon_center("ZZ99")  # field pair out of range


def test_grid_origin():
    """AA00 covers (-90, -180); center is (-89.5, -179)."""
    lat, lon = grid_to_latlon_center("AA00")
    assert lat == pytest.approx(-89.5)
    assert lon == pytest.approx(-179.0)


# ── Haversine ───────────────────────────────────────────────────────


def test_haversine_zero_distance():
    """Same point → 0 distance."""
    assert haversine_distance_miles(42.0, -83.0, 42.0, -83.0) == pytest.approx(0.0)


def test_haversine_known_distance_detroit_to_newington():
    """Detroit area to Newington CT — roughly 530 miles, ~78° true.

    Exact distance depends on which points within EN82 / FN31 you pick;
    with the rough centers below we should get something in the
    520-560 range.
    """
    # Center coords
    lat1, lon1 = 42.4, -83.0
    lat2, lon2 = 41.7, -72.7
    miles = haversine_distance_miles(lat1, lon1, lat2, lon2)
    km = haversine_distance_km(lat1, lon1, lat2, lon2)
    assert 510 < miles < 560
    assert miles == pytest.approx(km * 0.621371, rel=0.001)


def test_initial_bearing_east():
    """From a point at (40, -80), bearing toward (40, -70) is ~90° (east)."""
    bearing = initial_bearing_deg(40.0, -80.0, 40.0, -70.0)
    assert 80 < bearing < 100


def test_initial_bearing_north():
    """From (30, -80) toward (40, -80) is ~0° (north)."""
    bearing = initial_bearing_deg(30.0, -80.0, 40.0, -80.0)
    assert bearing < 5 or bearing > 355


def test_initial_bearing_south():
    """From (40, -80) toward (30, -80) is ~180° (south)."""
    bearing = initial_bearing_deg(40.0, -80.0, 30.0, -80.0)
    assert 175 < bearing < 185


def test_initial_bearing_west():
    """From (40, -70) toward (40, -80) is ~270° (west)."""
    bearing = initial_bearing_deg(40.0, -70.0, 40.0, -80.0)
    assert 265 < bearing < 275


# ── distance_and_bearing wrapper ────────────────────────────────────


def test_distance_and_bearing_no_grid_returns_none():
    d, b = distance_and_bearing(None, "FN31", units="miles")
    assert d is None
    assert b is None
    d, b = distance_and_bearing("EN82", None, units="miles")
    assert d is None
    assert b is None


def test_distance_and_bearing_invalid_grid_returns_none():
    """Malformed grid should yield (None, None), not raise."""
    d, b = distance_and_bearing("EN82", "ZZZZZZ", units="miles")
    assert d is None
    assert b is None


def test_distance_and_bearing_km_units():
    """km option produces km, not miles."""
    d_mi, _ = distance_and_bearing("EN82", "FN31", units="miles")
    d_km, _ = distance_and_bearing("EN82", "FN31", units="km")
    assert d_mi is not None and d_km is not None
    assert d_km > d_mi  # km is a smaller unit, so the number is bigger


def test_distance_and_bearing_same_grid_zero():
    d, _ = distance_and_bearing("EN82dj", "EN82dj", units="miles")
    assert d == pytest.approx(0.0)
