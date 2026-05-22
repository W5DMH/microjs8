"""Heartbeat + emergency beacon scheduler.

Two long-running timer threads. Both write to the same ``OutboundQueue``
that the scheduler reads from. The scheduler itself doesn't know
about heartbeat or emergency beacons — it just sees QUEUED rows.

Heartbeat (Step 6 spec — your locked answer):
  - Default 30 min interval
  - Immediate transmit when first toggled on (operator gets feedback)
  - Random slot offset per cycle (politer on a busy band; collisions
    with other stations doing 30-min beacons get spread out)
  - Toggling off lets in-flight TX complete; cancels future slots only.

Emergency beacon (Step 6 spec — your locked answer):
  - Every 5 min until power-off
  - No manual cancel — operator can only stop it by rebooting
  - Begins only when the operator has armed it on the EMERGENCY screen
    AND has GPS lock (not in this module — gating happens in app.py
    before we're started).

Both use a shared base class to keep the lifecycle handling DRY.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Optional

from microjs8.tx.queue import OutboundKind, OutboundQueue

_log = logging.getLogger(__name__)


# Heartbeat config (Step 6 locked decisions).
HEARTBEAT_INTERVAL_S = 30 * 60       # 30 minutes
# Random slot offset added to the 30-minute interval. Spread is up to
# 1 minute (4 JS8 slots), small enough that "30 min interval" is
# still accurate but enough to break up beacon collisions.
HEARTBEAT_RANDOM_OFFSET_S = 60

# Emergency beacon config.
EMERGENCY_BEACON_INTERVAL_S = 3 * 60  # 3 minutes — operator-spec May 2026


class _BaseBeacon(threading.Thread):
    """Shared lifecycle for heartbeat and emergency beacons."""

    def __init__(
        self,
        *,
        queue: OutboundQueue,
        message_kind: OutboundKind,
        message_factory: Callable[[], Optional[str]],
        interval_s: float,
        random_offset_s: float = 0.0,
        immediate_on_start: bool = False,
        single_shot: bool = False,
        on_complete: Optional[Callable[[], None]] = None,
        on_fire: Optional[Callable[[str], None]] = None,
        name: str = "beacon",
    ) -> None:
        """
        ``message_factory`` is called every interval to produce the
        text to enqueue. Returns None to skip this cycle (e.g., grid
        not yet known). The factory is called from the beacon thread,
        not from a UI thread.

        ``single_shot`` makes the beacon fire once (assuming
        ``immediate_on_start`` is also True) and then exit cleanly.
        Used for ``HbMode.SINGLE`` — one @HB in the next aligned TX
        window, then revert. When the single-shot completes, the
        ``on_complete`` callback (if provided) is invoked from the
        beacon thread so the app can flip the UI's mode back to OFF.

        ``on_fire`` (v0.0.10) is called with the wire text after a
        successful enqueue. The app uses this to log outbound
        heartbeats to the DIRECTED activity feed so the operator
        sees their own beacons alongside received traffic. Called
        from the beacon thread; the callback should marshal back to
        the asyncio thread if it touches anything thread-unsafe.
        """
        super().__init__(name=name, daemon=True)
        self._queue = queue
        self._kind = message_kind
        self._make_message = message_factory
        self._interval_s = interval_s
        self._random_offset_s = random_offset_s
        self._immediate_on_start = immediate_on_start
        self._single_shot = single_shot
        self._on_complete = on_complete
        self._on_fire = on_fire
        self._stop_event = threading.Event()
        self._fire_count = 0

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def fire_count(self) -> int:
        """Number of times we've successfully queued a message."""
        return self._fire_count

    def run(self) -> None:
        _log.info(
            "beacon %s starting (interval=%.0fs, immediate=%s, single_shot=%s)",
            self.name, self._interval_s, self._immediate_on_start,
            self._single_shot,
        )
        if self._immediate_on_start:
            self._fire_one()
            if self._single_shot:
                _log.info(
                    "beacon %s: single-shot complete, stopping", self.name
                )
                self._fire_on_complete()
                return
        while not self._stop_event.is_set():
            sleep_s = self._next_sleep_seconds()
            self._stop_event.wait(sleep_s)
            if self._stop_event.is_set():
                break
            self._fire_one()
        _log.info("beacon %s stopped", self.name)

    def _fire_on_complete(self) -> None:
        """Invoke the on_complete callback. Safe — exceptions logged
        but never propagated up; the beacon thread is already exiting
        regardless."""
        if self._on_complete is None:
            return
        try:
            self._on_complete()
        except Exception:
            _log.exception(
                "beacon %s: on_complete callback raised", self.name
            )

    def _next_sleep_seconds(self) -> float:
        """How long until the next fire."""
        if self._random_offset_s > 0:
            jitter = random.uniform(0, self._random_offset_s)
        else:
            jitter = 0.0
        return self._interval_s + jitter

    def _fire_one(self) -> None:
        """Build a message and enqueue it. Skips silently if factory
        returns None (e.g. heartbeat without grid)."""
        try:
            text = self._make_message()
        except Exception:
            _log.exception("beacon message factory raised")
            return
        if text is None:
            _log.debug("beacon %s: factory returned None, skipping", self.name)
            return
        msg_id = self._queue.enqueue_for_encoding(
            text, self._kind, to_call=None,
        )
        if msg_id is None:
            _log.warning("beacon %s: queue full, dropping this cycle", self.name)
            return
        self._fire_count += 1
        _log.info("beacon %s: queued message id=%d text=%r",
                  self.name, msg_id, text)
        # v0.0.10: notify the app so it can log this outgoing
        # broadcast to the directed-activity feed. Callback runs on
        # the beacon thread — implementations are responsible for
        # marshalling back to the asyncio loop if needed.
        if self._on_fire is not None:
            try:
                self._on_fire(text)
            except Exception:
                _log.exception(
                    "beacon %s: on_fire callback raised", self.name
                )


