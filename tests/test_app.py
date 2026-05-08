"""Tests for minijs8.app — orchestrator lifecycle.

These tests verify the asyncio loop wiring without touching any
hardware. They are runnable on the Pi 4 build host as well as a
developer laptop.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from minijs8.app import MiniJS8App
from minijs8.config import Config, StationConfig


def _make_config(*, configured: bool) -> Config:
    if configured:
        station = StationConfig(callsign="K1ABC", grid="FN42")
    else:
        station = StationConfig()
    return Config(station=station)


async def test_run_returns_when_stop_requested():
    """request_stop() must cause run() to return promptly."""
    app = MiniJS8App(_make_config(configured=True), headless=True)

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        app.request_stop()

    await asyncio.wait_for(
        asyncio.gather(app.run(), stopper()),
        timeout=3.0,
    )


async def test_run_returns_on_sigterm():
    """Sending SIGTERM to ourselves must shut the app down cleanly."""
    app = MiniJS8App(_make_config(configured=True), headless=True)

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
    app = MiniJS8App(_make_config(configured=True), headless=True)

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
    app = MiniJS8App(_make_config(configured=False), headless=True)

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        app.request_stop()

    await asyncio.wait_for(
        asyncio.gather(app.run(), stopper()),
        timeout=3.0,
    )
