"""Distance and bearing between Maidenhead grids.

Computes great-circle distance (in miles or kilometers) and initial
bearing (degrees true, 0-360) between two grids. Used for the Heard
List columns and for the future "where is this station relative to me"
displays.

We work from the *center* of each grid, not its origin corner. That
matches operator intuition ("X is 350 miles east of me") and reduces
the worst-case error to ~half a grid square — about 50 km for 4-char
grids, ~5 km for 6-char.

Distance precision is intentionally limited: 6-char grids resolve to
~5 km (the subsquare cell is 2.5' lat × 5' lon, ~5×7 km at mid-latitude).
Reporting fractional miles past one decimal place is fake precision.
"""

from __future__ import annotations

import math
from typing import Optional

EARTH_RADIUS_MI = 3958.7613
EARTH_RADIUS_KM = 6371.0088


def grid_to_latlon_center(grid: str) -> tuple[float, float]:
    """Return the (lat, lon) at the *center* of a grid string.

    Accepts 4 or 6-character Maidenhead grids. Raises ValueError on
    malformed input — caller is expected to validate first (we use the
    config validators) so this is a defensive last line.
    """
    g = grid.strip()
    if len(g) not in (4, 6):
        raise ValueError(f"grid must be 4 or 6 chars, got {grid!r}")

    # Field pair (uppercase A-R)
    field_lon = ord(g[0].upper()) - ord("A")
    field_lat = ord(g[1].upper()) - ord("A")
    if not (0 <= field_lon < 18 and 0 <= field_lat < 18):
        raise ValueError(f"grid field pair out of range: {grid!r}")

    # Square pair (digits)
    sq_lon = int(g[2])
    sq_lat = int(g[3])

    # Start at the SW corner of the square
    lon = -180.0 + field_lon * 20.0 + sq_lon * 2.0
    lat = -90.0 + field_lat * 10.0 + sq_lat * 1.0

    if len(g) == 6:
        ss_lon = ord(g[4].lower()) - ord("a")
        ss_lat = ord(g[5].lower()) - ord("a")
        if not (0 <= ss_lon < 24 and 0 <= ss_lat < 24):
            raise ValueError(f"grid subsquare out of range: {grid!r}")
        lon += ss_lon * (2.0 / 24.0)
        lat += ss_lat * (1.0 / 24.0)
        # Center of the 5'×2.5' subsquare
        lon += (2.0 / 24.0) / 2.0
        lat += (1.0 / 24.0) / 2.0
    else:
        # Center of the 2°×1° square
        lon += 1.0
        lat += 0.5
    return lat, lon


def haversine_distance_miles(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in miles."""
    return _haversine(lat1, lon1, lat2, lon2) * EARTH_RADIUS_MI


def haversine_distance_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in kilometers."""
    return _haversine(lat1, lon1, lat2, lon2) * EARTH_RADIUS_KM


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Returns angular distance in radians; multiply by Earth radius."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def initial_bearing_deg(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Initial true bearing from (lat1, lon1) toward (lat2, lon2)."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def distance_and_bearing(
    our_grid: Optional[str],
    their_grid: Optional[str],
    *,
    units: str = "miles",
) -> tuple[Optional[float], Optional[float]]:
    """Compute (distance, bearing) between two grids.

    Returns (None, None) if either grid is missing or invalid — the
    Heard List shows "---" in that case.
    """
    if not our_grid or not their_grid:
        return None, None
    try:
        lat1, lon1 = grid_to_latlon_center(our_grid)
        lat2, lon2 = grid_to_latlon_center(their_grid)
    except ValueError:
        return None, None
    if units == "km":
        dist = haversine_distance_km(lat1, lon1, lat2, lon2)
    else:
        dist = haversine_distance_miles(lat1, lon1, lat2, lon2)
    bearing = initial_bearing_deg(lat1, lon1, lat2, lon2)
    return dist, bearing
