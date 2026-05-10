"""TFT button watcher.

Two physical buttons on the Adafruit Mini PiTFT 1.3":

    BUTTON_TOP    = GPIO23  (Adafruit silkscreen "Button A")
    BUTTON_BOTTOM = GPIO24  (Adafruit silkscreen "Button B")

Both are active-LOW with on-board 10k pull-ups. ``gpiozero.Button``
handles debounce and polarity for us.

The watcher's job is to translate raw button events into UI commands:

  - **Single short press** of either button → screen ring navigation
    (TOP=advance, BOTTOM=retreat). Fired on RELEASE, not on press, so
    a hold for the shutdown gesture doesn't flicker the ring on the
    way in.

  - **Both held** → start a 5-second shutdown timer. The UI switches
    to the SHUTTING_DOWN screen immediately, with a countdown bar
    that drains from full to empty over the 5 s. Releasing either
    button before the timer expires cancels.

  - **Both held to completion** → invoke ``shutdown_callback``, which
    in production is ``systemctl poweroff`` via the asyncio loop.

The shutdown timer is driven from the asyncio loop with
``asyncio.sleep(0.05)`` ticks, not from a thread or hardware timer —
this keeps the cancellation semantics simple (one Task, cancel on
release).

GPIO callbacks happen on a gpiozero-internal thread; we marshal events
into the asyncio loop with ``loop.call_soon_threadsafe()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional, Protocol

from microjs8.ui.state import UIState

_log = logging.getLogger(__name__)

# Adafruit Mini PiTFT 1.3" pin mapping. Verified against
# https://learn.adafruit.com/adafruit-mini-pitft-135x240-color-tft-add-on-for-raspberry-pi/pinouts
PIN_BUTTON_TOP = 23
PIN_BUTTON_BOTTOM = 24

# Shutdown gesture timing.
SHUTDOWN_HOLD_S = 5.0      # both held this long → poweroff
_SHUTDOWN_TICK_S = 0.05    # 20 Hz progress bar updates


class _ButtonLike(Protocol):
    """Subset of gpiozero.Button we depend on."""

    when_pressed: Optional[Callable[[], None]]
    when_released: Optional[Callable[[], None]]
    is_pressed: bool

    def close(self) -> None: ...


# Type alias for the shutdown callback — async, returns None.
ShutdownCallback = Callable[[], Awaitable[None]]


class ButtonWatcher:
    """Owns both Button objects and the shutdown gesture state machine.

    Construct on the asyncio loop, before starting the render thread.
    Call ``start()`` to attach the GPIO callbacks. ``stop()`` releases
    the GPIO resources cleanly on shutdown.
    """

    def __init__(
        self,
        ui_state: UIState,
        loop: asyncio.AbstractEventLoop,
        shutdown_callback: ShutdownCallback,
        *,
        # Injectable for tests.
        button_top: Optional[_ButtonLike] = None,
        button_bottom: Optional[_ButtonLike] = None,
    ) -> None:
        self._ui = ui_state
        self._loop = loop
        self._shutdown_cb = shutdown_callback
        self._button_top: Optional[_ButtonLike] = button_top
        self._button_bottom: Optional[_ButtonLike] = button_bottom

        # State for the both-held gesture. All accessed from the
        # asyncio thread only — GPIO callbacks marshal here via
        # call_soon_threadsafe.
        self._top_pressed_at: Optional[float] = None
        self._bot_pressed_at: Optional[float] = None
        self._shutdown_task: Optional[asyncio.Task[None]] = None
        # When True, the current press cycle has been "consumed" by a
        # shutdown gesture; releases of either button must NOT fire
        # the single-press ring-navigation action. Reset only when
        # both buttons are back up.
        self._gesture_consumed: bool = False

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Attach GPIO callbacks. Idempotent."""
        if self._button_top is None:
            self._button_top = self._open_button(PIN_BUTTON_TOP)
        if self._button_bottom is None:
            self._button_bottom = self._open_button(PIN_BUTTON_BOTTOM)

        # gpiozero invokes these on its own thread → marshal to asyncio.
        self._button_top.when_pressed = lambda: self._loop.call_soon_threadsafe(
            self._on_top_pressed
        )
        self._button_top.when_released = lambda: self._loop.call_soon_threadsafe(
            self._on_top_released
        )
        self._button_bottom.when_pressed = lambda: self._loop.call_soon_threadsafe(
            self._on_bottom_pressed
        )
        self._button_bottom.when_released = lambda: self._loop.call_soon_threadsafe(
            self._on_bottom_released
        )

        _log.info(
            "buttons attached: TOP=GPIO%d, BOTTOM=GPIO%d, shutdown-hold=%.1fs",
            PIN_BUTTON_TOP, PIN_BUTTON_BOTTOM, SHUTDOWN_HOLD_S,
        )

    def stop(self) -> None:
        """Release GPIO and cancel any pending shutdown task."""
        if self._shutdown_task is not None and not self._shutdown_task.done():
            self._shutdown_task.cancel()
            self._shutdown_task = None
        for btn in (self._button_top, self._button_bottom):
            if btn is not None:
                try:
                    btn.close()
                except Exception:
                    _log.exception("error closing button")
        self._button_top = None
        self._button_bottom = None

    @staticmethod
    def _open_button(pin: int) -> _ButtonLike:
        """Real gpiozero.Button — imported lazily for host-test friendliness.

        We force the lgpio backend explicitly. The default gpiozero
        fallback chain (lgpio → rpigpio → pigpio → native) silently
        selects whatever loads, but RPi.GPIO and 'native' are both
        broken on Bookworm for our event-callback use. Setting the
        env var BEFORE first gpiozero import is the documented way
        to pin the backend; we set it again here as defense-in-depth
        in case some other module imported gpiozero first.
        """
        import os
        os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")

        from gpiozero import Button, Device  # type: ignore[import-not-found]

        # Verify the backend actually came up. If gpiozero fell back,
        # log loudly so the operator can fix the systemd Environment=
        # rather than chase a phantom button-doesn't-work bug.
        # gpiozero has used both "LgpioFactory" and "LGPIOFactory" as
        # the class name across versions — accept either spelling.
        factory_name = type(Device.pin_factory).__name__ if Device.pin_factory else "None"
        if factory_name.lower() not in ("lgpiofactory",):
            _log.warning(
                "gpiozero pin factory is %s, expected an Lgpio factory. "
                "Buttons may not respond. Check that GPIOZERO_PIN_FACTORY=lgpio "
                "is set in the systemd unit and that liblgpio is installed.",
                factory_name,
            )

        # bounce_time=50ms is a comfortable mechanical-tact-switch debounce
        # (typical bounce window for these caps is 5-15 ms; 50 is the
        # gpiozero example default and works well in noisy environments).
        return Button(
            pin,
            pull_up=True,             # PiTFT has its own 10k pull-up; we mirror.
            bounce_time=0.050,
            hold_time=0.050,
        )

    # ── Event handlers (asyncio thread) ──────────────────────────────

    def _on_top_pressed(self) -> None:
        self._top_pressed_at = time.monotonic()
        self._maybe_arm_shutdown()

    def _on_bottom_pressed(self) -> None:
        self._bot_pressed_at = time.monotonic()
        self._maybe_arm_shutdown()

    def _on_top_released(self) -> None:
        was_held_alone = (
            self._top_pressed_at is not None
            and self._bot_pressed_at is None
            and not self._gesture_consumed
        )
        self._top_pressed_at = None
        self._cancel_shutdown_if_armed()
        if was_held_alone:
            # Short press of TOP alone → advance ring.
            self._ui.advance_ring()
        # Reset gesture flag once both buttons are released.
        if self._bot_pressed_at is None:
            self._gesture_consumed = False

    def _on_bottom_released(self) -> None:
        was_held_alone = (
            self._bot_pressed_at is not None
            and self._top_pressed_at is None
            and not self._gesture_consumed
        )
        self._bot_pressed_at = None
        self._cancel_shutdown_if_armed()
        if was_held_alone:
            # Short press of BOTTOM alone → retreat ring.
            self._ui.retreat_ring()
        # Reset gesture flag once both buttons are released.
        if self._top_pressed_at is None:
            self._gesture_consumed = False

    # ── Shutdown gesture ─────────────────────────────────────────────

    def _maybe_arm_shutdown(self) -> None:
        """If both buttons are now down, kick off the countdown task."""
        if self._top_pressed_at is None or self._bot_pressed_at is None:
            return
        if self._shutdown_task is not None and not self._shutdown_task.done():
            return  # already armed
        _log.info("both buttons held — arming shutdown countdown")
        # Mark this press cycle so the eventual releases don't ALSO
        # trigger single-button navigation actions.
        self._gesture_consumed = True
        self._ui.begin_shutdown()
        self._shutdown_task = self._loop.create_task(self._shutdown_countdown())

    def _cancel_shutdown_if_armed(self) -> None:
        """Cancel the countdown task and roll the UI back."""
        if self._shutdown_task is not None and not self._shutdown_task.done():
            _log.info("shutdown gesture cancelled (button released)")
            self._shutdown_task.cancel()
            self._shutdown_task = None
            self._ui.cancel_shutdown()

    async def _shutdown_countdown(self) -> None:
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
            # Cancellation = user released a button. cancel_shutdown_if_armed()
            # has already restored the previous screen via UIState.
            raise

        _log.warning("shutdown countdown complete — invoking shutdown callback")
        try:
            await self._shutdown_cb()
        except Exception:
            _log.exception("shutdown callback raised")
            # Roll the UI back so we don't sit on the SHUTTING_DOWN screen
            # forever if the systemctl call somehow failed.
            self._ui.cancel_shutdown()


# ── Default shutdown callbacks ───────────────────────────────────────


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
    MicroJS8, the operator has just pressed both physical buttons for
    five continuous seconds — that's about as deliberate as a power-off
    gesture gets, and we honour it unconditionally rather than letting
    a stale ssh session veto the operator.

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
