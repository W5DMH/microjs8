"""Tests for the Phase 6 battery fuel-gauge reader.

Same testability pattern as Phase 5's display module:
``find_battery_dir`` and ``read_battery_state`` accept an injectable
root path (``base_path=tmp_path``), so the host suite can exercise
discovery + parsing without touching real ``/sys/class/power_supply``.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Optional

import pytest

from microjs8.power.battery import (
    BatteryReader,
    BatteryState,
    CRITICAL_BATTERY_PCT,
    LOW_BATTERY_PCT,
    find_battery_dir,
    read_battery_state,
)


# ── BatteryState derived flags ───────────────────────────────────────


def test_battery_state_charging_glyph_and_flags():
    s = BatteryState(80, 4.05, 250.0, 28.0, "Charging")
    assert s.is_charging is True
    assert s.is_discharging is False
    assert s.is_full is False
    assert s.is_low is False
    assert s.is_critical is False
    assert s.status_glyph == "↑"


def test_battery_state_discharging_glyph_and_flags():
    s = BatteryState(73, 3.95, -180.0, 27.0, "Discharging")
    assert s.is_charging is False
    assert s.is_discharging is True
    assert s.status_glyph == "↓"


def test_battery_state_full_glyph():
    s = BatteryState(100, 4.20, 0.0, 25.0, "Full")
    assert s.is_full is True
    assert s.status_glyph == "="


def test_battery_state_unknown_glyph():
    s = BatteryState(50, 3.85, 0.0, 26.0, "Unknown")
    assert s.status_glyph == "?"


def test_battery_state_low_threshold_only_when_discharging():
    """Spec §6.11: 'low' (amber) is *discharging at ≤15%*. The
    same percentage while charging is NOT low — operator is
    actively addressing it."""
    discharging_low = BatteryState(LOW_BATTERY_PCT, 3.7, -150.0, 25.0, "Discharging")
    charging_same_pct = BatteryState(LOW_BATTERY_PCT, 3.7, 200.0, 25.0, "Charging")
    assert discharging_low.is_low is True
    assert charging_same_pct.is_low is False


def test_battery_state_critical_threshold_only_when_discharging():
    """Spec §6.11: critical (red, TX gated) is discharging at ≤5%."""
    discharging_critical = BatteryState(CRITICAL_BATTERY_PCT, 3.5, -120.0, 25.0, "Discharging")
    charging_same_pct = BatteryState(CRITICAL_BATTERY_PCT, 3.5, 200.0, 25.0, "Charging")
    assert discharging_critical.is_critical is True
    assert charging_same_pct.is_critical is False


def test_battery_state_critical_implies_low():
    """A 4% discharging state should also satisfy is_low — colour
    threshold logic in HOME picks the most-severe colour first."""
    s = BatteryState(4, 3.45, -100.0, 25.0, "Discharging")
    assert s.is_critical is True
    assert s.is_low is True


def test_battery_state_normal_above_low_threshold():
    s = BatteryState(LOW_BATTERY_PCT + 1, 3.8, -150.0, 25.0, "Discharging")
    assert s.is_low is False
    assert s.is_critical is False


# ── find_battery_dir discovery ───────────────────────────────────────


def _mk_dir(parent: Path, name: str) -> Path:
    p = parent / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_find_battery_dir_picks_bq27_directory(tmp_path: Path):
    psupply = tmp_path / "power_supply"
    psupply.mkdir()
    _mk_dir(psupply, "bq27220-0")
    _mk_dir(psupply, "AC")            # other supply present
    found = find_battery_dir(base_path=psupply)
    assert found == psupply / "bq27220-0"


def test_find_battery_dir_handles_alternate_naming(tmp_path: Path):
    """The kernel may name the battery 'bq27220-battery' (no I2C
    addr suffix) on some platforms. The fragment match handles it."""
    psupply = tmp_path / "power_supply"
    psupply.mkdir()
    _mk_dir(psupply, "bq27220-battery")
    found = find_battery_dir(base_path=psupply)
    assert found is not None
    assert found.name == "bq27220-battery"


def test_find_battery_dir_case_insensitive(tmp_path: Path):
    """Older kernels may capitalize 'BQ27' — match anyway."""
    psupply = tmp_path / "power_supply"
    psupply.mkdir()
    _mk_dir(psupply, "BQ27220-0")
    found = find_battery_dir(base_path=psupply)
    assert found is not None


def test_find_battery_dir_returns_none_when_no_match(tmp_path: Path):
    psupply = tmp_path / "power_supply"
    psupply.mkdir()
    _mk_dir(psupply, "AC")
    _mk_dir(psupply, "USB")
    assert find_battery_dir(base_path=psupply) is None


def test_find_battery_dir_returns_none_when_root_missing(tmp_path: Path):
    """A stripped container or kernel without power_supply support
    has no /sys/class/power_supply directory at all. Don't crash."""
    missing = tmp_path / "no_such_dir"
    assert find_battery_dir(base_path=missing) is None


