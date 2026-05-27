"""Tests for v0.0.13 [hmi] keyboard auto-mode (plug-and-play).

Three test categories:
  1. ``HmiConfig.keyboard`` default flips from "usb" (v0.0.12) to "auto"
  2. ``_validate_hmi_keyboard`` accepts "auto" as a valid choice
  3. ``_from_dict`` defaults to "auto" when [hmi] keyboard is omitted

These complement the existing tests in test_config_hmi.py which still
cover the v0.0.12 explicit-mode behavior; nothing in those tests
regresses since "usb" and "uart" remain valid choices.
"""

from __future__ import annotations

import pytest

from microjs8.config import (
    Config,
    ConfigError,
    HmiConfig,
    _from_dict,
    _validate_hmi_keyboard,
)


class TestAutoModeIsDefault:
    """v0.0.13: HmiConfig default keyboard is "auto", not "usb"."""

    def test_dataclass_default(self) -> None:
        hmi = HmiConfig()
        assert hmi.keyboard == "auto"

    def test_config_default_has_auto_hmi(self) -> None:
        cfg = Config()
        assert cfg.hmi.keyboard == "auto"

    def test_uart_fields_default_still_present(self) -> None:
        # Auto mode preserves the UART config defaults so the daemon
        # knows what port/baud to try when /dev/serial0 is openable.
        hmi = HmiConfig()
        assert hmi.uart_device == "/dev/serial0"
        assert hmi.uart_baud == 115200


class TestValidateAcceptsAuto:
    """_validate_hmi_keyboard recognizes "auto" as a valid choice."""

    def test_auto_accepted(self) -> None:
        assert _validate_hmi_keyboard("auto") == "auto"

    def test_auto_case_insensitive(self) -> None:
        assert _validate_hmi_keyboard("AUTO") == "auto"
        assert _validate_hmi_keyboard("Auto") == "auto"

    def test_auto_whitespace_stripped(self) -> None:
        assert _validate_hmi_keyboard("  auto  ") == "auto"

    def test_usb_still_valid(self) -> None:
        # Backward compat: pre-v0.0.13 configs with "usb" continue to
        # validate and behave identically.
        assert _validate_hmi_keyboard("usb") == "usb"

    def test_uart_still_valid(self) -> None:
        # v0.0.12 configs with "uart" likewise continue to work.
        assert _validate_hmi_keyboard("uart") == "uart"

    def test_unknown_still_rejected(self) -> None:
        with pytest.raises(ConfigError, match="not recognized"):
            _validate_hmi_keyboard("magic")


class TestFromDictDefaultsToAuto:
    """Loading a config without [hmi] keyboard set falls back to auto."""

    def test_missing_section_uses_auto(self, tmp_path) -> None:
        # Pre-v0.0.13 configs don't have [hmi]; the default is "auto"
        # so they get plug-and-play behavior without any edit.
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "auto"

    def test_missing_keyboard_field_uses_auto(self, tmp_path) -> None:
        # [hmi] section present but no keyboard= line; defaults to auto.
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
            "hmi": {"uart_baud": 230400},  # only uart_baud set
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "auto"
        assert cfg.hmi.uart_baud == 230400  # other fields preserved

    def test_explicit_auto(self, tmp_path) -> None:
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
            "hmi": {"keyboard": "auto"},
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "auto"

    def test_explicit_usb_overrides_auto(self, tmp_path) -> None:
        # Operator wants strict single-backend behavior.
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
            "hmi": {"keyboard": "usb"},
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "usb"

    def test_explicit_uart_overrides_auto(self, tmp_path) -> None:
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
            "hmi": {"keyboard": "uart"},
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "uart"
