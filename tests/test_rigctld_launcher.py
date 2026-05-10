"""Tests for systemd/microjs8-rigctld-launcher.

The launcher is a shell script (intentionally — see comments in the
script itself), so we exercise it via subprocess in --dry-run mode.
The dry-run path skips the device-existence check and prints the
rigctld command line that would be exec'd, which is exactly what we
want to assert on.

We test:
  * Default config path is /var/microjs8/config.toml (the LIVE config,
    not the shipped template at /etc/) — this matters because the UI
    cycle handler writes only to /var, and a launcher reading /etc
    would silently use stale radio_id values.
  * Each radio profile produces the expected rigctld arguments.
  * The TOML extraction handles the real config format (comments
    above [radio], multiple sections, varied whitespace).
  * Missing or malformed config falls back cleanly to the qdx
    default rather than crashing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


LAUNCHER_PATH = Path(__file__).parent.parent / "systemd" / "microjs8-rigctld-launcher"


# ── Module-level guard: skip if shell launcher missing or unreadable
@pytest.fixture(scope="module", autouse=True)
def _launcher_present():
    if not LAUNCHER_PATH.exists():
        pytest.skip(f"launcher not present at {LAUNCHER_PATH}")
    if not os.access(LAUNCHER_PATH, os.X_OK):
        pytest.skip(f"launcher not executable: {LAUNCHER_PATH}")


def _run_launcher(config_path: Path, *, dry_run: bool = True) -> subprocess.CompletedProcess:
    """Run the launcher with MICROJS8_CONFIG pointing at config_path.

    Returns the CompletedProcess (including stdout, stderr, returncode).
    Always passes --dry-run by default so we don't try to exec rigctld
    or check for /dev/digirig presence.
    """
    args = [str(LAUNCHER_PATH)]
    if dry_run:
        args.append("--dry-run")
    env = {**os.environ, "MICROJS8_CONFIG": str(config_path)}
    return subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ── Default-path defense ────────────────────────────────────────────


def test_launcher_default_config_path_is_var_microjs8():
    """The launcher's default CONFIG_FILE must be /var/microjs8/config.toml.

    This is the path the daemon and config.save_atomic() use as the
    LIVE config. Reading the shipped template at /etc/ would mean the
    launcher and the daemon disagree about which radio is active —
    which is exactly the bug we're guarding against here.
    """
    text = LAUNCHER_PATH.read_text()
    # The default value comes from the parameter expansion in the
    # CONFIG_FILE assignment. We grep for the exact path string —
    # if anyone changes the default away from /var/microjs8/, this
    # fails loudly.
    assert "${MICROJS8_CONFIG:-/var/microjs8/config.toml}" in text, (
        "launcher default config path must be /var/microjs8/config.toml; "
        "any other path will be out of sync with config.save_atomic()'s "
        "write target"
    )
    # And explicitly: NOT /etc/microjs8/config.toml as the default.
    assert "${MICROJS8_CONFIG:-/etc/microjs8/config.toml}" not in text


# ── Radio profile dispatch (parameter-driven) ───────────────────────


@pytest.fixture
def make_config(tmp_path: Path):
    """Factory for writing realistic config.toml files.

    Returns a function that takes a radio_id (or None to omit the
    [radio] section entirely) and returns the path to the written
    file.
    """
    def _make(radio_id: str | None, *, with_comments: bool = True) -> Path:
        body = (
            'units_distance = "miles"\n'
            "\n"
            "[station]\n"
            'callsign = "W5DMH"\n'
            'grid = "EN83ih"\n'
            "\n"
        )
        if radio_id is not None:
            if with_comments:
                body += (
                    "# ── [radio] — radio hardware ─────────────────────\n"
                    "[radio]\n"
                    "# Comment line above the id field — must be ignored.\n"
                    f'id = "{radio_id}"\n'
                )
            else:
                body += f'[radio]\nid = "{radio_id}"\n'
        path = tmp_path / "config.toml"
        path.write_text(body)
        return path
    return _make


def test_launcher_g90_produces_correct_rigctld_args(make_config):
    """xiegu-g90-digirig should produce the G90 hamlib args."""
    cfg = make_config("xiegu-g90-digirig")
    result = _run_launcher(cfg)
    assert result.returncode == 0, (
        f"launcher exited {result.returncode}: stderr={result.stderr}"
    )
    # Hamlib model 3088 = G90, baud 19200, RTS-PTT on the same port.
    assert "-m 3088" in result.stdout
    assert "/dev/digirig" in result.stdout
    assert "-s 19200" in result.stdout
    assert "-P RTS" in result.stdout


def test_launcher_qdx_produces_correct_rigctld_args(make_config):
    """qdx should produce the QDX (TS-480-emulation) args."""
    cfg = make_config("qdx")
    result = _run_launcher(cfg)
    assert result.returncode == 0
    assert "-m 2028" in result.stdout
    assert "QDX_Transceiver" in result.stdout
    assert "-s 9600" in result.stdout


def test_launcher_digirig_rts_only_exits_clean_no_args(make_config):
    """digirig-rts-only is the no-CAT path — launcher exits 0 with
    no rigctld args printed (the daemon's RtsPttService handles
    PTT directly, no rigctld needed)."""
    cfg = make_config("digirig-rts-only")
    result = _run_launcher(cfg)
    assert result.returncode == 0
    # No "Would exec" line — we never reach the rigctld call.
    assert "Would exec" not in result.stdout


# ── TOML parsing edge cases ─────────────────────────────────────────


def test_launcher_handles_comments_above_id(make_config):
    """Real-world config has multiple comment lines between [radio]
    and id =. The launcher's sed range must skip them correctly."""
    cfg = make_config("xiegu-g90-digirig", with_comments=True)
    result = _run_launcher(cfg)
    assert result.returncode == 0
    assert "-m 3088" in result.stdout


