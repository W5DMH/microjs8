"""Tests for microjs8.paths."""

from __future__ import annotations

import os
from pathlib import Path

from microjs8 import paths


def test_default_paths_match_spec():
    """Production paths must match the spec exactly."""
    # Clear overrides for this assertion
    os.environ.pop("MICROJS8_DATA_DIR", None)
    os.environ.pop("MICROJS8_ETC_DIR", None)

    assert paths.data_dir() == Path("/var/microjs8")
    assert paths.etc_dir() == Path("/etc/microjs8")
    assert paths.config_path() == Path("/var/microjs8/config.toml")
    assert paths.default_config_path() == Path("/etc/microjs8/config.toml")
    assert paths.log_dir() == Path("/var/microjs8/log")
    assert paths.log_file() == Path("/var/microjs8/log/microjs8.log")
    assert paths.db_path() == Path("/var/microjs8/messages.db")


def test_env_overrides(tmp_path, monkeypatch):
    """Env vars must redirect both data and etc roots."""
    data = tmp_path / "data"
    etc = tmp_path / "etc"
    monkeypatch.setenv("MICROJS8_DATA_DIR", str(data))
    monkeypatch.setenv("MICROJS8_ETC_DIR", str(etc))

    assert paths.data_dir() == data
    assert paths.etc_dir() == etc
    assert paths.config_path() == data / "config.toml"
    assert paths.default_config_path() == etc / "config.toml"
    assert paths.log_dir() == data / "log"


def test_ensure_writable_dirs_creates_tree(tmp_path, monkeypatch):
    """ensure_writable_dirs() must create both data dir and log dir."""
    monkeypatch.setenv("MICROJS8_DATA_DIR", str(tmp_path / "data"))
    paths.ensure_writable_dirs()
    assert paths.data_dir().is_dir()
    assert paths.log_dir().is_dir()


def test_ensure_writable_dirs_idempotent(tmp_path, monkeypatch):
    """Calling twice must not error even though dirs already exist."""
    monkeypatch.setenv("MICROJS8_DATA_DIR", str(tmp_path / "data"))
    paths.ensure_writable_dirs()
    paths.ensure_writable_dirs()
    assert paths.data_dir().is_dir()
