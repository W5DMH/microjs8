"""Smoke tests for minijs8.__main__."""

from __future__ import annotations

import shutil
from pathlib import Path

from minijs8 import __main__


def test_check_config_returns_zero_with_valid_config(tmp_path, monkeypatch, capsys):
    """`python -m minijs8 --check-config` must exit 0 with valid config."""
    data = tmp_path / "data"
    etc = tmp_path / "etc"
    data.mkdir()
    etc.mkdir()
    monkeypatch.setenv("MINIJS8_DATA_DIR", str(data))
    monkeypatch.setenv("MINIJS8_ETC_DIR", str(etc))

    project_default = (
        Path(__file__).parent.parent / "etc-defaults" / "config.toml"
    )
    shutil.copy2(project_default, etc / "config.toml")

    rc = __main__.main(["--check-config"])
    assert rc == 0


def test_check_config_returns_two_with_bad_config(tmp_path, monkeypatch):
    """A malformed config must produce exit code 2."""
    data = tmp_path / "data"
    etc = tmp_path / "etc"
    data.mkdir()
    etc.mkdir()
    monkeypatch.setenv("MINIJS8_DATA_DIR", str(data))
    monkeypatch.setenv("MINIJS8_ETC_DIR", str(etc))

    (data / "config.toml").write_text(
        '[station]\ncallsign = "!!!"\n', encoding="utf-8"
    )

    rc = __main__.main(["--check-config"])
    assert rc == 2
