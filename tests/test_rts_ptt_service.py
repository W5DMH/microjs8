"""Tests for microjs8.cat.rts_ptt_service.RtsPttService.

The service does three jobs we verify:

  1. Open the serial port (via RtsPttClient) and reopen on failure.
  2. Forward PTT commands while connected, return False when not.
  3. Watchdog releases stuck PTT after a max-hold timeout.

Same shape as test_cat_service.py but no rigctld socket — we stub
``serial.Serial`` directly via the same fake we use in
``test_rts_ptt_client.py`` (kept inline here for self-containment).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import pytest

from microjs8.cat import rts_ptt_service as service_module
from microjs8.cat.rts_ptt_service import RtsPttService


@pytest.fixture
def fake_serial(monkeypatch):
    """Stub serial.Serial — same pattern as test_rts_ptt_client.py.

    Tracks open count, line writes, and lets tests inject failures.
    """
    state: dict[str, Any] = {
        "construct_count": 0,
        "rts_history": [],
        "dtr_history": [],
        "open_attempts": 0,  # # of times Serial() was called
        "raise_on_construct": None,
        "raise_on_rts_set": None,
        "closed_count": 0,
        "live_instances": 0,  # constructed - closed
    }

    class _FakeSerial:
        def __init__(self, **kwargs):
            state["open_attempts"] += 1
            if state["raise_on_construct"]:
                raise state["raise_on_construct"]
            state["construct_count"] += 1
            state["live_instances"] += 1
            self._rts = False
            self._dtr = False

        @property
        def rts(self) -> bool:
            return self._rts

        @rts.setter
        def rts(self, value: bool) -> None:
            if state["raise_on_rts_set"]:
                raise state["raise_on_rts_set"]
            self._rts = bool(value)
            state["rts_history"].append(bool(value))

        @property
        def dtr(self) -> bool:
            return self._dtr

        @dtr.setter
        def dtr(self, value: bool) -> None:
            self._dtr = bool(value)
            state["dtr_history"].append(bool(value))

        def close(self) -> None:
            state["closed_count"] += 1
            state["live_instances"] -= 1

    class _FakeSerialModule:
        Serial = _FakeSerial
        SerialException = Exception

    monkeypatch.setitem(sys.modules, "serial", _FakeSerialModule)
    return state


def _wait_for(predicate, timeout=2.0, interval=0.02):
    """Poll until predicate() is True or timeout. Returns the final
    result of predicate() — assert in the caller."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ── Lifecycle ───────────────────────────────────────────────────────


def test_service_opens_port_on_start(fake_serial, monkeypatch):
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
    finally:
        svc.stop()


def test_service_returns_false_when_not_connected(fake_serial, monkeypatch):
    """Calls before connection-attempt completes (or after a failure)
    should return False / None safely."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 5.0)
    fake_serial["raise_on_construct"] = OSError("device not present")
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        # Give the reconnect thread a brief tick to try and fail.
        time.sleep(0.1)
        assert not svc.is_connected
        assert svc.ptt_on() is False
        assert svc.ptt_off() is False
        # CAT operations should soft-fail (no rigctld at all).
        assert svc.get_frequency_hz() is None
        assert svc.set_frequency_hz(7_078_000) is False
    finally:
        svc.stop()


def test_status_callback_fires_on_connect(fake_serial, monkeypatch):
    """When the port opens, on_status_change is called with True."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    states: list[bool] = []
    svc = RtsPttService(
        serial_port="/dev/digirig",
        on_status_change=lambda c: states.append(c),
    )
    svc.start()
    try:
        assert _wait_for(lambda: True in states, timeout=1.0)
    finally:
        svc.stop()
    # Should have at least one True (connect) — and after stop, a False.
    assert True in states


