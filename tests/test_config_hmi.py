"""Tests for the [hmi] configuration section (v0.0.12).

Three test categories:
  1. Defaults — fresh Config has keyboard="usb" / serial0 / 115200
  2. Parser — _from_dict accepts valid [hmi] tables, rejects bad ones
  3. save_atomic — preserves [hmi] across station-only edits

These tests are file-system free (use _from_dict directly with
parsed dicts) where possible. The save_atomic tests use a tmp_path
fixture and monkey-patch paths.config_path so they don't touch the
real /var/lib/microjs8.
"""

from __future__ import annotations

import pytest

from microjs8 import config
from microjs8.config import (
    Config,
    ConfigError,
    HmiConfig,
    StationConfig,
    _from_dict,
    _validate_hmi_keyboard,
    _validate_uart_baud,
    _validate_uart_device,
)


# ── HmiConfig dataclass defaults ─────────────────────────────────────


class TestHmiConfigDefaults:
    def test_default_keyboard_is_usb(self) -> None:
        hmi = HmiConfig()
        assert hmi.keyboard == "usb"

    def test_default_uart_device(self) -> None:
        hmi = HmiConfig()
        assert hmi.uart_device == "/dev/serial0"

    def test_default_uart_baud(self) -> None:
        hmi = HmiConfig()
        assert hmi.uart_baud == 115200

    def test_config_default_has_hmi_field(self) -> None:
        cfg = Config()
        assert cfg.hmi == HmiConfig()

    def test_dataclass_is_frozen(self) -> None:
        # HmiConfig must be immutable so it can be safely shared
        # across threads and used as a hashable in equality checks.
        hmi = HmiConfig()
        with pytest.raises(Exception):
            hmi.keyboard = "uart"  # type: ignore[misc]


# ── Field validators ─────────────────────────────────────────────────


class TestValidateHmiKeyboard:
    def test_usb_accepted(self) -> None:
        assert _validate_hmi_keyboard("usb") == "usb"

    def test_uart_accepted(self) -> None:
        assert _validate_hmi_keyboard("uart") == "uart"

    def test_case_insensitive(self) -> None:
        # Operators editing by hand might write "USB" or "Uart"; the
        # validator normalizes to lowercase.
        assert _validate_hmi_keyboard("USB") == "usb"
        assert _validate_hmi_keyboard("Uart") == "uart"

    def test_whitespace_stripped(self) -> None:
        assert _validate_hmi_keyboard("  uart  ") == "uart"

    def test_unknown_backend_rejected(self) -> None:
        with pytest.raises(ConfigError, match="not recognized"):
            _validate_hmi_keyboard("bluetooth")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _validate_hmi_keyboard("")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_hmi_keyboard(42)
        with pytest.raises(ConfigError):
            _validate_hmi_keyboard(None)
        with pytest.raises(ConfigError):
            _validate_hmi_keyboard(["uart"])


class TestValidateUartDevice:
    def test_default_path_accepted(self) -> None:
        assert _validate_uart_device("/dev/serial0") == "/dev/serial0"

    def test_alternative_paths_accepted(self) -> None:
        # We don't lock the operator into /dev/serial0; an alternative
        # USB-serial dongle path is valid too.
        assert _validate_uart_device("/dev/ttyAMA0") == "/dev/ttyAMA0"
        assert _validate_uart_device("/dev/ttyUSB0") == "/dev/ttyUSB0"

    def test_relative_path_rejected(self) -> None:
        with pytest.raises(ConfigError, match="absolute path"):
            _validate_uart_device("serial0")

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _validate_uart_device("")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_uart_device(0)

    def test_device_not_required_to_exist(self) -> None:
        # We don't stat() the device — at config-load time, the UART
        # hardware might not be enabled yet. The keyboard thread
        # handles missing devices gracefully.
        assert _validate_uart_device("/dev/does-not-exist") == "/dev/does-not-exist"


