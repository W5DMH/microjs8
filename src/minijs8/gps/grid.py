"""Maidenhead grid locator conversion.

Converts (lat, lon) decimal-degrees into a 4 or 6-character Maidenhead
locator. The math is well-known and small but the index/wrap logic is
easy to get wrong — see https://en.wikipedia.org/wiki/Maidenhead_Locator_System

Convention used here:
  - lat/lon in **decimal degrees**, lat ∈ [-90, 90], lon ∈ [-180, 180]
  - First field pair (uppercase A-R) covers 20° lon × 10° lat
  - Square pair (digits 0-9) covers 2° lon × 1° lat within the field
  - Subsquare pair (lowercase a-x) covers 5' lon × 2.5' lat within the square

The 8-character extended (extended-square pair, digits) is not produced —
JS8 traffic universally uses the 4 or 6 character forms.
"""

from __future__ import annotations


# Grid system anchors at (-180°, -90°) — the south-pole, antimeridian
# corner. All offsets are computed from there.
_LON_ORIGIN = -180.0
_LAT_ORIGIN = -90.0

# Field / square / subsquare cell sizes in degrees.
_FIELD_LON = 20.0
_FIELD_LAT = 10.0
_SQUARE_LON = 2.0
_SQUARE_LAT = 1.0
_SUBSQUARE_LON = _SQUARE_LON / 24.0   # 5 minutes
_SUBSQUARE_LAT = _SQUARE_LAT / 24.0   # 2.5 minutes


def latlon_to_grid(lat: float, lon: float, *, precision: int = 6) -> str:
    """Convert decimal-degree (lat, lon) to a Maidenhead grid string.

    ``precision`` is 4 (FN42) or 6 (FN42aa). 6 is the JS8 default.

    Edge cases:
      - Inputs are clamped to valid ranges (lat ∈ [-90, 90], lon ∈ [-180, 180]).
        We clamp rather than raise because GPS fix data can momentarily
        report out-of-range values during cold-start or with bad
        almanac data, and the daemon should keep running cleanly.
      - The grid for exactly 180.0 lon (= -180.0) is "RR.." (the last
        possible field), not the wraparound to "AA..".
    """
    if precision not in (4, 6):
        raise ValueError(f"precision must be 4 or 6, got {precision}")

    # Clamp without warning — GPS noise should not raise here.
    lat = max(-90.0, min(90.0, lat))
    lon = max(-180.0, min(180.0, lon))

    # Field pair (uppercase). Lon advances faster than lat (each
    # field is 20° lon × 10° lat), so divide by 20 / 10.
    lon_off = lon - _LON_ORIGIN
    lat_off = lat - _LAT_ORIGIN

    field_lon = int(lon_off // _FIELD_LON)
    field_lat = int(lat_off // _FIELD_LAT)
    # The very-corner case: at lon=180 exactly, field_lon would be 18
    # (out of valid 0-17). Pull back into the last cell.
    field_lon = min(field_lon, 17)
    field_lat = min(field_lat, 17)

    grid = chr(ord("A") + field_lon) + chr(ord("A") + field_lat)

    # Square pair (digits). 10 squares per field in each axis.
    sq_lon_off = (lon_off - field_lon * _FIELD_LON)
    sq_lat_off = (lat_off - field_lat * _FIELD_LAT)
    sq_lon = int(sq_lon_off // _SQUARE_LON)
    sq_lat = int(sq_lat_off // _SQUARE_LAT)
    sq_lon = min(sq_lon, 9)
    sq_lat = min(sq_lat, 9)
    grid += f"{sq_lon}{sq_lat}"

    if precision == 4:
        return grid

    # Subsquare pair (lowercase a-x, 24 per square in each axis).
    ss_lon_off = sq_lon_off - sq_lon * _SQUARE_LON
    ss_lat_off = sq_lat_off - sq_lat * _SQUARE_LAT
    ss_lon = int(ss_lon_off // _SUBSQUARE_LON)
    ss_lat = int(ss_lat_off // _SUBSQUARE_LAT)
    ss_lon = min(ss_lon, 23)
    ss_lat = min(ss_lat, 23)
    grid += chr(ord("a") + ss_lon) + chr(ord("a") + ss_lat)
    return grid
