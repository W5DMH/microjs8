"""Tests for minijs8.config."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from minijs8 import config


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect data + etc dirs into a tmp tree."""
    data = tmp_path / "data"
    etc = tmp_path / "etc"
    data.mkdir()
    etc.mkdir()
    monkeypatch.setenv("MINIJS8_DATA_DIR", str(data))
    monkeypatch.setenv("MINIJS8_ETC_DIR", str(etc))
    return data, etc


@pytest.fixture
def shipped_default(isolated_paths):
    """Place the project's default config at the etc dir location."""
    _, etc = isolated_paths
    project_default = (
        Path(__file__).parent.parent / "etc-defaults" / "config.toml"
    )
    shutil.copy2(project_default, etc / "config.toml")
    return etc / "config.toml"


# ── First-boot behaviour ─────────────────────────────────────────────────────


def test_first_boot_copies_default(isolated_paths, shipped_default):
    """When live config is missing, default must be copied in."""
    data, _ = isolated_paths
    live = data / "config.toml"
    assert not live.exists()

    cfg = config.load()

    assert live.exists()
    # Loaded config matches the shipped sentinel values.
    assert cfg.station.callsign == "N0CALL"
    assert cfg.station.grid == ""
    assert not cfg.tx_allowed


def test_first_boot_fails_if_no_default(isolated_paths):
    """No live config AND no shipped default → ConfigError."""
    with pytest.raises(config.ConfigError, match="image appears to be incomplete"):
        config.load()


def test_existing_live_config_not_overwritten(isolated_paths, shipped_default):
    """If a live config exists, defaults must NOT be re-copied over it."""
    data, _ = isolated_paths
    live = data / "config.toml"
    live.write_text(
        '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n', encoding="utf-8"
    )
    mtime_before = live.stat().st_mtime

    cfg = config.load()

    # Same mtime means file untouched.
    assert live.stat().st_mtime == mtime_before
    assert cfg.station.callsign == "K1ABC"
    assert cfg.station.grid == "FN42"


# ── Validation ───────────────────────────────────────────────────────────────


def _write(live: Path, body: str) -> None:
    live.write_text(body, encoding="utf-8")


