"""Configuration loading and validation for MicroJS8.

Configuration lives in TOML at ``paths.config_path()`` (default
``/var/microjs8/config.toml``). On first boot, if no live config exists,
the shipped defaults at ``paths.default_config_path()`` are copied in.

Callsign and grid validation follows amateur-radio convention:

  - **Callsign**: uppercase letters and digits, 3-10 characters. Matches
    the practical envelope of FCC/ITU callsigns (e.g. "K1ABC", "VE3XYZ",
    "JA1AAA/P"). The sentinel "N0CALL" is treated as *unconfigured* —
    parses cleanly so the daemon can boot, but ``tx_allowed`` is False.

  - **Grid**: Maidenhead locator, either 4-character ("FN42") or
    6-character ("FN42aa"). Empty string means unset; ``tx_allowed`` is
    False until at least a 4-character grid is supplied.

Any other validation failure raises ``ConfigError``. The daemon catches
this at startup and refuses to enter the run loop — better to fail loudly
than to operate from a malformed config.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from microjs8 import paths

_log = logging.getLogger(__name__)

# Practical callsign envelope. Deliberately loose: this is not a license
# database; we only need to reject obvious garbage.
_CALLSIGN_RE = re.compile(r"^[A-Z0-9/]{3,10}$")

# Maidenhead 4 or 6 character grid. 4-char: FF99. 6-char: FF99ll.
_GRID4_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")
_GRID6_RE = re.compile(r"^[A-R]{2}[0-9]{2}[a-x]{2}$")

# Sentinel callsign. Matches JS8Call's default so an unconfigured station
# is recognizable on the air (though we won't TX with it set).
UNCONFIGURED_CALLSIGN = "N0CALL"


class ConfigError(Exception):
    """Raised when the loaded configuration is invalid."""


@dataclass(frozen=True)
class StationConfig:
    """Operator identity. Required for any TX activity.

    A freshly-flashed image ships with ``callsign = "N0CALL"`` and
    ``grid = ""``; the first-boot setup wizard (Step 2+) walks the
    operator through filling these in.
    """

    callsign: str = UNCONFIGURED_CALLSIGN
    grid: str = ""

    @property
    def is_configured(self) -> bool:
        """True if both callsign and grid have been set to real values."""
        return (
            self.callsign != UNCONFIGURED_CALLSIGN
            and self.callsign != ""
            and self.grid != ""
        )


@dataclass(frozen=True)
class Config:
    """Top-level configuration.

    Only the fields needed in Step 1 are populated. Subsequent steps add
    sections (``[modem]``, ``[ui]``, ``[gps]``, etc.) without breaking
    Step 1's schema — TOML's tolerance for unknown sections is exactly
    what we want here.
    """

    station: StationConfig = field(default_factory=StationConfig)
    units_distance: str = "miles"  # "miles" | "km" — resolved in v0.7

    # Days to retain decoded frames in the SQLite store. Step 5 default
    # is 30 days per spec. Configurable later if we need it.
    retention_days: int = 30

    # Step 6: radio identity. Selects which entry in the embedded
    # radios.py registry to use for hamlib model + baud rate + PTT
    # method. Default is "qdx" since that's our reference hardware.
    radio_id: str = "qdx"

    # Path the config was loaded from. Useful for log messages and for
    # the eventual first-boot wizard which writes back to this path.
    source_path: Path = field(default_factory=paths.config_path)

    @property
    def tx_allowed(self) -> bool:
        """True if the daemon may key the transmitter.

        Step 1 doesn't TX; this property exists so later steps have a
        single, unambiguous gate to consult before any frame is sent.
        """
        return self.station.is_configured


def _validate_callsign(value: str) -> str:
    """Normalize and validate a callsign string."""
    if not isinstance(value, str):
        raise ConfigError(f"callsign must be a string, got {type(value).__name__}")
    normalized = value.strip().upper()
    if normalized == "":
        return UNCONFIGURED_CALLSIGN
    if not _CALLSIGN_RE.match(normalized):
        raise ConfigError(
            f"callsign {value!r} is not a valid format "
            f"(expected 3-10 chars, A-Z 0-9 /; e.g. 'K1ABC')"
        )
    return normalized


def _validate_grid(value: str) -> str:
    """Normalize and validate a Maidenhead grid string.

    Empty string is allowed and means *unset*. Otherwise must be 4 or 6
    characters with the proper alphanumeric pattern.
    """
    if not isinstance(value, str):
        raise ConfigError(f"grid must be a string, got {type(value).__name__}")
    if value == "":
        return ""
    # Maidenhead is case-sensitive: first pair upper, last pair lower.
    if len(value) == 4:
        normalized = value.upper()
        if not _GRID4_RE.match(normalized):
            raise ConfigError(f"grid {value!r} is not a valid 4-char Maidenhead locator")
        return normalized
    if len(value) == 6:
        normalized = value[:4].upper() + value[4:].lower()
        if not _GRID6_RE.match(normalized):
            raise ConfigError(f"grid {value!r} is not a valid 6-char Maidenhead locator")
        return normalized
    raise ConfigError(
        f"grid {value!r} must be empty, 4 chars, or 6 chars (got {len(value)})"
    )


def _validate_units(value: str) -> str:
    if value not in ("miles", "km"):
        raise ConfigError(f"units_distance must be 'miles' or 'km', got {value!r}")
    return value


def _from_dict(data: dict[str, Any], source_path: Path) -> Config:
    """Build a Config from a parsed TOML dict, validating each field."""
    station_data = data.get("station", {})
    if not isinstance(station_data, dict):
        raise ConfigError("[station] section must be a table")

    station = StationConfig(
        callsign=_validate_callsign(station_data.get("callsign", UNCONFIGURED_CALLSIGN)),
        grid=_validate_grid(station_data.get("grid", "")),
    )

    units = _validate_units(data.get("units_distance", "miles"))

    # [radio] section (Step 6). Defaults to "qdx". Validates against
    # the embedded radios.py registry so a typo'd radio_id fails at
    # config load, not at the moment we try to start CAT.
    radio_data = data.get("radio", {})
    if not isinstance(radio_data, dict):
        raise ConfigError("[radio] section must be a table")
    radio_id = _validate_radio_id(radio_data.get("id", "qdx"))

    return Config(
        station=station,
        units_distance=units,
        radio_id=radio_id,
        source_path=source_path,
    )


def _validate_radio_id(value: Any) -> str:
    """Validate a radio_id against the embedded registry."""
    # Lazy import — avoid pulling in the cat package at module import time
    # in environments that don't need CAT (e.g. docs builds).
    from microjs8.cat.radios import known_radio_ids

    if not isinstance(value, str):
        raise ConfigError(
            f"[radio] id must be a string, got {type(value).__name__}"
        )
    known = known_radio_ids()
    if value not in known:
        raise ConfigError(
            f"[radio] id {value!r} is unknown; valid choices: {known}"
        )
    return value


def _ensure_live_config_exists() -> Path:
    """First-boot handler: copy shipped defaults into place if needed.

    Returns the path of the live config (which is guaranteed to exist
    after this call). Raises ConfigError if neither the live config nor
    the shipped default is present — that would mean a broken image.
    """
    live = paths.config_path()
    if live.exists():
        return live

    default = paths.default_config_path()
    if not default.exists():
        raise ConfigError(
            f"no config found at {live} and no shipped default at {default}; "
            f"image appears to be incomplete"
        )

    paths.ensure_writable_dirs()
    shutil.copy2(default, live)
    _log.info("first boot: copied default config %s -> %s", default, live)
    return live


def load() -> Config:
    """Load and validate the configuration.

    Performs first-boot detection if the live config is missing.
    """
    live_path = _ensure_live_config_exists()
    try:
        with live_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"failed to read {live_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"failed to parse {live_path}: {exc}") from exc

    config = _from_dict(raw, live_path)

    if config.station.is_configured:
        _log.info(
            "config loaded: callsign=%s grid=%s units=%s",
            config.station.callsign,
            config.station.grid,
            config.units_distance,
        )
    else:
        _log.warning(
            "config loaded but station identity is incomplete "
            "(callsign=%s grid=%r); transmit will be disabled until set",
            config.station.callsign,
            config.station.grid,
        )

    return config


def save_atomic(
    callsign: str,
    grid: str,
    units: str,
    radio_id: Optional[str] = None,
) -> Config:
    """Validate and atomically write a new live configuration.

    Atomic semantics: write to ``config.toml.tmp``, fsync, then rename
    on top of ``config.toml``. A power loss during the write leaves
    either the old config or the new one intact, never a half-file.

    Returns the parsed Config on success. Raises ConfigError on
    validation failure (caller catches and surfaces to the operator).

    Parameters
    ----------
    radio_id : optional str
        The radio profile to write into ``[radio]``. When None
        (default), reads the current live config to preserve whatever
        radio_id is already set there. This lets the legacy
        save-station-fields call sites (callsign / grid / units edits
        from the Setup screen) keep working without dropping the
        operator's radio selection on every edit.

    The write target is ``paths.config_path()``, NOT the
    ``shipped_default_path()`` — defaults are read-only on the image.
    """
    # Validate first; throw cleanly before touching the filesystem.
    callsign = _validate_callsign(callsign)
    grid = _validate_grid(grid)
    units = _validate_units(units)

    # If radio_id wasn't explicitly supplied, preserve whatever the
    # current live config has (or fall back to the default). Avoids
    # the trap of "edit callsign → next restart, [radio] is gone".
    if radio_id is None:
        try:
            current = load()
            radio_id = current.radio_id
        except ConfigError:
            radio_id = "qdx"  # safe fallback — same as Config dataclass default
    radio_id = _validate_radio_id(radio_id)

    live = paths.config_path()
    paths.ensure_writable_dirs()

    body = (
        "# MicroJS8 — live configuration (written by setup wizard)\n"
        f'units_distance = "{units}"\n'
        "\n"
        "[station]\n"
        f'callsign = "{callsign}"\n'
        f'grid = "{grid}"\n'
        "\n"
        "[radio]\n"
        f'id = "{radio_id}"\n'
    )

    tmp = live.with_suffix(live.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, live)
    except OSError as exc:
        # Clean up the temp file if it was created.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise ConfigError(f"failed to write {live}: {exc}") from exc

    _log.info(
        "config saved: callsign=%s grid=%s units=%s radio=%s",
        callsign, grid, units, radio_id,
    )
    # Re-load to return a parsed Config — also catches the (unlikely)
    # case where what we just wrote doesn't round-trip cleanly.
    return load()
