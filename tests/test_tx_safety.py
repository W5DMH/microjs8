"""Tests for microjs8.tx.safety.TxSafetyGate.

Exercises all four safety checks plus the emergency-override path.
We use a controllable chrony stub so tests don't depend on the real
``chronyc`` binary.
"""

from __future__ import annotations

import pytest

from microjs8.tx.safety import N0CALL, TxSafetyGate
from microjs8.ui.state import UIState


def _state(
    callsign: str = "K1ABC",
    grid: str = "FN42",
    tx_allowed: bool = True,
) -> UIState:
    return UIState(callsign, grid, tx_allowed, "miles")


# ── Normal path ──────────────────────────────────────────────────────


def test_all_passing_allows_tx():
    s = _state()
    gate = TxSafetyGate(s, chrony_ok_fn=lambda: True)
    ok, reason = gate.check_can_transmit()
    assert ok
    assert reason is None


def test_no_callsign_blocks():
    s = _state(callsign=N0CALL, tx_allowed=False)
    gate = TxSafetyGate(s, chrony_ok_fn=lambda: True)
    ok, reason = gate.check_can_transmit()
    assert not ok
    assert "callsign" in reason.lower()


def test_no_grid_blocks():
    s = _state(callsign="K1ABC", grid="", tx_allowed=False)
    gate = TxSafetyGate(s, chrony_ok_fn=lambda: True)
    ok, reason = gate.check_can_transmit()
    assert not ok
    assert "grid" in reason.lower()


def test_chrony_unsynced_blocks():
    s = _state()
    gate = TxSafetyGate(s, chrony_ok_fn=lambda: False)
    ok, reason = gate.check_can_transmit()
    assert not ok
    assert "time" in reason.lower() or "sync" in reason.lower()


# ── Emergency override path ─────────────────────────────────────────


def test_emergency_override_bypasses_callsign_and_grid():
    """Once emergency override fires, N0CALL + missing grid are OK
    *if* GPS lock is present. (Grid will be sourced from GPS.)"""
    from microjs8.gps.types import FixKind, GpsFix
    s = _state(callsign=N0CALL, grid="", tx_allowed=False)
    s.trigger_emergency_override()
    s.set_gps(GpsFix(
        kind=FixKind.FIX_3D,
        lat=42.5, lon=-83.0, altitude_m=200.0,
        speed_mps=None, track_deg=None, hdop=1.5,
        fix_time=None, satellites_used=8,
        received_at=0.0,
    ))
    gate = TxSafetyGate(s, chrony_ok_fn=lambda: True)
    ok, reason = gate.check_can_transmit()
    assert ok, f"expected emergency override to allow TX, got: {reason}"


def test_emergency_override_still_requires_gps():
    """SOS without a real grid is useless — block until we have it."""
    s = _state(callsign=N0CALL, grid="", tx_allowed=False)
    s.trigger_emergency_override()
    # No GPS fix set.
    gate = TxSafetyGate(s, chrony_ok_fn=lambda: True)
    ok, reason = gate.check_can_transmit()
    assert not ok
    assert "gps" in reason.lower()


def test_emergency_override_still_requires_chrony():
    """Even in emergency, slot timing matters — JS8 protocol is
    UTC-aligned and a misaligned TX won't decode."""
    from microjs8.gps.types import FixKind, GpsFix
    s = _state(callsign=N0CALL, grid="", tx_allowed=False)
    s.trigger_emergency_override()
    s.set_gps(GpsFix(
        kind=FixKind.FIX_3D,
        lat=42.5, lon=-83.0, altitude_m=200.0,
        speed_mps=None, track_deg=None, hdop=1.5,
        fix_time=None, satellites_used=8,
        received_at=0.0,
    ))
    gate = TxSafetyGate(s, chrony_ok_fn=lambda: False)
    ok, reason = gate.check_can_transmit()
    assert not ok
    assert "time" in reason.lower() or "sync" in reason.lower()


# ── Default chrony probe ─────────────────────────────────────────────


def test_default_chrony_ok_handles_missing_binary(monkeypatch):
    """When chronyc isn't on PATH, we must NOT crash — and we return
    False (defensive: never TX if we can't verify time discipline)."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    from microjs8.tx.safety import default_chrony_ok, _chrony_cache
    # Reset cache so we hit the missing-binary path.
    _chrony_cache["checked_at"] = 0.0
    _chrony_cache["result"] = False
    assert default_chrony_ok() is False


def test_default_chrony_ok_parses_normal_status(monkeypatch):
    """When chronyc reports Normal leap + valid Reference ID, return True."""
    sample_output = """\
Reference ID    : C0A80101 (gps.local)
Stratum         : 1
Ref time (UTC)  : Tue Apr 29 10:00:00 2026
System time     : 0.000123456 seconds slow of NTP time
Last offset     : -0.000050 seconds
RMS offset      : 0.000123 seconds
Frequency       : 0.025 ppm slow
Residual freq   : -0.001 ppm
Skew            : 0.150 ppm
Root delay      : 0.001234 seconds
Root dispersion : 0.000567 seconds
Update interval : 64.2 seconds
Leap status     : Normal
"""
    class _FakeResult:
        returncode = 0
        stdout = sample_output
        stderr = ""

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/chronyc")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeResult())
    from microjs8.tx.safety import default_chrony_ok, _chrony_cache
    _chrony_cache["checked_at"] = 0.0
    _chrony_cache["result"] = False
    assert default_chrony_ok() is True


def test_default_chrony_ok_returns_false_for_zero_refid(monkeypatch):
    """All-zeros Reference ID means chrony hasn't picked a source yet."""
    sample_output = """\
Reference ID    : 00000000 ()
Leap status     : Normal
"""
    class _FakeResult:
        returncode = 0
        stdout = sample_output
        stderr = ""

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/chronyc")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeResult())
    from microjs8.tx.safety import default_chrony_ok, _chrony_cache
    _chrony_cache["checked_at"] = 0.0
    _chrony_cache["result"] = False
    assert default_chrony_ok() is False


def test_default_chrony_ok_returns_false_when_not_normal_leap(monkeypatch):
    sample_output = """\
Reference ID    : C0A80101 (gps.local)
Leap status     : Insert second
"""
    class _FakeResult:
        returncode = 0
        stdout = sample_output
        stderr = ""

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/chronyc")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeResult())
    from microjs8.tx.safety import default_chrony_ok, _chrony_cache
    _chrony_cache["checked_at"] = 0.0
    _chrony_cache["result"] = False
    assert default_chrony_ok() is False


def test_default_chrony_ok_handles_subprocess_error(monkeypatch):
    """chronyc raises → False (defensive)."""
    import subprocess
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/chronyc")
    def boom(*a, **kw):
        raise OSError("chronyc broke")
    monkeypatch.setattr("subprocess.run", boom)
    from microjs8.tx.safety import default_chrony_ok, _chrony_cache
    _chrony_cache["checked_at"] = 0.0
    _chrony_cache["result"] = False
    assert default_chrony_ok() is False
