"""Typed GPS fix data.

The reader thread emits ``GpsFix`` snapshots into UIState; renderers
read them. ``GpsFix`` is frozen so it's safe to share across threads
without locking.

Per spec §6.1.1, the home screen shows GPS state in plain language:
no fix / 2D / 3D / fix-quality color. The fix kind enum maps to those
display states.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class FixKind(enum.IntEnum):
    """How good is this fix.

    Values mirror gpsd's TPV.mode field (NMEA 0183 GGA fix-quality):
        0 = unknown
        1 = no fix
        2 = 2D fix (lat/lon valid, alt unreliable)
        3 = 3D fix (lat/lon/alt valid)
    """

    UNKNOWN = 0
    NO_FIX = 1
    FIX_2D = 2
    FIX_3D = 3


@dataclass(frozen=True)
class GpsFix:
    """Snapshot of the most recent GPS data we have."""

    kind: FixKind
    lat: float | None
    lon: float | None
    altitude_m: float | None
    speed_mps: float | None
    track_deg: float | None
    hdop: float | None
    # UTC time of fix as Unix epoch seconds. None if not yet known.
    fix_time: float | None
    # Number of satellites used in the fix (None if not reported).
    satellites_used: int | None
    # Wall-clock monotonic timestamp when this fix was received by the
    # daemon. Used to age the displayed value ("fix is 12 s old").
    received_at: float

    @property
    def has_position(self) -> bool:
        """True if lat/lon are valid (i.e. 2D or 3D fix)."""
        return (
            self.kind in (FixKind.FIX_2D, FixKind.FIX_3D)
            and self.lat is not None
            and self.lon is not None
        )


# Sentinel "no fix yet" snapshot.
def no_fix(now: float) -> GpsFix:
    return GpsFix(
        kind=FixKind.NO_FIX,
        lat=None, lon=None, altitude_m=None,
        speed_mps=None, track_deg=None, hdop=None,
        fix_time=None, satellites_used=None,
        received_at=now,
    )