def test_find_battery_dir_picks_lowest_sorted_when_multiple(tmp_path: Path):
    """Two BQ27 chips on the same bus is unusual but we must be
    deterministic. Test that the ordering is name-sorted."""
    psupply = tmp_path / "power_supply"
    psupply.mkdir()
    _mk_dir(psupply, "bq27220-1")
    _mk_dir(psupply, "bq27220-0")
    found = find_battery_dir(base_path=psupply)
    assert found is not None
    assert found.name == "bq27220-0"


# ── read_battery_state parsing ───────────────────────────────────────


def _write_battery_dir(
    parent: Path,
    *,
    capacity: int = 73,
    voltage_uv: int = 3950000,
    current_ua: int = -180000,
    temp_decic: int = 270,
    status: str = "Discharging",
    name: str = "bq27220-0",
    skip: tuple[str, ...] = (),
) -> Path:
    """Build a fake battery sysfs tree. Files in ``skip`` aren't created."""
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    if "capacity" not in skip:
        (d / "capacity").write_text(f"{capacity}\n")
    if "voltage_now" not in skip:
        (d / "voltage_now").write_text(f"{voltage_uv}\n")
    if "current_now" not in skip:
        (d / "current_now").write_text(f"{current_ua}\n")
    if "temp" not in skip:
        (d / "temp").write_text(f"{temp_decic}\n")
    if "status" not in skip:
        (d / "status").write_text(f"{status}\n")
    return d


def test_read_battery_state_parses_all_fields(tmp_path: Path):
    d = _write_battery_dir(tmp_path)
    s = read_battery_state(d)
    assert s.capacity == 73
    assert s.voltage_v == pytest.approx(3.95)
    assert s.current_ma == pytest.approx(-180.0)
    assert s.temperature_c == pytest.approx(27.0)
    assert s.status == "Discharging"


def test_read_battery_state_clamps_out_of_range_capacity(tmp_path: Path):
    """Some BQ27 drivers transiently return 101% during init.
    Clamp to 100 so the UI math stays sane."""
    d = _write_battery_dir(tmp_path, capacity=101)
    s = read_battery_state(d)
    assert s.capacity == 100


def test_read_battery_state_clamps_negative_capacity(tmp_path: Path):
    d = _write_battery_dir(tmp_path, capacity=-5)
    s = read_battery_state(d)
    assert s.capacity == 0


def test_read_battery_state_tolerates_missing_voltage(tmp_path: Path):
    """voltage_now is optional in the kernel BQ27 driver. Default
    cleanly to 0.0 V rather than failing the whole snapshot."""
    d = _write_battery_dir(tmp_path, skip=("voltage_now",))
    s = read_battery_state(d)
    assert s.voltage_v == 0.0
    # Other fields still parsed correctly.
    assert s.capacity == 73


def test_read_battery_state_tolerates_missing_current(tmp_path: Path):
    d = _write_battery_dir(tmp_path, skip=("current_now",))
    s = read_battery_state(d)
    assert s.current_ma == 0.0


def test_read_battery_state_tolerates_missing_temp(tmp_path: Path):
    """temp may be absent on simpler fuel gauges; default to NaN
    so consumers can decide how to render it."""
    d = _write_battery_dir(tmp_path, skip=("temp",))
    s = read_battery_state(d)
    assert math.isnan(s.temperature_c)


def test_read_battery_state_raises_on_missing_capacity(tmp_path: Path):
    """capacity is mandatory — it's the one field every Linux
    power_supply driver guarantees. If it's missing, something is
    badly wrong and we raise rather than report a fake 0%."""
    d = _write_battery_dir(tmp_path, skip=("capacity",))
    with pytest.raises(RuntimeError, match=r"capacity"):
        read_battery_state(d)


def test_read_battery_state_raises_on_missing_status(tmp_path: Path):
    """status is mandatory — without it we can't decide
    is_charging/is_discharging."""
    d = _write_battery_dir(tmp_path, skip=("status",))
    with pytest.raises(RuntimeError, match=r"status"):
        read_battery_state(d)


def test_read_battery_state_raises_on_malformed_capacity(tmp_path: Path):
    d = _write_battery_dir(tmp_path)
    (d / "capacity").write_text("not_a_number\n")
    with pytest.raises(RuntimeError, match=r"capacity"):
        read_battery_state(d)


# ── BatteryReader async polling ──────────────────────────────────────


class _FakeUIState:
    """Minimal UIState stand-in for BatteryReader testing.

    Records every set_battery() call so tests can assert the timing
    and contents of each poll without needing the real UIState.
    """

    def __init__(self) -> None:
        self.calls: list[Optional[BatteryState]] = []

    def set_battery(self, state: Optional[BatteryState]) -> None:
        self.calls.append(state)


@pytest.mark.asyncio
async def test_battery_reader_polls_and_updates_state(tmp_path: Path):
    psupply = tmp_path / "power_supply"
    _write_battery_dir(psupply, capacity=85, status="Charging")

    ui = _FakeUIState()
    reader = BatteryReader(
        ui,
        base_path=psupply,
        poll_interval_s=0.01,         # fast for tests
    )
    loop = asyncio.get_running_loop()
    reader.start(loop)
    # Let it poll a few times.
    await asyncio.sleep(0.05)
    reader.stop()

    assert len(ui.calls) >= 2
    last = ui.calls[-1]
    assert last is not None
    assert last.capacity == 85
    assert last.status == "Charging"


