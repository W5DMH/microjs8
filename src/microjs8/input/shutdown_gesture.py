"""Fn+Q press-and-hold shutdown gesture.

Replaces the both-buttons-held gesture from MiniJS8 (which lived in
``input/buttons.py``) with a keyboard-event-driven equivalent. The
state machine is the same; only the input source differs:

  - Operator presses ``Fn+Q`` → ``arm()`` is called.
  - The UI immediately switches to the SHUTTING_DOWN screen with a
    countdown bar that drains over ``SHUTDOWN_HOLD_S`` seconds.
  - If the operator releases ``Fn+Q`` before the timer expires,
    ``cancel()`` rolls the UI back. No shutdown.
  - If the operator holds to completion, the shutdown callback fires —
    in production, ``systemctl poweroff --ignore-inhibitors``.

Why a single-key gesture is enough now: the CardputerZero has no tactile
buttons; shutdown lives entirely on the keyboard. ``Fn+Q`` is a distinct
evdev keycode (the kernel keymap turns it into one), so we don't need
modifier tracking. The press-and-hold safety margin survives because the
3-second hold + cancel-on-release semantics are preserved.

Why asyncio not a thread: keyboard events arrive on the asyncio loop
already (the keyboard reader thread marshals them via
``loop.call_soon_threadsafe``). Hosting the countdown as a coroutine is
strictly less plumbing than the buttons.py model, which had to bridge
gpiozero's callback thread → asyncio.

Test surface: the unit test (``tests/test_shutdown_gesture.py``) drives
the gesture with a fake loop and a fake UIState, reusing the same
patterns as the prior ``test_buttons.py`` so coverage parity is
preserved.

The ``systemctl_poweroff`` and ``fake_shutdown`` callbacks are relocated
from ``input/buttons.py`` verbatim — same shutdown semantics, same
``--ignore-inhibitors`` flag, same polkit rule
(``polkit/50-microjs8-poweroff.rules``) authorising the ``microjs8`` user.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Final, Optional

from microjs8.ui.state import UIState

_log = logging.getLogger(__name__)


# Hold duration to fire shutdown. 3 seconds is the value documented in
# the build spec §6.3.2 (vs MiniJS8's 5 s) — a deliberately shorter hold
# because the keyboard gesture is more discoverable and less ambiguous
# than holding two physical buttons. A non-deliberate Fn+Q press happens
# rarely enough that 3 s is comfortable; 5 s would be tedious for a
# user who just wants to power down at the end of a session.
SHUTDOWN_HOLD_S: Final[float] = 3.0
# Phase 15: upstream's canonical name. The original name is kept
# as an alias since older microjs8 code may import it.
DEFAULT_SHUTDOWN_HOLD_S: Final[float] = SHUTDOWN_HOLD_S

# UI countdown tick rate. 20 Hz is plenty for a smooth progress bar
# without taxing the asyncio loop.
_SHUTDOWN_TICK_S: Final[float] = 0.05


class ShutdownGesture:
    """Owns the Fn+Q press-and-hold state machine.

    Phase 15 (May 2026): adopted upstream's idempotent ``arm()`` /
    ``cancel()`` with a ``source`` diagnostic kwarg and bool returns.
    ``is_armed`` is a method (was a property in earlier microjs8
    revisions); this matches the EmergencyArmGesture API and lets
    future versions accept additional state without breaking
    callers.

    The Fn+Q binding is preserved (per the operator's Q1: keep the
    Phase 3 keyboard-only convention; the CardputerZero has no
    tactile buttons, so the upstream Ctrl-X path doesn't apply).
    """

    def __init__(
        self,
        ui: UIState,
        loop: asyncio.AbstractEventLoop,
        shutdown_callback: Callable[[], Awaitable[None]],
        hold_seconds: float = SHUTDOWN_HOLD_S,
    ) -> None:
        self._ui = ui
        self._loop = loop
        self._shutdown_cb = shutdown_callback
        self._hold_s = float(hold_seconds)
        self._task: Optional[asyncio.Task[None]] = None
        # _armed_at retained for backward-compat diagnostic logging
        # in cancel() (held duration). Optional[float], None when idle.
        self._armed_at: Optional[float] = None

    def is_armed(self) -> bool:
        """True iff a countdown is currently running and not yet done."""
        return self._task is not None and not self._task.done()

    def arm(self, *, source: str = "keyboard Fn+Q") -> bool:
        """Start the countdown.

        Returns True if newly armed; False if a countdown was
        already running (idempotent — typically from keyboard
        auto-repeat or a double-trigger race). ``source`` is
        logged for diagnostics; default "keyboard Fn+Q" matches
        the most common caller (router's Fn+Q handler).
        """
        if self.is_armed():
            _log.debug("shutdown already armed; arm(%s) ignored", source)
            return False
        _log.info(
            "shutdown armed via %s (%.1fs hold)",
            source, self._hold_s,
        )
        self._armed_at = time.monotonic()
        self._ui.begin_shutdown()
        self._task = self._loop.create_task(self._countdown())
        return True

    def cancel(self, *, source: str = "keyboard Fn+Q") -> bool:
        """Cancel a running countdown and restore the previous screen.

        Returns True if cancelled, False if nothing was running.
        Safe to call unconditionally — useful in key-release
        handlers that don't know whether the gesture was actually
        armed.
        """
        if not self.is_armed():
            return False
        held = time.monotonic() - (self._armed_at or 0.0)
        _log.info(
            "shutdown cancelled via %s (held %.2fs)", source, held,
        )
        assert self._task is not None
        self._task.cancel()
        self._task = None
        self._armed_at = None
        self._ui.cancel_shutdown()
        return True

    def stop(self) -> None:
        """Release any in-flight task. For app-shutdown / test teardown.

        microjs8-specific: not in upstream. Used by run()'s cleanup
        path so the daemon can exit cleanly even with a half-armed
        countdown. Does NOT restore the UI (the daemon is exiting
        anyway). Tests use this for fixture teardown.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None
        self._armed_at = None

    async def _countdown(self) -> None:
        """Tick the progress bar at 20 Hz; fire shutdown_cb on completion.

        The progress bar drains from 1.0 (full) to 0.0 (empty) over
        ``self._hold_s`` seconds. We use ``time.monotonic`` (not
        wall clock) so chrony stepping mid-countdown can't surprise
        us with a negative elapsed.

        Cancellation: ``asyncio.CancelledError`` propagates out
        cleanly. The caller (``cancel()``) has already rolled the
        UI back; we don't double-roll here.
        """
        start = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - start
                remaining_frac = max(0.0, 1.0 - elapsed / self._hold_s)
                self._ui.update_shutdown_progress(remaining_frac)
                if elapsed >= self._hold_s:
                    break
                await asyncio.sleep(_SHUTDOWN_TICK_S)
        except asyncio.CancelledError:
            # Cancel path: UIState was already rolled back by cancel().
            raise

        _log.warning("shutdown countdown complete — invoking callback")
        try:
            await self._shutdown_cb()
        except Exception:
            _log.exception("shutdown callback raised")
            # If the callback failed (auth issue with polkit, transient
            # systemctl error, etc.) restore the UI so the operator
            # isn't stranded on a SHUTTING_DOWN screen forever.
            self._ui.cancel_shutdown()


# ── Default shutdown callbacks ────────────────────────────────────────


async def systemctl_poweroff() -> None:
    """Invoke ``systemctl poweroff`` via subprocess on the asyncio loop.

    We do NOT call ``os.system()`` or block — that would freeze the
    asyncio loop and the render thread couldn't push the final frame.
    ``asyncio.create_subprocess_exec`` spawns and returns immediately;
    the kernel takes care of the rest as the daemon's signal handlers
    fire from systemd's stop sequence.

    ``--ignore-inhibitors`` is critical here. systemd-logind's default
    behaviour blocks shutdown when other users are logged in (e.g. an
    SSH session left open during development). For an appliance like
    MicroJS8, the operator has just held ``Fn+Q`` for three continuous
    seconds — that's about as deliberate as a power-off gesture gets,
    and we honour it unconditionally rather than letting a stale ssh
    session veto the operator.

    Authorization to perform the power-off comes from the polkit rule
    installed at ``/etc/polkit-1/rules.d/50-microjs8-poweroff.rules``.
    Without that rule, this call returns "Interactive authentication
    required" and shutdown silently fails.
    """
    _log.warning("invoking: systemctl poweroff --ignore-inhibitors")
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl", "poweroff", "--ignore-inhibitors",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Don't wait indefinitely — once systemctl has issued the request,
    # the rest of shutdown is handled by systemd / kernel. But we DO
    # want to capture the immediate return so authorization failures
    # surface in the journal instead of leaving the operator wondering
    # why the device didn't power off.
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=5.0
        )
    except asyncio.TimeoutError:
        # systemctl normally returns within milliseconds; a 5-second
        # hang means systemd is busy bringing things down, which is the
        # path we want — exit and let the kernel finish.
        _log.info("systemctl poweroff issued, no response within 5s "
                  "(expected during normal shutdown)")
        return

    if proc.returncode != 0:
        _log.error(
            "systemctl poweroff failed (rc=%d): %s",
            proc.returncode,
            stderr.decode("utf-8", errors="replace").strip(),
        )
    else:
        _log.info("systemctl poweroff acknowledged")


async def fake_shutdown() -> None:
    """No-op shutdown for host tests."""
    _log.info("fake_shutdown() called (test mode)")