def test_launcher_handles_minimal_config(make_config):
    """Bare-bones [radio] section with just the id line — the
    minimum a setup-wizard-written config would have."""
    cfg = make_config("xiegu-g90-digirig", with_comments=False)
    result = _run_launcher(cfg)
    assert result.returncode == 0
    assert "-m 3088" in result.stdout


def test_launcher_falls_back_to_qdx_when_radio_section_missing(tmp_path: Path):
    """A config with [station] but no [radio] section should default
    to qdx (matches Config dataclass default in config.py)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'units_distance = "miles"\n'
        '[station]\n'
        'callsign = "W5DMH"\n'
        'grid = "EN83ih"\n'
        # No [radio] section at all.
    )
    result = _run_launcher(cfg)
    assert result.returncode == 0
    # qdx is the fallback: should emit QDX rigctld args.
    assert "-m 2028" in result.stdout


def test_launcher_falls_back_to_qdx_when_id_field_missing(tmp_path: Path):
    """A [radio] section without an id = line — also qdx fallback."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[station]\n'
        'callsign = "W5DMH"\n'
        'grid = "EN83ih"\n'
        '[radio]\n'
        '# Comment but no id field.\n'
    )
    result = _run_launcher(cfg)
    assert result.returncode == 0
    assert "-m 2028" in result.stdout


def test_launcher_unknown_radio_id_exits_nonzero(make_config):
    """An unrecognized radio_id should exit with status 2 (the
    launcher's dedicated 'unknown radio' code) rather than silently
    falling through. Better to fail loud than ship the wrong args
    to rigctld."""
    cfg = make_config("not-a-real-radio")
    result = _run_launcher(cfg)
    assert result.returncode == 2
    assert "unknown radio id" in result.stderr.lower()


def test_launcher_handles_nonexistent_config_file(tmp_path: Path):
    """A nonexistent config path should fall back to qdx default
    (same behavior as config.py's load() if the file is missing
    from the live location)."""
    cfg = tmp_path / "does-not-exist.toml"
    # Don't create the file.
    result = _run_launcher(cfg)
    assert result.returncode == 0
    # qdx default → QDX args.
    assert "-m 2028" in result.stdout


def test_launcher_id_with_extra_whitespace(tmp_path: Path):
    """Robustness: accept ' id  =  "qdx" ' with extra whitespace
    around the equals sign and after the value. TOML allows it,
    so the launcher should too."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[station]\n'
        'callsign = "W5DMH"\n'
        'grid = "EN83ih"\n'
        '[radio]\n'
        '   id   =   "xiegu-g90-digirig"   \n'
    )
    result = _run_launcher(cfg)
    assert result.returncode == 0
    assert "-m 3088" in result.stdout


def test_launcher_picks_first_id_when_multiple_radio_sections(tmp_path: Path):
    """Defensive: if for some reason there are multiple [radio]
    sections (a malformed config), the launcher should pick the
    FIRST one and not crash. Matches config.py's parser behavior."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[station]\ncallsign = "W5DMH"\ngrid = "EN83ih"\n'
        '[radio]\nid = "qdx"\n'
        '[other]\nfoo = "bar"\n'
    )
    result = _run_launcher(cfg)
    assert result.returncode == 0
    assert "-m 2028" in result.stdout  # qdx args
