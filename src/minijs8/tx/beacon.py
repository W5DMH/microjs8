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

from minijs8.tx.queue import OutboundKind, OutboundQueue

_log = logging.getLogger(__name__)


# Heartbeat config (Step 6 locked decisions).
HEARTBEAT_INTERVAL_S = 30 * 60       # 30 minutes
# Random slot offset added to the 30-minute interval. Spread is up to
# 1 minute (4 JS8 slots), small enough that "30 min interval" is
# still accurate but enough to break up beacon collisions.
HEARTBEAT_RANDOM_OFFSET_S = 60

# Emergency beacon config.
EMERGENCY_BEACON_INTERVAL_S = 5 * 60  # 5 minutes


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
        name: str = "beacon",
    ) -> None:
        """
        ``message_factory`` is called every interval to produce the
        text to enqueue. Returns None to skip this cycle (e.g., grid
        not yet known). The factory is called from the beacon thread,
        not from a UI thread.
        """
        super().__init__(name=name, daemon=True)
        self._queue = queue
        self._kind = message_kind
        self._make_message = message_factory
        self._interval_s = interval_s
        self._random_offset_s = random_offset_s
        self._immediate_on_start = immediate_on_start
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
            "beacon %s starting (interval=%.0fs, immediate=%s)",
            self.name, self._interval_s, self._immediate_on_start,
        )
        if self._immediate_on_start:
            self._fire_one()
        while not self._stop_event.is_set():
            sleep_s = self._next_sleep_seconds()
            self._stop_event.wait(sleep_s)
            if self._stop_event.is_set():
                break
            self._fire_one()
        _log.info("beacon %s stopped", self.name)

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


# ── Concrete beacons ────────────────────────────────────────────────


class HeartbeatBeacon(_BaseBeacon):
    """Periodic @HB broadcast.

    Construct with a callback that returns the current callsign+grid
    (or None if not configured). The beacon respects "immediate-on-toggle"
    by firing once at start.
    """

    def __init__(
        self,
        queue: OutboundQueue,
        identity_factory: Callable[[], Optional[tuple[str, str]]],
        *,
        interval_s: float = HEARTBEAT_INTERVAL_S,
        random_offset_s: float = HEARTBEAT_RANDOM_OFFSET_S,
    ) -> None:
        self._identity_factory = identity_factory
        super().__init__(
            queue=queue,
            message_kind=OutboundKind.HEARTBEAT,
            message_factory=self._build_message,
            interval_s=interval_s,
            random_offset_s=random_offset_s,
            immediate_on_start=True,
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
        # on-air: "<call>: @HB HEARTBEAT <grid>". Has to fit in 12
        # chars of *encoded* payload — the JS8 codec compresses common
        # patterns aggressively.
        # We let the encoder do the codec work; just hand it the
        # full text.
        return f"{callsign}: @HB HEARTBEAT {grid}"


class EmergencyBeacon(_BaseBeacon):
    """Emergency beacon.

    No manual cancel. Only stops when the daemon stops (which happens
    on reboot or shutdown). Per spec.
    """

    def __init__(
        self,
        queue: OutboundQueue,
        identity_factory: Callable[[], Optional[tuple[str, str]]],
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
        callsign, grid = identity
        # Grid is required for emergency beacon — knowing where the
        # distress call came from is the entire point.
        if not grid:
            return None
        # Use whatever callsign we have. In emergency-override mode
        # this may be N0CALL; that's intentional per spec (an
        # unconfigured station can still call for help).
        ident = callsign or "N0CALL"
        # All-call format: "<call>: @ALLCALL SOS <grid>"
        # SOS is internationally recognized; in JS8 community usage
        # this is the convention for genuine emergency traffic.
        return f"{ident}: @ALLCALL SOS {grid}"
