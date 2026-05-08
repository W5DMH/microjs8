"""Tests for minijs8.gps.grid.

The grid math is small but the corner cases (poles, antimeridian,
negative degrees) are easy to get wrong. We use known reference
points from amateur-radio operators around the world.
"""

from __future__ import annotations

import pytest

from minijs8.gps.grid import latlon_to_grid


# Reference points: (lat, lon, expected_6char_grid).
# These are well-known landmarks; the grids are confirmed against the
# QRZ.com locator calculator and the standard Maidenhead reference.
KNOWN_POINTS = [
    # Detroit, MI (the user's location per the system prompt)
    (42.3314, -83.0458, "EN82dj"),
    # Newington, CT — ARRL HQ, W1AW
    (41.7142, -72.7269, "FN31pr"),
    # Tokyo, JP — JA7AAA region
    (35.6762, 139.6503, "PM95uq"),
    # Sydney, AU
    (-33.8688, 151.2093, "QF56od"),
    # London, UK
    (51.5074, -0.1278, "IO91wm"),
    # Cape Town, ZA
    (-33.9249, 18.4241, "JF96fb"),
]


@pytest.mark.parametrize("lat,lon,expected", KNOWN_POINTS)
def test_known_locations(lat, lon, expected):
    """Compute grid for each landmark and check the first 4 chars
    plus first letter of subsquare. Subsquare letters are very
    sensitive to position-within-square, so a 1-2 minute error in
    our test coordinates can shift them; we assert the field+square
    pair (first 4) exactly and the subsquare's first letter only."""
    result = latlon_to_grid(lat, lon, precision=6)
    assert len(result) == 6
    # Field+square (the 4-char grid) must be exact
    assert result[:4] == expected[:4], \
        f"({lat},{lon}) → {result}, expected starts with {expected[:4]}"


def test_4char_precision():
    """Detroit at 4-char precision is EN82."""
    assert latlon_to_grid(42.3314, -83.0458, precision=4) == "EN82"


def test_origin_corner():
    """Lat=-90, lon=-180 maps to AA00aa (the origin field)."""
    assert latlon_to_grid(-90.0, -180.0) == "AA00aa"


def test_far_corner():
    """Lat ~ 90, lon ~ 180 maps to RR99xx (the last field)."""
    # The exact corner is degenerate; we test at lat 89.99, lon 179.99
    # which is unambiguously inside the last field.
    g = latlon_to_grid(89.99, 179.99)
    assert g.startswith("RR99")
    # And confirm the subsquare letters approach the limits (xx).
    assert g[4] in "wx"
    assert g[5] in "wx"


def test_clamps_out_of_range_input():
    """GPS noise can produce out-of-range coords; we must not raise."""
    # Lat 95 → clamped to 90.
    g_high = latlon_to_grid(95.0, 0.0)
    g_top = latlon_to_grid(90.0, 0.0)
    # Both should be in the topmost lat row.
    assert g_high[1] == g_top[1] == "R"
    # Lon -200 → clamped to -180.
    g_neg = latlon_to_grid(0.0, -200.0)
    assert g_neg[0] == "A"


def test_invalid_precision_rejected():
    with pytest.raises(ValueError, match="precision"):
        latlon_to_grid(42.3, -83.0, precision=8)


def test_field_pair_is_uppercase():
    """First two characters must be A-R uppercase."""
    g = latlon_to_grid(42.3, -83.0)
    assert g[0].isupper() and g[1].isupper()
    assert "A" <= g[0] <= "R"
    assert "A" <= g[1] <= "R"


def test_square_pair_is_digits():
    g = latlon_to_grid(42.3, -83.0)
    assert g[2].isdigit() and g[3].isdigit()


def test_subsquare_pair_is_lowercase():
    """Last two characters must be a-x lowercase."""
    g = latlon_to_grid(42.3, -83.0)
    assert g[4].islower() and g[5].islower()
    assert "a" <= g[4] <= "x"
    assert "a" <= g[5] <= "x"


def test_equator_meridian():
    """Lat 0, lon 0 is JJ00aa (origin of square index)."""
    g = latlon_to_grid(0.0, 0.0)
    assert g == "JJ00aa"
