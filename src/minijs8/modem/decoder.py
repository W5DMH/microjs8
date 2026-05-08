"""JS8 decode thread.

Responsibilities:

  - Wait until the next 15-second JS8 slot boundary (UTC-aligned).
  - Read a snapshot of the 60-second audio ring buffer.
  - Hand it to ``gfsk8.Decoder.decode()`` along with the slot's UTC
    seconds-of-day. The decoder's callback fires once per detected
    frame; we collect those and pass them to a higher-level callback
    that lives in the asyncio thread.
  - Repeat.

Slot timing
-----------

JS8 Normal mode uses 15-second slots aligned to UTC. The slot boundary
is whenever ``(UTC seconds since midnight) % 15 == 0``, i.e. 00:00:00,
00:00:15, 00:00:30, etc. We sleep until just past the boundary, snapshot
the buffer, then call decode.

Slot timing depends on accurate system time. With chrony+GPS (Step 4)
or chrony+NTP (when networked), we have ±50 ms time accuracy — plenty
for JS8's ±1 s tolerance. If chrony hasn't yet disciplined the clock,
decodes will fail until it does; the daemon doesn't crash, just
produces no output until time is right.

Threading
---------

The decode thread holds the GIL when it's not inside ``decoder.decode()``.
The wrapper releases the GIL inside the C++ decode loop, so the asyncio
loop and render thread are free to run during the (~1-2 second) decode
work. On Pi Zero 2W this is the heaviest single CPU consumer in the
daemon — roughly one core's worth for the duration.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional, Protocol

import numpy as np

from minijs8.protocol.types import DecodedFrame

_log = logging.getLogger(__name__)

# JS8 slot length in seconds. Normal mode = 15. Other submodes have
# different slot lengths but Normal is the dominant traffic mode and
# we decode all 5 submodes from the same 60-s buffer.
JS8_SLOT_SECONDS = 15

# How long after the slot boundary to sleep before decoding. The
# decoder needs the FULL 15 seconds of audio for the slot, so we
# decode ~0.5 s after the slot ENDS (i.e. 0.5 s after the next
# boundary), which means our snapshot is guaranteed to contain the
# completed slot's audio.
_DECODE_LAG_S = 0.5


class _DecoderProto(Protocol):
    """The subset of gfsk8.Decoder we use."""

    def decode(
        self, audio: np.ndarray, nutc: int, callback: Any
    ) -> None: ...


# Type alias for the asyncio-side callback we invoke per decoded frame.
FrameCallback = Callable[[DecodedFrame], None]


class DecodeThread(threading.Thread):
    """Slot-boundary-driven JS8 decoder.

    Construct with the audio capture (for snapshots), an asyncio loop
    (for marshaling the decoded callback), and a ``on_frame`` callback
    that runs on the asyncio thread.

    The decoder factory is injected for testability. In production it
    constructs ``gfsk8.Decoder(gfsk8.AllSubmodes)``.
    """

    def __init__(
        self,
        audio_capture,
        loop: asyncio.AbstractEventLoop,
        on_frame: FrameCallback,
        *,
        decoder_factory: Optional[Callable[[], _DecoderProto]] = None,
        name: str = "js8-decode",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._audio = audio_capture
        self._loop = loop
        self._on_frame = on_frame
        self._decoder_factory = decoder_factory
        self._stop_event = threading.Event()
        self._decoder: Optional[_DecoderProto] = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        _log.info("decode thread starting (slot=%ds)", JS8_SLOT_SECONDS)
        try:
            self._decoder = self._build_decoder()
        except Exception:
            _log.exception(
                "could not construct gfsk8.Decoder — decode thread exiting. "
                "Check that the .so is installed in the venv."
            )
            return

        while not self._stop_event.is_set():
            # Wait until the next slot boundary plus a small lag.
            sleep_s = self._seconds_until_next_decode()
            self._stop_event.wait(sleep_s)
            if self._stop_event.is_set():
                break
            self._decode_one_slot()
        _log.info("decode thread stopping")

    # ── Slot timing ──────────────────────────────────────────────────

    def _seconds_until_next_decode(self) -> float:
        """Seconds to sleep until we should call decode().

        We decode at slot_end + _DECODE_LAG_S, which is the same as
        the next slot's start + _DECODE_LAG_S.
        """
        now = time.time()
        # Seconds past the most recent slot boundary.
        past = now % JS8_SLOT_SECONDS
        # Seconds until the next boundary.
        next_boundary = JS8_SLOT_SECONDS - past
        return next_boundary + _DECODE_LAG_S

    def _current_nutc(self) -> int:
        """JS8's nutc parameter is UTC seconds-of-day, slot-aligned.

        The decoder uses nutc to pick the proper FFT alignment; it
        should be the START of the slot we're decoding.
        """
        # We're called RIGHT AFTER a slot ends, so the slot we want
        # to decode is the one that just ENDED.
        now_utc = time.gmtime()
        seconds_of_day_now = (
            now_utc.tm_hour * 3600 + now_utc.tm_min * 60 + now_utc.tm_sec
        )
        # The most recent slot boundary, BEFORE we crossed into the
        # current slot. Subtract _DECODE_LAG_S worth of seconds (so we
        # reliably index the just-ended slot's start, not the upcoming).
        # Round DOWN to the slot boundary that began the slot we just
        # captured.
        prev_boundary = (
            (seconds_of_day_now - 1) // JS8_SLOT_SECONDS
        ) * JS8_SLOT_SECONDS
        return max(0, prev_boundary)

    # ── Decode dispatch ──────────────────────────────────────────────

    def _decode_one_slot(self) -> None:
        """Snapshot audio and run the decoder once."""
        audio = self._audio.snapshot()
        nutc = self._current_nutc()

        # Collect frames in a list during the synchronous decode call;
        # forward each to the asyncio loop after the decoder returns.
        # (gfsk8.Decoder may invoke the callback from its own internal
        # threads if it parallelizes, so we collect first and dispatch
        # once we're back on a single thread.)
        collected: list[DecodedFrame] = []
        received_at = time.time()

        def on_decoded(d: Any) -> None:
            try:
                frame = DecodedFrame(
                    text=d.text,
                    raw=getattr(d, "message", "") or "",
                    snr_db=int(d.snr_db),
                    frequency_hz=float(d.frequency_hz),
                    dt_seconds=float(d.dt_seconds),
                    submode=int(d.submode),
                    quality=int(d.quality),
                    frame_type=int(d.frame_type),
                    utc_seconds_of_day=nutc,
                    received_at=received_at,
                )
                collected.append(frame)
            except Exception:
                _log.exception("error converting decoded frame")

        try:
            assert self._decoder is not None
            self._decoder.decode(audio, nutc, on_decoded)
        except Exception:
            _log.exception("decoder.decode() raised")
            return

        if collected:
            _log.info(
                "decoded %d frame(s) in slot starting at %d UTC seconds-of-day",
                len(collected), nutc,
            )

        # Marshal each frame onto the asyncio thread.
        for frame in collected:
            try:
                self._loop.call_soon_threadsafe(self._on_frame, frame)
            except RuntimeError:
                # Loop closed during shutdown.
                return

    def _build_decoder(self) -> _DecoderProto:
        if self._decoder_factory is not None:
            return self._decoder_factory()
        # Lazy-import gfsk8 so host-side tests don't need the .so.
        import gfsk8  # type: ignore[import-not-found]
        return gfsk8.Decoder(gfsk8.AllSubmodes)
