"""Emergency-beacon arm/disarm hold gesture.

Sibling to ``shutdown_gesture.ShutdownGesture``: a 3-second hold
gesture with progress feedback and explicit cancel. Difference is
this one runs in BOTH directions:

  - **Arming** (3-second hold of ENTER while beacon is idle) →
    on completion, calls ``arm_callback`` which fires up the
    EmergencyBeacon TX thread.
  - **Disarming** (3-second hold of ESC while beacon is armed) →
    on completion, calls ``disarm_callback`` which stops the
    EmergencyBeacon and clears the armed flag.

Both directions share a single asyncio task — only one hold can
be in progress at a time. The router decides which direction to
start based on the current ``emergency_beacon_armed`` flag.

Cancellation: pressing ESC during an arming hold, or pressing
ENTER during a disarming hold, cancels via ``cancel()``. The UI
state snaps back to "idle but possibly armed" (cancel does NOT
toggle the armed flag — it only aborts the in-flight transition).

Implementation note: keyboard events in MiniJS8 don't model
key-release, so this isn't a literal "hold the key down" gesture
the way the hardware-button shutdown is. Instead, the operator
presses the trigger key once and watches a 3-second countdown,
which is interruptible by pressing the cancel key. The visual
contract ("hold for 3 seconds") matches the operator's mental
model; the implementation just uses an asyncio timer instead of
GPIO press/release semantics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Literal, Optional

from microjs8.ui.state import UIState

_log = logging.getLogger(__name__)


# Default hold duration. Spec §6.4: "ARM: hold ENTER 3 s".
DEFAULT_EMERGENCY_HOLD_S: float = 3.0

# 20 Hz progress tick — same rate as ShutdownGesture for visual
# consistency.
_EMERGENCY_TICK_S: float = 0.05


# Direction labels — used in logs and surfaced to UIState so the
# renderer can show "Arming…" vs "Disarming…".
Direction = Literal["arm", "disarm"]


# Completion callback signature. Both arming and disarming need to
# do app-level work (start/stop the beacon thread) which is async.
ArmCallback = Callable[[], Awaitable[None]]


class EmergencyArmGesture:
    """3-second hold gesture for arming or disarming the emergency beacon.

    Owns a single asyncio Task at a time; mutually-exclusive between
    arming and disarming. ``begin_arming()`` and ``begin_disarming()``
    are idempotent against double-start; ``cancel()`` is idempotent
    against no-op cancel.
    """

    def __init__(
        self,
        ui: UIState,
        loop: asyncio.AbstractEventLoop,
        arm_callback: ArmCallback,
        disarm_callback: ArmCallback,
        hold_seconds: float = DEFAULT_EMERGENCY_HOLD_S,
    ) -> None:
        self._ui = ui
        self._loop = loop
        self._arm_cb = arm_callback
        self._disarm_cb = disarm_callback
        self._hold_s = float(hold_seconds)
        self._task: Optional[asyncio.Task[None]] = None
        self._direction: Optional[Direction] = None

    # ── Predicates ───────────────────────────────────────────────────

    def is_active(self) -> bool:
        """True iff any hold is currently in progress."""
        return self._task is not None and not self._task.done()

    def is_arming(self) -> bool:
        return self.is_active() and self._direction == "arm"

    def is_disarming(self) -> bool:
        return self.is_active() and self._direction == "disarm"

    # ── Hold initiators ──────────────────────────────────────────────

    def begin_arming(self, *, source: str = "keyboard") -> bool:
        """Start the 3-second arm-hold.

        Returns True if newly started; False if a hold was already
        in progress (regardless of direction). The router calls this
        on Enter-press in the EMERGENCY screen when the beacon is
        currently idle.
        """
        if self.is_active():
            _log.debug(
                "emergency hold already active (%s); begin_arming ignored",
                self._direction,
            )
            return False
        _log.info("emergency: arming hold started via %s", source)
        self._direction = "arm"
        self._ui.begin_emergency_arm_hold()
        self._task = self._loop.create_task(self._countdown("arm"))
        return True

    def begin_disarming(self, *, source: str = "keyboard") -> bool:
        """Start the 3-second disarm-hold.

        Returns True if newly started; False if a hold was already
        in progress. The router calls this on Esc-press in the
        EMERGENCY screen when the beacon is currently armed.
        """
        if self.is_active():
            _log.debug(
                "emergency hold already active (%s); begin_disarming ignored",
                self._direction,
            )
            return False
        _log.info("emergency: disarming hold started via %s", source)
        self._direction = "disarm"
        self._ui.begin_emergency_disarm_hold()
        self._task = self._loop.create_task(self._countdown("disarm"))
        return True

    # ── Cancel ───────────────────────────────────────────────────────

    def cancel(self, *, source: str = "keyboard") -> bool:
        """Cancel the in-flight hold (either direction).

        Returns True if cancelled, False if nothing was running.
        Safe to call unconditionally. The armed-flag on UIState is
        NOT toggled — cancel aborts the transition, it doesn't reverse
        a completed arm/disarm.
        """
        if not self.is_active():
            return False
        _log.info(
            "emergency: %s hold cancelled via %s",
            self._direction, source,
        )
        assert self._task is not None
        self._task.cancel()
        self._task = None
        self._direction = None
        self._ui.cancel_emergency_hold()
        return True

    # ── Countdown coroutine ──────────────────────────────────────────

    async def _countdown(self, direction: Direction) -> None:
        """Tick at 20 Hz; on completion, invoke the appropriate callback.

        Drains ``emergency_hold_progress`` from 1.0 to 0.0 over
        ``self._hold_s``. ``time.monotonic`` (not wall-clock) so a
        chrony step mid-countdown can't surprise us with a negative
        elapsed.

        Cancellation propagates the standard CancelledError up. The
        caller (``cancel()``) has already rolled the UI back via
        ``ui.cancel_emergency_hold()``; we don't double-cancel here.
        """
        start = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - start
                remaining_frac = max(0.0, 1.0 - elapsed / self._hold_s)
                self._ui.update_emergency_hold_progress(remaining_frac)
                if elapsed >= self._hold_s:
                    break
                await asyncio.sleep(_EMERGENCY_TICK_S)
        except asyncio.CancelledError:
            raise

        _log.warning(
            "emergency: %s hold complete — invoking callback", direction,
        )
        # Choose the right completion action.
        cb = self._arm_cb if direction == "arm" else self._disarm_cb
        try:
            await cb()
        except Exception:
            _log.exception("emergency %s callback raised", direction)
            # If the callback failed, roll back the hold state so we
            # don't leave the operator stranded with a half-completed
            # transition. The armed flag isn't touched in this path —
            # the callback is responsible for updating it; if the
            # callback failed, the armed flag stays at its old value.
            self._ui.cancel_emergency_hold()
        finally:
            # Always clear our local task reference so a follow-up
            # gesture can start fresh.
            self._task = None
            self._direction = None
