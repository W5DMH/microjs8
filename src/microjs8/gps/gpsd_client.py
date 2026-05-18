"""Minimal gpsd JSON-protocol client.

gpsd accepts TCP connections on port 2947 and emits line-delimited JSON.
Sending ``?WATCH={"enable":true,"json":true}`` opens the firehose. We
care about ``TPV`` reports (time, position, velocity); ``SKY`` reports
(satellite info) we use only to populate the satellites_used field.

We deliberately avoid the ``gpsd-py3`` PyPI package because it's
unmaintained (last release 2017) and is a thin wrapper around the same
JSON socket we can implement in 50 lines of stdlib. Fewer dependencies
== fewer aarch64 wheel headaches at image-build time.

Reference: https://gpsd.io/gpsd_json.html
"""

from __future__ import annotations

import json
import logging
import math
import socket
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

from microjs8.gps.types import FixKind, GpsFix

_log = logging.getLogger(__name__)

GPSD_DEFAULT_HOST = "127.0.0.1"
GPSD_DEFAULT_PORT = 2947

# WGS84 ellipsoid constants (the geodetic reference frame GPS uses).
# Reference: NIMA Technical Report TR8350.2 (DoD WGS84 definition).
#   a   = semi-major (equatorial) axis in metres
#   f   = flattening
#   e2  = first eccentricity squared = 2f - f²
#   b   = semi-minor (polar) axis = a(1-f)
# These constants are exact by definition of the WGS84 datum; we keep
# them as module-level finals so callers don't recompute on each fix.
_WGS84_A: float = 6_378_137.0
_WGS84_F: float = 1.0 / 298.257223563
_WGS84_E2: float = 2 * _WGS84_F - _WGS84_F * _WGS84_F
_WGS84_B: float = _WGS84_A * (1.0 - _WGS84_F)