def test_valid_minimal_config(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n')
    cfg = config.load()
    assert cfg.station.callsign == "K1ABC"
    assert cfg.station.grid == "FN42"
    assert cfg.tx_allowed
    assert cfg.units_distance == "miles"


def test_valid_6char_grid(isolated_paths):
    data, _ = isolated_paths
    _write(
        data / "config.toml",
        '[station]\ncallsign = "VE3XYZ"\ngrid = "FN03dh"\n',
    )
    cfg = config.load()
    assert cfg.station.grid == "FN03dh"


def test_callsign_normalized_to_uppercase(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "k1abc"\ngrid = "fn42"\n')
    cfg = config.load()
    assert cfg.station.callsign == "K1ABC"
    assert cfg.station.grid == "FN42"


def test_invalid_callsign_rejected(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "!!"\ngrid = "FN42"\n')
    with pytest.raises(config.ConfigError, match="callsign"):
        config.load()


def test_invalid_grid_length_rejected(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "K1ABC"\ngrid = "FN4"\n')
    with pytest.raises(config.ConfigError, match="grid"):
        config.load()


def test_invalid_grid_pattern_rejected(isolated_paths):
    data, _ = isolated_paths
    # ZZ99 — Z is outside the A-R range for the field pair.
    _write(data / "config.toml", '[station]\ncallsign = "K1ABC"\ngrid = "ZZ99"\n')
    with pytest.raises(config.ConfigError, match="grid"):
        config.load()


def test_invalid_units_rejected(isolated_paths):
    data, _ = isolated_paths
    # Top-level keys must come before any [section] header in TOML.
    _write(
        data / "config.toml",
        'units_distance = "furlongs"\n[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n',
    )
    with pytest.raises(config.ConfigError, match="units_distance"):
        config.load()


def test_units_km_accepted(isolated_paths):
    """The non-default units value must round-trip correctly."""
    data, _ = isolated_paths
    _write(
        data / "config.toml",
        'units_distance = "km"\n[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n',
    )
    cfg = config.load()
    assert cfg.units_distance == "km"


def test_malformed_toml_rejected(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", "this is = not [valid toml")
    with pytest.raises(config.ConfigError, match="failed to parse"):
        config.load()


# ── tx_allowed gate ──────────────────────────────────────────────────────────


def test_tx_disabled_when_callsign_n0call(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "N0CALL"\ngrid = "FN42"\n')
    cfg = config.load()
    assert not cfg.tx_allowed


def test_tx_disabled_when_grid_empty(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "K1ABC"\ngrid = ""\n')
    cfg = config.load()
    assert not cfg.tx_allowed


def test_tx_enabled_when_both_set(isolated_paths):
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n')
    cfg = config.load()
    assert cfg.tx_allowed


# ── Forward-compat: unknown sections must not break the loader ───────────────


def test_unknown_section_ignored(isolated_paths):
    """Step 1 must tolerate Step 2+ config keys it doesn't yet know."""
    data, _ = isolated_paths
    _write(
        data / "config.toml",
        '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n\n'
        '[future_step]\nsome_field = 42\n',
    )
    cfg = config.load()  # must not raise
    assert cfg.station.callsign == "K1ABC"


# ── Radio identity (Step 6) ──────────────────────────────────────────────────


def test_radio_id_defaults_to_qdx(isolated_paths):
    """No [radio] section at all → defaults to 'qdx'."""
    data, _ = isolated_paths
    _write(data / "config.toml", '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n')
    cfg = config.load()
    assert cfg.radio_id == "qdx"


def test_radio_id_explicit_qdx(isolated_paths):
    data, _ = isolated_paths
    _write(
        data / "config.toml",
        '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n'
        '[radio]\nid = "qdx"\n',
    )
    cfg = config.load()
    assert cfg.radio_id == "qdx"


def test_radio_id_unknown_rejected(isolated_paths):
    data, _ = isolated_paths
    _write(
        data / "config.toml",
        '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n'
        '[radio]\nid = "ic-9700"\n',
    )
    with pytest.raises(config.ConfigError, match="unknown"):
        config.load()


def test_radio_section_must_be_table(isolated_paths):
    data, _ = isolated_paths
    # Top-level `radio = "qdx"` (not a section) forces the dict check
    # to fail. Top-level keys must come before the first [section]
    # header in TOML.
    _write(
        data / "config.toml",
        'radio = "qdx"\n'
        '[station]\ncallsign = "K1ABC"\ngrid = "FN42"\n',
    )
    with pytest.raises(config.ConfigError, match="must be a table"):
        config.load()


# ── save_atomic preserves and writes [radio] ────────────────────────


def test_save_atomic_writes_radio_section(isolated_paths, shipped_default):
    """save_atomic should always write a [radio] section to the live
    config — previously it only wrote [station] + units, which dropped
    any operator-set radio_id on every save."""
    data, _ = isolated_paths
    cfg = config.save_atomic(
        callsign="W5DMH", grid="EN83ih", units="miles", radio_id="qdx",
    )
    body = (data / "config.toml").read_text()
    assert "[radio]" in body
    assert 'id = "qdx"' in body
    assert cfg.radio_id == "qdx"


def test_save_atomic_preserves_existing_radio_id(isolated_paths, shipped_default):
    """When called WITHOUT a radio_id argument, save_atomic should
    preserve whatever was already in the live config. This protects
    operator's radio selection when they edit callsign / grid / units
    from Setup."""
    data, _ = isolated_paths
    # First save with explicit radio_id.
    config.save_atomic(
        callsign="W5DMH", grid="EN83ih", units="miles",
        radio_id="digirig-rts-only",
    )
    # Now save WITHOUT radio_id — should preserve the existing value.
    cfg = config.save_atomic(
        callsign="W5DMH", grid="EN84", units="km",
        # radio_id NOT supplied
    )
    body = (data / "config.toml").read_text()
    assert 'id = "digirig-rts-only"' in body
    assert cfg.radio_id == "digirig-rts-only"
    # Confirm the other fields DID change.
    assert cfg.station.grid == "EN84"
    assert cfg.units_distance == "km"


def test_save_atomic_falls_back_to_qdx_when_no_existing_config(
    isolated_paths, shipped_default,
):
    """If there's no live config yet (first save in this environment),
    save_atomic with no radio_id falls back to 'qdx' — the same
    default the Config dataclass uses."""
    data, _ = isolated_paths
    # No live config exists yet at this point — only the shipped default.
    # First save without radio_id should default to qdx.
    cfg = config.save_atomic(
        callsign="W5DMH", grid="EN83", units="miles",
        # no radio_id
    )
    assert cfg.radio_id == "qdx"


def test_save_atomic_validates_radio_id(isolated_paths, shipped_default):
    """save_atomic should reject invalid radio_ids the same way load()
    does — better to fail at write time than to write garbage that
    silently breaks at next daemon start."""
    with pytest.raises(config.ConfigError):
        config.save_atomic(
            callsign="W5DMH", grid="EN83", units="miles",
            radio_id="bogus-radio",
        )
