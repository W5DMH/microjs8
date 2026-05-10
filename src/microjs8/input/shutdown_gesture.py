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

# UI countdown tick rate. 20 Hz is plenty for a smooth progress bar
# without taxing the asyncio loop.
_SHUTDOWN_TICK_S: Final[float] = 0.05


class ShutdownGesture:
    """Owns the Fn+Q press-and-hold state machine.

    Lifecycle:
      - ``arm()`` is called from the router when ``Fn+Q`` is pressed.
        Idempotent — a second arm() while the timer is already running
        is a no-op (defends against keyboard auto-repeat or double events).
      - ``cancel()`` is called when ``Fn+Q`` is released. Cancels the
        countdown task (if any) and rolls the UI back.
      - On full hold completion, ``shutdown_callback`` is awaited.

    The instance is constructed once at app startup with a reference to
    the running asyncio loop and the shared UIState. ``stop()`` cancels
    any in-flight countdown for clean test/test-suite teardown.
    """

    def __init__(
        self,
        ui: UIState,
        loop: asyncio.AbstractEventLoop,
        shutdown_callback: Callable[[], Awaitable[None]],
    ) -> None:
        self._ui = ui
        self._loop = loop
        self._shutdown_cb = shutdown_callback
        self._task: Optional[asyncio.Task[None]] = None
        # When the press fired — only used for diagnostic logging when
        # cancel() is called (so the journal records how long the
        # operator held).
        self._armed_at: Optional[float] = None

    # ── Public API ────────────────────────────────────────────────────

    def arm(self) -> None:
        """Begin the countdown. Called on Fn+Q key-down."""
        if self._task is not None and not self._task.done():
            # Already armed — typically from keyboard auto-repeat. Ignore.
            return
        _log.info("Fn+Q pressed — arming shutdown countdown (%.1fs)", SHUTDOWN_HOLD_S)
        self._armed_at = time.monotonic()
        self._ui.begin_shutdown()
        self._task = self._loop.create_task(self._countdown())

    def cancel(self) -> None:
        """Abort the countdown. Called on Fn+Q key-up."""
        if self._task is None or self._task.done():
            # Nothing armed (or completed); nothing to cancel.
            return
        held = time.monotonic() - (self._armed_at or 0.0)
        _log.info("shutdown gesture cancelled (Fn+Q released after %.2fs)", held)
        self._task.cancel()
        self._task = None
        self._armed_at = None
        self._ui.cancel_shutdown()

    def stop(self) -> None:
        """Release any in-flight task. For app-shutdown / test teardown."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None
        self._armed_at = None

    @property
    def is_armed(self) -> bool:
        """True if the countdown is currently running."""
        return self._task is not None and not self._task.done()

    # ── Internals ─────────────────────────────────────────────────────

    async def _countdown(self) -> None:
        """Tick the progress bar and fire the shutdown when done."""
        start = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - start
                remaining_frac = max(0.0, 1.0 - elapsed / SHUTDOWN_HOLD_S)
                self._ui.update_shutdown_progress(remaining_frac)
                if elapsed >= SHUTDOWN_HOLD_S:
                    break
                await asyncio.sleep(_SHUTDOWN_TICK_S)
        except asyncio.CancelledError:
            # cancel() above already restored the previous screen.
            raise

        _log.warning("shutdown countdown complete — invoking shutdown callback")
        try:
            await self._shutdown_cb()
        except Exception:
            _log.exception("shutdown callback raised")
            # Roll back the UI so we don't sit on the SHUTTING_DOWN
            # screen forever if the systemctl call somehow failed.
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