class TestValidateUartBaud:
    @pytest.mark.parametrize(
        "rate", [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
    )
    def test_common_rates_accepted(self, rate: int) -> None:
        assert _validate_uart_baud(rate) == rate

    def test_uncommon_rate_passes_with_warning(self, caplog) -> None:
        # 100000 isn't a standard rate but a custom radio might use it.
        # We accept but log a warning so typos surface.
        result = _validate_uart_baud(100000)
        assert result == 100000
        assert any("unusual" in r.message for r in caplog.records)

    def test_zero_rejected(self) -> None:
        with pytest.raises(ConfigError, match="positive"):
            _validate_uart_baud(0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _validate_uart_baud(-115200)

    def test_non_int_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be an integer"):
            _validate_uart_baud("115200")
        with pytest.raises(ConfigError):
            _validate_uart_baud(115200.0)

    def test_bool_rejected(self) -> None:
        # In Python, bool is a subclass of int. Sneaking True/False
        # through as a baud rate would be confusing; the validator
        # explicitly rejects bool to surface the operator's mistake.
        with pytest.raises(ConfigError, match="must be an integer"):
            _validate_uart_baud(True)
        with pytest.raises(ConfigError):
            _validate_uart_baud(False)


# ── _from_dict parsing of [hmi] section ──────────────────────────────


def _minimal_dict_with_hmi(hmi: dict) -> dict:
    """Build a minimal config dict for _from_dict, with a custom [hmi]."""
    return {
        "station": {"callsign": "K1ABC", "grid": "FN42"},
        "units_distance": "miles",
        "radio": {"id": "qdx"},
        "hmi": hmi,
    }


class TestFromDictHmiSection:
    def test_missing_section_uses_defaults(self, tmp_path) -> None:
        # Pre-v0.0.12 config files don't have [hmi]; defaults must
        # produce the USB-keyboard behavior.
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
        }
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi == HmiConfig()
        assert cfg.hmi.keyboard == "usb"

    def test_full_uart_config(self, tmp_path) -> None:
        data = _minimal_dict_with_hmi({
            "keyboard": "uart",
            "uart_device": "/dev/ttyAMA0",
            "uart_baud": 230400,
        })
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "uart"
        assert cfg.hmi.uart_device == "/dev/ttyAMA0"
        assert cfg.hmi.uart_baud == 230400

    def test_partial_uart_config_fills_defaults(self, tmp_path) -> None:
        # Operator only sets keyboard="uart"; uart_device/baud should
        # default to /dev/serial0 and 115200.
        data = _minimal_dict_with_hmi({"keyboard": "uart"})
        cfg = _from_dict(data, tmp_path / "config.toml")
        assert cfg.hmi.keyboard == "uart"
        assert cfg.hmi.uart_device == "/dev/serial0"
        assert cfg.hmi.uart_baud == 115200

    def test_bad_keyboard_value_raises(self, tmp_path) -> None:
        data = _minimal_dict_with_hmi({"keyboard": "bluetooth"})
        with pytest.raises(ConfigError, match="not recognized"):
            _from_dict(data, tmp_path / "config.toml")

    def test_section_not_table_raises(self, tmp_path) -> None:
        # [hmi] = "uart"  ← wrong, should be a TOML table not a string
        data = {
            "station": {"callsign": "K1ABC", "grid": "FN42"},
            "units_distance": "miles",
            "radio": {"id": "qdx"},
            "hmi": "uart",
        }
        with pytest.raises(ConfigError, match="must be a table"):
            _from_dict(data, tmp_path / "config.toml")


# ── save_atomic preserves [hmi] across station-only edits ────────────


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect paths.config_path so save_atomic doesn't touch real fs."""
    config_path = tmp_path / "config.toml"
    default_path = tmp_path / "config.toml.default"

    monkeypatch.setattr(config.paths, "config_path", lambda: config_path)
    monkeypatch.setattr(
        config.paths, "default_config_path", lambda: default_path
    )
    monkeypatch.setattr(config.paths, "ensure_writable_dirs", lambda: None)

    # Seed a default config so first-boot logic works.
    default_path.write_text(
        '# default config\n'
        'units_distance = "miles"\n'
        '\n'
        '[station]\n'
        'callsign = "N0CALL"\n'
        'grid = ""\n'
        '\n'
        '[radio]\n'
        'id = "qdx"\n'
    )
    return config_path


class TestSaveAtomicPreservesHmi:
    def test_explicit_hmi_written(self, isolated_config) -> None:
        # Operator switches to UART mode via a hypothetical Setup-UI
        # call that passes the hmi kwarg explicitly.
        hmi = HmiConfig(
            keyboard="uart", uart_device="/dev/serial0", uart_baud=115200,
        )
        cfg = config.save_atomic(
            callsign="K1ABC", grid="FN42", units="miles",
            radio_id="qdx", groups=(), hmi=hmi,
        )
        assert cfg.hmi == hmi

    def test_hmi_none_preserves_previous(self, isolated_config) -> None:
        # First write: set UART mode.
        config.save_atomic(
            callsign="K1ABC", grid="FN42", units="miles",
            radio_id="qdx", groups=(),
            hmi=HmiConfig(keyboard="uart"),
        )

        # Second write: legacy station-only edit, hmi=None means
        # "preserve current". The UART setting must survive.
        cfg = config.save_atomic(
            callsign="K1ABC", grid="EN83", units="miles",
        )
        assert cfg.hmi.keyboard == "uart"

    def test_default_hmi_not_emitted(self, isolated_config) -> None:
        # Cleanliness: if hmi is at defaults, config.toml shouldn't
        # have a noisy [hmi] block.
        config.save_atomic(
            callsign="K1ABC", grid="FN42", units="miles",
        )
        body = isolated_config.read_text()
        assert "[hmi]" not in body
        assert "keyboard" not in body  # only appears under [hmi]

    def test_non_default_hmi_emitted(self, isolated_config) -> None:
        config.save_atomic(
            callsign="K1ABC", grid="FN42", units="miles",
            hmi=HmiConfig(keyboard="uart"),
        )
        body = isolated_config.read_text()
        assert "[hmi]" in body
        assert 'keyboard = "uart"' in body
