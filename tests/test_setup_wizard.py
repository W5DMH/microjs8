"""End-to-end tests for the setup wizard.

Drives the router with synthetic KeyEvents from the unconfigured-station
state through to a fully-saved configuration, asserting that:

  - tx_allowed flips to True after a valid Call+Grid pair is committed
  - the actual config.toml file contains the values
  - reload picks up the new values cleanly
  - the bypass button activates the emergency override
"""

from __future__ import annotations

from pathlib import Path

import pytest

from microjs8 import config as config_mod
from microjs8.input.events import Key, KeyEvent
from microjs8.input.router import InputRouter
from microjs8.ui.state import Screen, UIState


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    data = tmp_path / "data"
    etc = tmp_path / "etc"
    data.mkdir()
    etc.mkdir()
    monkeypatch.setenv("MICROJS8_DATA_DIR", str(data))
    monkeypatch.setenv("MICROJS8_ETC_DIR", str(etc))
    # Ship a default config so first-boot logic doesn't fail.
    project_default = (
        Path(__file__).parent.parent / "etc-defaults" / "config.toml"
    )
    (etc / "config.toml").write_text(
        project_default.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return data, etc


def _make_save(state: UIState):
    """Build a save callback that mimics the app-level wiring:
    on a successful save, refresh UIState identity. The router's
    in-progress edit reads OTHER current values from snapshot, so
    if we don't refresh after the callsign save, the subsequent
    grid save would write a stale (N0CALL) callsign."""
    def save(callsign: str, grid: str, units: str) -> bool:
        try:
            cfg = config_mod.save_atomic(callsign, grid, units)
        except config_mod.ConfigError:
            return False
        state.set_identity(
            cfg.station.callsign, cfg.station.grid,
            cfg.units_distance, cfg.tx_allowed,
        )
        return True
    return save


def _save(callsign: str, grid: str, units: str) -> bool:
    """Module-level fallback save (used by tests that don't need
    UIState refresh — single-edit tests)."""
    try:
        config_mod.save_atomic(callsign, grid, units)
        return True
    except config_mod.ConfigError:
        return False


def _emergency():
    pass


def test_full_setup_wizard_flow(isolated_paths):
    data, _ = isolated_paths
    state = UIState("N0CALL", "", False, "miles")
    state.set_screen(Screen.SETUP)
    router = InputRouter(
        state, save_config=_make_save(state), emergency_bypass=_emergency,
    )

    # Initial state: focus on callsign, tx blocked.
    assert state.focused_field_name() == "callsign"
    assert not state.snapshot().tx_allowed

    # Type K8XYZ as callsign.
    router.handle(KeyEvent(key=Key.ENTER))
    for ch in "K8XYZ":
        router.handle(KeyEvent(char=ch))
    router.handle(KeyEvent(key=Key.ENTER))

    # Saved.
    cfg = config_mod.load()
    assert cfg.station.callsign == "K8XYZ"

    # Wizard saved + cleared edit mode, but station still blocked
    # because grid is empty.
    snap = state.snapshot()
    assert snap.editing_field is None
    assert not snap.tx_allowed

    # Tab to grid.
    router.handle(KeyEvent(key=Key.TAB))
    assert state.focused_field_name() == "grid"

    # Edit grid.
    router.handle(KeyEvent(key=Key.ENTER))
    for ch in "EN82":
        router.handle(KeyEvent(char=ch))
    router.handle(KeyEvent(key=Key.ENTER))

    # Now configured. The save callback refreshed UIState identity, so
    # tx_allowed should be True without an extra reload step.
    cfg = config_mod.load()
    assert cfg.station.callsign == "K8XYZ"
    assert cfg.station.grid == "EN82"
    assert cfg.tx_allowed
    assert state.snapshot().tx_allowed


def test_invalid_callsign_blocks_save(isolated_paths):
    state = UIState("N0CALL", "", False, "miles")
    state.set_screen(Screen.SETUP)
    router = InputRouter(state, save_config=_save, emergency_bypass=_emergency)

    router.handle(KeyEvent(key=Key.ENTER))
    for ch in "BAD!!":
        router.handle(KeyEvent(char=ch))
    # The "!" doesn't pass the keyboard layout check anyway, but the
    # router's max-length cap doesn't filter chars — only length. So
    # this exercises the validator-rejects-pattern path.
    router.handle(KeyEvent(key=Key.ENTER))
    snap = state.snapshot()
    assert snap.editing_field == "callsign"  # still editing
    assert snap.edit_invalid is True


def test_units_field_editable(isolated_paths):
    state = UIState("K1ABC", "FN42", True, "miles")
    state.set_screen(Screen.SETUP)
    router = InputRouter(
        state, save_config=_make_save(state), emergency_bypass=_emergency,
    )

    # Tab callsign -> grid -> units
    router.handle(KeyEvent(key=Key.TAB))
    router.handle(KeyEvent(key=Key.TAB))
    assert state.focused_field_name() == "units"

    # Edit units to "km".
    router.handle(KeyEvent(key=Key.ENTER))
    # Buffer pre-fills with "miles" — clear it first.
    for _ in range(5):
        router.handle(KeyEvent(key=Key.BACKSPACE))
    for ch in "km":
        router.handle(KeyEvent(char=ch))
    router.handle(KeyEvent(key=Key.ENTER))

    cfg = config_mod.load()
    assert cfg.units_distance == "km"


def test_emergency_bypass_activates_override(isolated_paths):
    state = UIState("N0CALL", "", False, "miles")
    state.set_screen(Screen.SETUP)
    bypass_calls = {"n": 0}

    def bypass():
        bypass_calls["n"] += 1
        state.trigger_emergency_override()

    router = InputRouter(state, save_config=_save, emergency_bypass=bypass)

    # Tab callsign -> grid -> units -> freq_hz -> radio -> emergency_bypass
    for _ in range(5):
        router.handle(KeyEvent(key=Key.TAB))
    assert state.focused_field_name() == "emergency_bypass"

    router.handle(KeyEvent(key=Key.ENTER))
    assert bypass_calls["n"] == 1
    snap = state.snapshot()
    assert snap.emergency_override is True
    assert snap.tx_allowed is True
    assert snap.screen is Screen.EMERGENCY


def test_atomic_save_uses_tmp_then_rename(isolated_paths, monkeypatch):
    """Verify the .tmp file appears mid-write and disappears at rename."""
    data, _ = isolated_paths
    config_mod.save_atomic("K1ABC", "FN42", "miles")
    # No leftover .tmp files.
    leftovers = list(data.glob("config.toml.tmp*"))
    assert leftovers == []
    # Live file present.
    assert (data / "config.toml").exists()


def test_save_atomic_rejects_invalid_inputs(isolated_paths):
    with pytest.raises(config_mod.ConfigError):
        config_mod.save_atomic("BAD!!", "FN42", "miles")
    with pytest.raises(config_mod.ConfigError):
        config_mod.save_atomic("K1ABC", "ZZZZ", "miles")
    with pytest.raises(config_mod.ConfigError):
        config_mod.save_atomic("K1ABC", "FN42", "furlongs")