def test_stop_releases_ptt(fake_serial, monkeypatch):
    """stop() must release PTT before closing — defense in depth."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
        svc.ptt_on()
        # Confirm RTS is asserted right now.
        assert fake_serial["rts_history"][-1] is True
    finally:
        svc.stop()
    # After stop, RTS history should end with False (released).
    assert fake_serial["rts_history"][-1] is False


# ── PTT operations ──────────────────────────────────────────────────


def test_ptt_on_asserts_rts(fake_serial, monkeypatch):
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
        assert svc.ptt_on() is True
        assert fake_serial["rts_history"][-1] is True
    finally:
        svc.stop()


def test_ptt_off_releases_rts(fake_serial, monkeypatch):
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
        svc.ptt_on()
        assert svc.ptt_off() is True
        assert fake_serial["rts_history"][-1] is False
    finally:
        svc.stop()


def test_ptt_kick_resets_watchdog(fake_serial, monkeypatch):
    """ptt_kick() should re-stamp the held-at timestamp so the
    watchdog doesn't fire mid-burst."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
        svc.ptt_on()
        original_held_at = svc._ptt_held_at  # type: ignore[attr-defined]
        time.sleep(0.05)
        svc.ptt_kick()
        new_held_at = svc._ptt_held_at  # type: ignore[attr-defined]
        assert new_held_at is not None
        assert original_held_at is not None
        assert new_held_at > original_held_at, (
            "ptt_kick should advance the watchdog held-at timestamp"
        )
    finally:
        svc.stop()


def test_ptt_kick_when_not_keyed_is_safe(fake_serial, monkeypatch):
    """ptt_kick() with no active PTT should not crash or set timing."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
        svc.ptt_kick()  # must not raise
        assert svc._ptt_held_at is None  # type: ignore[attr-defined]
    finally:
        svc.stop()


# ── Watchdog ────────────────────────────────────────────────────────


def test_watchdog_releases_stuck_ptt(fake_serial, monkeypatch):
    """If PTT is held longer than max-hold, the watchdog forcibly
    releases. Test with a very small override so we don't have to
    wait 20 seconds."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
        svc.set_ptt_max_hold(0.3)  # 300 ms — short enough for tests
        svc.ptt_on()
        # Wait long enough that the watchdog fires (200ms tick + 300ms hold).
        # Watchdog runs every 0.2 s, so 0.8 s is enough.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if svc._ptt_held_at is None:  # type: ignore[attr-defined]
                break
            time.sleep(0.05)
        assert svc._ptt_held_at is None, (  # type: ignore[attr-defined]
            "watchdog should have force-released stuck PTT"
        )
        # The line state should also reflect PTT released.
        assert fake_serial["rts_history"][-1] is False
    finally:
        svc.stop()


def test_set_ptt_max_hold_zero_clears_override(fake_serial, monkeypatch):
    """Passing 0 to set_ptt_max_hold clears the override (back to
    the default 20s). Lets callers cancel a long-burst override
    after end_burst."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.set_ptt_max_hold(60.0)
    assert svc._ptt_max_hold_override_s == 60.0  # type: ignore[attr-defined]
    svc.set_ptt_max_hold(0.0)
    assert svc._ptt_max_hold_override_s is None  # type: ignore[attr-defined]


def test_ptt_off_clears_max_hold_override(fake_serial, monkeypatch):
    """After a normal ptt_off(), the override should be cleared so
    the next PTT cycle uses the default. Mirrors CatService semantics.
    """
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        assert _wait_for(lambda: svc.is_connected, timeout=1.0)
        svc.set_ptt_max_hold(60.0)
        svc.ptt_on()
        svc.ptt_off()
        assert svc._ptt_max_hold_override_s is None  # type: ignore[attr-defined]
    finally:
        svc.stop()


# ── Reconnect ──────────────────────────────────────────────────────


def test_reconnect_after_initial_failure(fake_serial, monkeypatch):
    """When the port isn't available at start, the reconnect thread
    keeps retrying. Once available, the service comes online."""
    monkeypatch.setattr(service_module, "_RECONNECT_DELAY_S", 0.05)
    fake_serial["raise_on_construct"] = OSError("device not yet plugged in")
    svc = RtsPttService(serial_port="/dev/digirig")
    svc.start()
    try:
        time.sleep(0.2)
        assert not svc.is_connected
        # Operator plugs in the device — failures stop.
        fake_serial["raise_on_construct"] = None
        assert _wait_for(lambda: svc.is_connected, timeout=1.5)
    finally:
        svc.stop()
