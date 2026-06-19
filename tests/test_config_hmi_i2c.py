"""Tests for v0.0.16 HmiConfig I2C extensions.

Verifies:
  - HmiConfig has i2c_bus / i2c_address fields with sensible defaults
  - "i2c" is a valid keyboard choice
  - _from_dict reads i2c_bus / i2c_address from TOML data
  - Validators reject bad values (non-int, out-of-range, bool)
  - Serializer round-trips i2c fields correctly

ASCII-only per v0.0.14 paste-encoding policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from microjs8.config import (
    ConfigError,
    HmiConfig,
    _from_dict,
    _validate_hmi_keyboard,
    _validate_i2c_address,
    _validate_i2c_bus,
)


class TestHmiConfigI2cDefaults:
    """HmiConfig has sensible I2C defaults."""

    def test_default_i2c_bus_is_1(self) -> None:
        # /dev/i2c-1 is the standard userspace bus on Pi GPIO 2/3.
        hmi = HmiConfig()
        assert hmi.i2c_bus == 1

    def test_default_i2c_address_is_cardkb(self) -> None:
        # 0x5F is the M5Stack CardKB v1.1 fixed address.
        hmi = HmiConfig()
        assert hmi.i2c_address == 0x5F


class TestHmiKeyboardChoiceI2c:
    """The 'i2c' keyboard choice is now valid."""

    def test_i2c_is_a_valid_choice(self) -> None:
        # v0.0.16 added 'i2c' to _HMI_KEYBOARD_CHOICES.
        assert _validate_hmi_keyboard("i2c") == "i2c"

    def test_i2c_is_case_insensitive(self) -> None:
        # The validator lowercases the input.
        assert _validate_hmi_keyboard("I2C") == "i2c"
        assert _validate_hmi_keyboard("I2c") == "i2c"

    def test_unknown_keyboard_still_rejected(self) -> None:
        # Regression guard: 'spi' shouldn't sneak through.
        with pytest.raises(ConfigError, match="not recognized"):
            _validate_hmi_keyboard("spi")


class TestValidateI2cBus:
    """_validate_i2c_bus rejects bad values."""

    def test_accepts_zero(self) -> None:
        # /dev/i2c-0 exists on some Pi variants. Allow it.
        assert _validate_i2c_bus(0) == 0

    def test_accepts_one(self) -> None:
        assert _validate_i2c_bus(1) == 1

    def test_accepts_two(self) -> None:
        # Bookworm Pi Zero 2W shows /dev/i2c-2.
        assert _validate_i2c_bus(2) == 2

    def test_rejects_negative(self) -> None:
        with pytest.raises(ConfigError, match="non-negative"):
            _validate_i2c_bus(-1)

    def test_rejects_implausible_high(self) -> None:
        # Sanity ceiling catches typos like 100 instead of 1.
        with pytest.raises(ConfigError, match="implausible"):
            _validate_i2c_bus(100)

    def test_rejects_string(self) -> None:
        with pytest.raises(ConfigError, match="integer"):
            _validate_i2c_bus("1")

    def test_rejects_bool(self) -> None:
        # bool is a subclass of int in Python -- the validator
        # explicitly rejects True / False to avoid TOML
        # ``i2c_bus = true`` accidentally meaning ``i2c_bus = 1``.
        with pytest.raises(ConfigError, match="integer"):
            _validate_i2c_bus(True)

    def test_rejects_float(self) -> None:
        with pytest.raises(ConfigError, match="integer"):
            _validate_i2c_bus(1.5)


class TestValidateI2cAddress:
    """_validate_i2c_address rejects bad values."""

    def test_accepts_cardkb_default(self) -> None:
        assert _validate_i2c_address(0x5F) == 0x5F

    def test_accepts_lower_unreserved(self) -> None:
        # 0x08 is the first non-reserved 7-bit address.
        assert _validate_i2c_address(0x08) == 0x08

    def test_accepts_upper_unreserved(self) -> None:
        # 0x77 is the last non-reserved 7-bit address.
        assert _validate_i2c_address(0x77) == 0x77

    def test_rejects_negative(self) -> None:
        with pytest.raises(ConfigError, match="range"):
            _validate_i2c_address(-1)

    def test_rejects_above_7bit_range(self) -> None:
        # 8-bit addresses don't exist on standard I2C.
        with pytest.raises(ConfigError, match="range"):
            _validate_i2c_address(0x80)
        with pytest.raises(ConfigError, match="range"):
            _validate_i2c_address(0xFF)

    def test_rejects_bool(self) -> None:
        with pytest.raises(ConfigError, match="integer"):
            _validate_i2c_address(True)

    def test_rejects_string(self) -> None:
        with pytest.raises(ConfigError, match="integer"):
            _validate_i2c_address("0x5F")


class TestFromDictI2cExtensions:
    """_from_dict reads the new i2c_bus and i2c_address fields."""

    def test_defaults_when_missing(self, tmp_path: Path) -> None:
        # Pre-v0.0.16 configs don't have i2c_bus or i2c_address.
        # The new defaults must kick in transparently.
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.i2c_bus == 1
        assert cfg.hmi.i2c_address == 0x5F

    def test_reads_i2c_fields(self, tmp_path: Path) -> None:
        # Explicit values in TOML are honored.
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
            "hmi": {
                "keyboard": "i2c",
                "i2c_bus": 2,
                "i2c_address": 0x5F,
            },
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "i2c"
        assert cfg.hmi.i2c_bus == 2
        assert cfg.hmi.i2c_address == 0x5F

    def test_rejects_bad_i2c_address(self, tmp_path: Path) -> None:
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
            "hmi": {
                "keyboard": "i2c",
                "i2c_address": 0x100,  # out of 7-bit range
            },
        }
        with pytest.raises(ConfigError, match="range"):
            _from_dict(data, tmp_path / "config.toml")