# ── Concrete beacons ────────────────────────────────────────────────


class HeartbeatBeacon(_BaseBeacon):
    """Periodic @HB broadcast.

    Construct with a callback that returns the current callsign+grid
    (or None if not configured). The interval is mode-driven (see
    spec §5.5 / §6.9): 20 minutes for HbMode.TWENTY_MIN, 60 minutes
    for HbMode.ONE_HR, single-shot for HbMode.SINGLE. app.py picks
    the interval and constructs the beacon accordingly.

    With ``single_shot=True`` the beacon fires one @HB at startup
    and then exits, invoking ``on_complete`` from the beacon thread.
    The caller (app.py) uses on_complete to flip ``hb_mode`` back to
    OFF on the asyncio thread.
    """

    def __init__(
        self,
        queue: OutboundQueue,
        identity_factory: Callable[[], Optional[tuple[str, str]]],
        *,
        interval_s: float = HEARTBEAT_INTERVAL_S,
        random_offset_s: float = HEARTBEAT_RANDOM_OFFSET_S,
        single_shot: bool = False,
        on_complete: Optional[Callable[[], None]] = None,
        on_fire: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._identity_factory = identity_factory
        super().__init__(
            queue=queue,
            message_kind=OutboundKind.HEARTBEAT,
            message_factory=self._build_message,
            interval_s=interval_s,
            # No random jitter for single-shot — we want predictable
            # "fire and stop" behavior.
            random_offset_s=0.0 if single_shot else random_offset_s,
            immediate_on_start=True,
            single_shot=single_shot,
            on_complete=on_complete,
            on_fire=on_fire,
            name="hb-beacon",
        )

    def _build_message(self) -> Optional[str]:
        identity = self._identity_factory()
        if identity is None:
            return None
        callsign, grid = identity
        if not callsign or callsign == "N0CALL" or not grid:
            return None
        # Modern JS8Call heartbeat format. Mirrors what we observed
        # on-air: "<call>: @HB HEARTBEAT <grid>". gfsk8's
        # AUTO_REMOVE_MYCALL strips the leading "<call>:" since it
        # matches our own — the from-envelope is auto-added, so the
        # on-air payload is "@HB HEARTBEAT <grid>".
        return f"{callsign}: @HB HEARTBEAT {grid}"


class EmergencyBeacon(_BaseBeacon):
    """Emergency SOS beacon.

    Identity factory returns ``(callsign, grid, lat, lon)`` where lat
    and lon are optional floats. The beacon prefers GPS lat/lon for
    the on-air position field — exact coordinates are the most useful
    payload a distress message can carry — and falls back to the
    configured Maidenhead grid only when no GPS fix is available.
    Refuses to TX when neither is available (an SOS with no location
    is useless to anyone listening).
    """

    def __init__(
        self,
        queue: OutboundQueue,
        identity_factory: Callable[
            [],
            Optional[
                tuple[
                    str,
                    Optional[str],
                    Optional[float],
                    Optional[float],
                ]
            ],
        ],
        *,
        interval_s: float = EMERGENCY_BEACON_INTERVAL_S,
    ) -> None:
        self._identity_factory = identity_factory
        super().__init__(
            queue=queue,
            message_kind=OutboundKind.ALLCALL,
            message_factory=self._build_message,
            interval_s=interval_s,
            random_offset_s=0.0,
            immediate_on_start=True,
            name="emergency-beacon",
        )

    def _build_message(self) -> Optional[str]:
        identity = self._identity_factory()
        if identity is None:
            return None
        callsign, grid, lat, lon = identity
        # Use whatever callsign we have. In emergency-override mode
        # this may be N0CALL; that's intentional per spec (an
        # unconfigured station can still call for help).
        ident = callsign or "N0CALL"
        # Position payload: GPS lat/lon (preferred) else grid (fall-
        # back). Decimal degrees to 4 places gives ~11 m precision —
        # plenty for emergency dispatch, fits in two JS8 frames after
        # the envelope. The space between lat and lon (not comma) is
        # JS8-tokenisation-friendly: receivers parsing the message
        # body see two distinct tokens for the two numbers.
        if lat is not None and lon is not None:
            pos = f"{lat:+.4f} {lon:+.4f}"
        elif grid:
            pos = grid
        else:
            return None  # no location → SOS would be unactionable
        # All-call format: "<call>: @ALLCALL SOS <pos>"
        # SOS is internationally recognized; in JS8 community usage
        # this is the convention for genuine emergency traffic.
        return f"{ident}: @ALLCALL SOS {pos}"
