"""Type definitions for the JS8 protocol layer.

Frozen dataclasses passed between threads. The decoder thread emits
``DecodedFrame`` from the C++ wrapper, the protocol parser produces
``HeardStation`` and ``DirectedMessage`` records, and the store +
UI consume those.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class FrameKind(enum.Enum):
    """High-level classification of an incoming JS8 frame.

    Determines which UI screen, if any, surfaces the frame and how
    the operator can respond. ACK is its own kind because it short-
    circuits the retry state machine in Step 6.
    """

    HEARTBEAT = "HEARTBEAT"           # "<call> @HB <grid>"
    CQ = "CQ"                          # "<call>: CQ <grid>" or "@ALLCALL CQ"
    DIRECTED_MESSAGE = "DIRECTED"      # "<from>: <to> <text>"
    DIRECTED_QUERY = "QUERY"           # "<from>: <to> <CMD>?"
    DIRECTED_COMMAND = "COMMAND"       # "<from>: <to> <CMD> <ARG>"
    ACK = "ACK"                        # "<from>: <to> ACK"
    ALLCALL = "ALLCALL"                # "<from>: @ALLCALL <text>"
    UNKNOWN = "UNKNOWN"                # parser couldn't classify


@dataclass(frozen=True)
class DecodedFrame:
    """Raw frame as the GFSK8 decoder gave it to us.

    All fields mirror the C++ ``gfsk8::Decoded`` struct plus a
    ``received_at`` timestamp for our retention/UI logic.
    """

    text: str           # Varicode-unpacked human-readable
    raw: str            # 12-char raw payload (for forensic logging)
    snr_db: int
    frequency_hz: float
    dt_seconds: float
    submode: int        # 0=Normal, 1=Fast, 2=Turbo, 3=Slow, 4=Ultra
    quality: int
    frame_type: int
    utc_seconds_of_day: int  # the slot we decoded
    received_at: float       # Unix epoch when frame finished decoding


@dataclass(frozen=True)
class ParsedFrame:
    """A DecodedFrame after the protocol layer has classified it."""

    decoded: DecodedFrame
    kind: FrameKind
    from_call: str | None         # sender callsign (None if unparseable)
    to_call: str | None            # recipient (or @ALLCALL / @HB)
    grid: str | None               # sender's grid if heartbeat / CQ
    body: str                      # remainder of the message after FROM/TO
    is_for_us: bool                # ``to_call`` matches our callsign


@dataclass(frozen=True)
class HeardStation:
    """One row of the Heard List.

    The Heard List shows the *most recent* sighting of each callsign,
    so when the store inserts a new sighting of an already-heard
    callsign we update last_heard, snr_db, grid, frequency_hz, distance_mi,
    bearing_deg with the new values.
    """

    callsign: str
    snr_db: int
    grid: str | None
    frequency_hz: float
    distance_mi: float | None    # None if our grid or theirs is missing
    bearing_deg: float | None
    last_heard: float            # Unix epoch
