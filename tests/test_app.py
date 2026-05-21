"""Tests for microjs8.app — orchestrator lifecycle.

These tests verify the asyncio loop wiring without touching any
hardware. They are runnable on the Pi 4 build host as well as a
developer laptop.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from microjs8.app import MicroJS8App
from microjs8.config import Config, StationConfig


def _make_config(*, configured: bool) -> Config:
    if configured:
        station = StationConfig(callsign="K1ABC", grid="FN42")
    else:
        station = StationConfig()
    return Config(station=station)


async def test_run_returns_when_stop_requested():
    """request_stop() must cause run() to return promptly."""
    app = MicroJS8App(_make_config(configured=True), headless=True)

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        app.request_stop()

    await asyncio.wait_for(
        asyncio.gather(app.run(), stopper()),
        timeout=3.0,
    )


async def test_run_returns_on_sigterm():
    """Sending SIGTERM to ourselves must shut the app down cleanly."""
    app = MicroJS8App(_make_config(configured=True), headless=True)

    async def kicker() -> None:
        # Give run() a moment to install the signal handler.
        await asyncio.sleep(0.05)
        signal.raise_signal(signal.SIGTERM)

    await asyncio.wait_for(
        asyncio.gather(app.run(), kicker()),
        timeout=3.0,
    )


async def test_request_stop_is_idempotent():
    """Calling request_stop() multiple times must be safe."""
    app = MicroJS8App(_make_config(configured=True), headless=True)

    async def multi_stop() -> None:
        await asyncio.sleep(0.05)
        app.request_stop()
        app.request_stop()
        app.request_stop()

    await asyncio.wait_for(
        asyncio.gather(app.run(), multi_stop()),
        timeout=3.0,
    )


async def test_runs_with_unconfigured_station():
    """An unconfigured station (N0CALL) must still allow the daemon to run.

    TX is gated separately; the daemon must boot regardless so the operator
    can use the (future) on-device setup wizard.
    """
    app = MicroJS8App(_make_config(configured=False), headless=True)

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        app.request_stop()

    await asyncio.wait_for(
        asyncio.gather(app.run(), stopper()),
        timeout=3.0,
    )


# ── Phase 19 v0.0.9: exit code differentiation ───────────────────────


def test_exit_code_default_is_zero():
    """Phase 19 v0.0.9: a freshly-constructed app has exit_code 0 —
    the clean-exit default. systemd's Restart=on-failure leaves us
    down on exit 0."""
    app = MicroJS8App(_make_config(configured=True), headless=True)
    assert app.exit_code == 0


def test_request_stop_keeps_exit_code_zero():
    """Phase 19 v0.0.9: request_stop() is the user-initiated exit
    (EXIT button, SIGTERM). It must NOT flip the restart flag —
    systemd should leave us down."""
    app = MicroJS8App(_make_config(configured=True), headless=True)
    app.request_stop()
    assert app.exit_code == 0


def test_request_restart_sets_exit_code_75():
    """Phase 19 v0.0.9: request_restart() is the radio-cycle path.
    It must set exit_code to 75 (EX_TEMPFAIL) so systemd's
    RestartForceExitStatus=75 brings us back up."""
    app = MicroJS8App(_make_config(configured=True), headless=True)
    app.request_restart()
    assert app.exit_code == 75, (
        f"request_restart must produce exit_code=75 for systemd "
        f"RestartForceExitStatus=75; got {app.exit_code}"
    )


def test_request_restart_also_sets_stop_event():
    """Phase 19 v0.0.9: request_restart() must also unblock the
    main loop (it shares the _stop event so run() returns)."""
    app = MicroJS8App(_make_config(configured=True), headless=True)
    assert not app._stop.is_set()
    app.request_restart()
    assert app._stop.is_set(), (
        "request_restart() must also signal the stop event so "
        "run() actually returns"
    )


def test_request_restart_is_idempotent():
    """Phase 19 v0.0.9: calling request_restart() multiple times is
    safe and keeps exit_code at 75."""
    app = MicroJS8App(_make_config(configured=True), headless=True)
    app.request_restart()
    app.request_restart()
    app.request_restart()
    assert app.exit_code == 75


async def test_run_returns_when_restart_requested():
    """Phase 19 v0.0.9: request_restart() (the radio-cycle path)
    must cause run() to return promptly, just like request_stop()."""
    app = MicroJS8App(_make_config(configured=True), headless=True)

    async def kicker() -> None:
        await asyncio.sleep(0.05)
        app.request_restart()

    await asyncio.wait_for(
        asyncio.gather(app.run(), kicker()),
        timeout=3.0,
    )
    # And the exit code reflects the restart intent
    assert app.exit_code == 75