@pytest.mark.asyncio
async def test_battery_reader_handles_late_appearing_battery(tmp_path: Path):
    """Discovery is retried when the battery directory doesn't exist
    at startup. We test the fast-retry path by overriding the
    REDISCOVER_INTERVAL_S to something tiny."""
    psupply = tmp_path / "power_supply"
    psupply.mkdir()                   # empty — no BQ27 yet

    ui = _FakeUIState()
    reader = BatteryReader(
        ui, base_path=psupply, poll_interval_s=0.01,
    )
    # Speed up the rediscovery interval just for this test.
    reader.REDISCOVER_INTERVAL_S = 0.02   # type: ignore[misc]

    loop = asyncio.get_running_loop()
    reader.start(loop)
    await asyncio.sleep(0.05)             # battery still missing
    assert ui.calls == []                 # no successful poll yet

    # Now create the battery directory mid-flight.
    _write_battery_dir(psupply, capacity=42, status="Discharging")
    await asyncio.sleep(0.10)             # let rediscovery + poll fire
    reader.stop()

    assert len(ui.calls) >= 1
    assert ui.calls[-1].capacity == 42


@pytest.mark.asyncio
async def test_battery_reader_surfaces_unknown_after_sustained_failures(tmp_path: Path):
    """If the battery dir disappears mid-flight (chip reset, kernel
    rebind, whatever), three consecutive failed reads must push
    ``set_battery(None)`` and drop back into rediscovery."""
    psupply = tmp_path / "power_supply"
    bdir = _write_battery_dir(psupply, capacity=50, status="Discharging")

    ui = _FakeUIState()
    reader = BatteryReader(
        ui, base_path=psupply, poll_interval_s=0.01,
    )
    reader.REDISCOVER_INTERVAL_S = 0.20   # type: ignore[misc]

    loop = asyncio.get_running_loop()
    reader.start(loop)
    # Let it poll once successfully.
    await asyncio.sleep(0.03)
    assert any(c is not None for c in ui.calls)

    # Break the battery dir by removing the mandatory 'capacity' file.
    (bdir / "capacity").unlink()
    # Wait for FAILS_BEFORE_UNKNOWN polls (~3 polls × 0.01s + slack).
    await asyncio.sleep(0.10)
    reader.stop()

    # Last meaningful call should be None (unknown).
    assert ui.calls[-1] is None


@pytest.mark.asyncio
async def test_battery_reader_stop_is_idempotent(tmp_path: Path):
    """stop() before start() is a no-op. stop() after stop() is also
    a no-op. No exception either way."""
    ui = _FakeUIState()
    reader = BatteryReader(ui, base_path=tmp_path)
    reader.stop()                         # before start
    loop = asyncio.get_running_loop()
    reader.start(loop)
    reader.stop()
    reader.stop()                         # second stop


# ── Integration with UIState ─────────────────────────────────────────


def test_uistate_set_battery_marks_dirty_on_change():
    """UIState.set_battery should set the dirty flag so the render
    thread redraws when capacity or status changes."""
    from microjs8.ui.state import UIState
    ui = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    ui.consume_dirty()                # clear initial dirty
    s = BatteryState(80, 4.05, 250.0, 28.0, "Charging")
    ui.set_battery(s)
    assert ui.consume_dirty() is True


def test_uistate_set_battery_skips_dirty_on_irrelevant_change():
    """A 1 Hz poll can deliver the same capacity+status with a
    slightly different voltage — that shouldn't redraw HOME."""
    from microjs8.ui.state import UIState
    ui = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    s1 = BatteryState(80, 4.05, 250.0, 28.0, "Charging")
    ui.set_battery(s1)
    ui.consume_dirty()
    s2 = BatteryState(80, 4.06, 245.0, 28.1, "Charging")  # capacity & status same
    ui.set_battery(s2)
    assert ui.consume_dirty() is False


def test_uistate_set_battery_to_none_marks_dirty():
    """Going from known to unknown is visibly different on HOME
    ('--' replaces the percentage), so it must trigger redraw."""
    from microjs8.ui.state import UIState
    ui = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    ui.set_battery(BatteryState(80, 4.05, 250.0, 28.0, "Charging"))
    ui.consume_dirty()
    ui.set_battery(None)
    assert ui.consume_dirty() is True


def test_uistate_set_battery_none_to_none_does_not_dirty():
    from microjs8.ui.state import UIState
    ui = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    ui.consume_dirty()
    ui.set_battery(None)
    # _battery was already None at startup; setting to None again is a no-op.
    assert ui.consume_dirty() is False


def test_uistate_snapshot_includes_battery():
    from microjs8.ui.state import UIState
    ui = UIState(callsign="K1ABC", grid="FN42", tx_allowed=True)
    snap = ui.snapshot()
    assert snap.battery is None
    s = BatteryState(75, 3.95, -150.0, 26.0, "Discharging")
    ui.set_battery(s)
    snap2 = ui.snapshot()
    assert snap2.battery is s