def _ecef_to_lla(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert ECEF (m) to geodetic (lat deg, lon deg, alt m).

    Bowring 1976 closed-form solution — converges in one shot for any
    point above the surface, no iteration required. Used as a
    workaround when gpsd 3.22 reports ``lat=0, lon=0`` in TPV records
    while the ECEF position is correct (observed with u-blox 7
    firmware PROTVER 14.00 — W5DMH bench, May 2026; gpsmon shows
    the proper coordinates so the receiver is computing them, gpsd
    just isn't surfacing them in the JSON output).

    The Bowring algorithm is the industry-standard closed-form
    geodetic conversion: accurate to better than 1 cm for any
    point within several hundred km of the surface, with no
    iteration. Returns lat/lon in degrees, altitude in metres
    above the WGS84 ellipsoid.

    Special cases:
      - At the poles (``p == 0``), longitude is undefined and we
        return 0; latitude is ±90 depending on the sign of z.
      - At the Earth's centre (all three zero), returns (0, 0, -a)
        — nonsense but doesn't raise.
    """
    p = math.sqrt(x * x + y * y)
    if p == 0.0:
        # On the polar axis: longitude undefined, lat is ±90.
        lat = math.copysign(math.pi / 2, z)
        return math.degrees(lat), 0.0, abs(z) - _WGS84_B
    # Bowring's reduced-latitude formulation.
    e_prime_sq = (_WGS84_A * _WGS84_A - _WGS84_B * _WGS84_B) / (_WGS84_B * _WGS84_B)
    theta = math.atan2(z * _WGS84_A, p * _WGS84_B)
    sin_theta = math.sin(theta)
    cos_theta = math.cos(theta)
    lat_rad = math.atan2(
        z + e_prime_sq * _WGS84_B * sin_theta * sin_theta * sin_theta,
        p - _WGS84_E2 * _WGS84_A * cos_theta * cos_theta * cos_theta,
    )
    lon_rad = math.atan2(y, x)
    sin_lat = math.sin(lat_rad)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    alt = p / math.cos(lat_rad) - n
    return math.degrees(lat_rad), math.degrees(lon_rad), alt

# Watch command to start the JSON firehose.
_WATCH_REQUEST = b'?WATCH={"enable":true,"json":true}\n'

# Read buffer for JSON lines. gpsd lines are typically <500 bytes;
# 4 KiB is a comfortable upper bound.
_RECV_BUFSIZE = 4096

# Connect timeout (gpsd is local; if it's down longer than this,
# something is wrong and we should surface that).
_CONNECT_TIMEOUT_S = 3.0
# socket.recv timeout — we want to wake periodically to check the
# stop event in the calling thread, similar to the keyboard pattern.
_RECV_TIMEOUT_S = 0.5


def _parse_iso8601_utc(s: str) -> Optional[float]:
    """gpsd emits ISO 8601 UTC timestamps like '2026-04-28T18:00:00.000Z'."""
    try:
        # Python's fromisoformat accepts +00:00 but not 'Z' until 3.11+.
        # We're 3.11+, so fromisoformat handles it directly.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _tpv_to_fix(tpv: dict, now: float, satellites_used: Optional[int]) -> GpsFix:
    """Translate a gpsd TPV record into our GpsFix dataclass.

    Filters out "null island" (lat=0, lon=0) coordinates — gpsd
    sometimes emits these as a placeholder during fix acquisition,
    before the receiver has actually computed a position. The TPV's
    ``mode`` field may already say 3 (FIX_3D) at that point, which
    misleadingly looks like "we have a 3D fix" downstream.

    Real-world impact (W5DMH bench, May 2026): the EMERGENCY screen
    was rendering "+0.0000, +0.0000" because the parser passed
    through gpsd's null-island intermediate report, while the HOME
    screen showed "3D fix (6 sat)" purely on the mode field. Filter
    these out at the source so downstream consumers see the fix as
    "kind = whatever gpsd said, position = unknown" — which renders
    as "acquiring" instead of false coordinates.

    A legitimate position at (0.0, 0.0) — middle of the Atlantic
    off the African coast — is not a realistic operating location
    for amateur radio (international waters, no jurisdiction), so
    this filter has effectively zero false positives.
    """
    mode = tpv.get("mode", 0)
    try:
        kind = FixKind(mode)
    except ValueError:
        kind = FixKind.UNKNOWN

    lat = tpv.get("lat")
    lon = tpv.get("lon")
    alt = tpv.get("altMSL", tpv.get("alt"))

    # Null-island sentinel: gpsd reports both coordinates exactly
    # zero. This is gpsd 3.22's bug with u-blox 7 PROTVER 14.00 —
    # the receiver computes a real ECEF position but gpsd's geodetic
    # conversion fails silently and emits zeros in the lat/lon
    # fields. Detect this and fall back to deriving lat/lon from
    # the ECEF coordinates that ARE in the TPV record, using
    # Bowring's closed-form WGS84 conversion. Verified against
    # gpsmon's own LTP-Pos output on the W5DMH bench:
    #   ECEF (539473.15, -4619509.83, 4350310.41)
    #     → (43.2794°N, -83.3391°W) (matches gpsmon to 4 decimals)
    if lat == 0.0 and lon == 0.0:
        ecef_x = tpv.get("ecefx")
        ecef_y = tpv.get("ecefy")
        ecef_z = tpv.get("ecefz")
        if (
            ecef_x is not None
            and ecef_y is not None
            and ecef_z is not None
            and not (ecef_x == 0.0 and ecef_y == 0.0 and ecef_z == 0.0)
        ):
            lat, lon, derived_alt = _ecef_to_lla(ecef_x, ecef_y, ecef_z)
            # Prefer the gpsd altMSL value when sensible; ECEF-derived
            # altitude is height above the WGS84 ellipsoid (HAE), not
            # mean sea level — for emergency use either is acceptable
            # but altMSL is what operators expect. Fall back to the
            # ECEF-derived altitude only when gpsd's value is the
            # nonsense -17 m default that accompanies the null-island
            # bug (altMSL = altHAE - geoidSep, and gpsd sets
            # altHAE = 0 in this state).
            if alt is None or (-50.0 < alt < 0.0):
                alt = derived_alt
        else:
            # No ECEF data either — genuine null-island. Treat as
            # missing position so downstream renderers show
            # "acquiring" instead of bogus coordinates.
            lat = None
            lon = None

    return GpsFix(
        kind=kind,
        lat=lat,
        lon=lon,
        altitude_m=alt,
        speed_mps=tpv.get("speed"),
        track_deg=tpv.get("track"),
        hdop=None,  # populated by SKY reports
        fix_time=_parse_iso8601_utc(tpv["time"]) if "time" in tpv else None,
        satellites_used=satellites_used,
        received_at=now,
    )


class GpsdClient:
    """Connect to gpsd and yield GpsFix snapshots.

    Construct, then call ``stream(stop_event)`` in a thread; it yields
    every TPV report until stop_event is set or the connection drops.
    On any socket error the client cleans up and returns; the caller is
    responsible for reconnect logic (the reader thread does this).

    Why a class and not a function: we need to hold the socket and a
    little parsing state (last-seen satellites_used from SKY reports) —
    cleaner as instance fields than as closure variables.
    """

    def __init__(
        self,
        host: str = GPSD_DEFAULT_HOST,
        port: int = GPSD_DEFAULT_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._satellites_used: Optional[int] = None
        # Buffer for partial JSON lines split across recv() calls.
        self._line_buf: bytes = b""
        # Last good position observed from any TPV in this session.
        #
        # gpsd emits TPV reports at a higher frequency than the
        # receiver computes new positions. Many of those reports
        # carry only status updates (mode change, satellite count,
        # time) and omit lat/lon entirely — or send the null-island
        # placeholder. Without this caching layer, every such
        # intermediate report would blank the UI's last-known
        # position; the operator sees "acquiring" flicker on/off
        # depending on which TPV arrived most recently.
        #
        # We preserve the last good lat/lon across intermediate
        # reports. The cache clears only when gpsd reports NO_FIX
        # (kind=1) — meaning the receiver explicitly lost lock.
        # Power-cycling the daemon or reconnecting to gpsd also
        # clears it (the client instance is rebuilt by reader.py
        # on reconnect, so __init__ re-runs).
        #
        # Observed on-air (W5DMH bench, May 2026): a receiver in a
        # known-locked state shows position updates every few
        # seconds in NMEA, but gpsd's TPV cadence is faster — the
        # majority of TPVs omit position fields. Without caching,
        # HOME flickered "acquiring (5 sat)" / "3D fix (6 sat)" as
        # consecutive reports arrived.
        self._cached_lat: Optional[float] = None
        self._cached_lon: Optional[float] = None

    def connect(self) -> None:
        """Open the TCP connection and send the WATCH request.

        Raises socket.error / ConnectionRefusedError / TimeoutError
        on failure — caller catches.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT_S)
        sock.connect((self._host, self._port))
        sock.sendall(_WATCH_REQUEST)
        # Switch to a shorter recv timeout for the streaming phase.
        sock.settimeout(_RECV_TIMEOUT_S)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._line_buf = b""
        self._satellites_used = None
        # Drop cached position too — a reconnect implies a fresh
        # session and we shouldn't carry stale coordinates across
        # gpsd disconnects.
        self._cached_lat = None
        self._cached_lon = None

    def stream(self, stop_event) -> Iterator[GpsFix]:
        """Yield GpsFix records as they arrive.

        ``stop_event`` is a threading.Event; the loop checks it between
        recv calls so the thread can shut down within ~0.5 s.

        On socket error the iterator returns; caller is expected to
        ``close()`` and (if desired) reconnect.
        """
        if self._sock is None:
            raise RuntimeError("call connect() before stream()")

        while not stop_event.is_set():
            try:
                chunk = self._sock.recv(_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError as exc:
                _log.info("gpsd socket error: %s", exc)
                return
            if not chunk:
                # Peer closed cleanly.
                _log.info("gpsd closed the connection")
                return

            self._line_buf += chunk
            while b"\n" in self._line_buf:
                line, _, self._line_buf = self._line_buf.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                fix = self._parse_line(line)
                if fix is not None:
                    yield fix

    def _parse_line(self, line: bytes) -> Optional[GpsFix]:
        """Parse one JSON line. TPV → GpsFix; SKY → updates sat count
        and returns None; everything else returns None.

        Position caching: when a TPV carries valid lat/lon, update
        the cache. When a TPV carries no lat/lon (or the null-island
        placeholder), backfill from the cache if available — so the
        UI sees a continuous position rather than flickering on/off
        with each intermediate TPV. When a TPV reports NO_FIX, clear
        the cache (receiver explicitly lost lock).

        Malformed JSON is logged once at DEBUG and dropped — gpsd is
        reliable enough that this should never happen, but we don't
        want a single bad line to kill the stream.
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.debug("gpsd JSON decode error: %s on line %r", exc, line[:80])
            return None

        cls = obj.get("class")
        if cls == "TPV":
            # Diagnostic — captures the exact fields gpsd sends so we
            # can debug "GPS locked but UI shows acquiring" reports.
            # DEBUG-level: only enabled with ``--log-level DEBUG`` so
            # the journal isn't polluted with ~1 TPV/sec messages
            # in steady-state operation. The W5DMH bench used this
            # at INFO level temporarily to diagnose the gpsd 3.22
            # null-island-on-u-blox-7 bug; once the ECEF fallback
            # below was confirmed working, we demoted to DEBUG.
            _log.debug(
                "gpsd TPV: mode=%s lat=%r lon=%r ecef=(%r,%r,%r) sats=%r",
                obj.get("mode"), obj.get("lat"), obj.get("lon"),
                obj.get("ecefx"), obj.get("ecefy"), obj.get("ecefz"),
                self._satellites_used,
            )
            fix = _tpv_to_fix(obj, time.monotonic(), self._satellites_used)
            # Update cache if this TPV has a real position; backfill
            # the returned fix if it doesn't.
            if fix.lat is not None and fix.lon is not None:
                self._cached_lat = fix.lat
                self._cached_lon = fix.lon
            elif (
                fix.kind in (FixKind.FIX_2D, FixKind.FIX_3D)
                and self._cached_lat is not None
                and self._cached_lon is not None
            ):
                # Intermediate TPV without position, but the receiver
                # still claims a fix — backfill the last good lat/lon
                # so the UI doesn't flicker. We rebuild the GpsFix
                # since it's frozen.
                from dataclasses import replace
                fix = replace(
                    fix,
                    lat=self._cached_lat,
                    lon=self._cached_lon,
                )
            elif fix.kind is FixKind.NO_FIX:
                # Receiver explicitly lost lock — drop the cache.
                self._cached_lat = None
                self._cached_lon = None
            return fix
        if cls == "SKY":
            # Count satellites with .used == True
            sats = obj.get("satellites") or []
            used = sum(1 for s in sats if s.get("used"))
            self._satellites_used = used if sats else None
            return None
        # VERSION / DEVICES / WATCH responses — ignored.
        return None
