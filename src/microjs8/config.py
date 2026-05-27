"""Configuration loading and validation for MiniJS8.

Configuration lives in TOML at ``paths.config_path()`` (default
``/var/lib/microjs8/config.toml``). On first boot, if no live config exists,
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

v0.0.12: ``[hmi]`` section added so the operator can pick between the
USB-keyboard backend (default; reads /dev/input/by-id/*-event-kbd) and
the UART-keyboard backend (Cardputer ADV via /dev/serial0).
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

# JS8Call group callsign envelope (per JS8Call Guide v2.2 p.10):
# starts with '@', up to 8 alphanumeric chars with optional slashes
# (e.g. '@EMCOMM', '@ARESGA', '@DX/NA', '@REGION/1', '@GROUP/0').
# We're slightly stricter than the published regex
# ``[@][A-Z0-9/]{0,3}[/]?[A-Z0-9/]{0,3}[/]?[A-Z0-9/]{0,3}`` — we just
# require 1-8 chars after the '@' so the validation is intuitive
# to operators ("8 chars after the at-sign, letters digits and
# slashes"). All in-the-wild groups we've observed comply.
_GROUP_RE = re.compile(r"^@[A-Z0-9/]{1,8}$")

# Groups that every JS8Call station is implicitly in. Listing these
# explicitly in config is redundant and confuses the address-set
# logic (we'd double-count), so we reject them at validation.
_IMPLICIT_GROUPS: frozenset[str] = frozenset({"@ALLCALL", "@HB"})

# Max number of operator-configured custom groups. JS8Call has no
# documented limit but more than a handful gets unwieldy: every
# message to any of these groups counts as "for us", every
# group SNR? query triggers an auto-respond burst, and the Setup
# field becomes a wall of comma-separated text. Cap at 4 per the
# W5DMH spec discussion (May 2026).
MAX_GROUPS = 4

# Maidenhead 4 or 6 character grid. 4-char: FF99. 6-char: FF99ll.
_GRID4_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")
_GRID6_RE = re.compile(r"^[A-R]{2}[0-9]{2}[a-x]{2}$")

# Sentinel callsign. Matches JS8Call's default so an unconfigured station
# is recognizable on the air (though we won't TX with it set).
UNCONFIGURED_CALLSIGN = "N0CALL"

# HMI keyboard backends. Three choices:
#
#   - "auto"  (default, v0.0.13+) — start BOTH USB and UART backends if
#     either device is present. Each backend handles its own absence
#     gracefully (USB: 2 s reconnect loop; UART: thread exits cleanly
#     when /dev/serial0 isn't openable). Operator can plug in either
#     keyboard, and events from either flow through the same router.
#     This is the plug-and-play default.
#
#   - "usb" — explicit single-backend: USB / TCA8418 only, UART skipped
#     entirely. Useful when /dev/serial0 isn't reserved for keyboard
#     input (e.g. operator wants the UART for a different peripheral
#     like a GPS module on a non-default port).
#
#   - "uart" — explicit single-backend: UART only, USB skipped. Useful
#     when running the Cardputer ADV sandwich and you specifically
#     don't want a USB keyboard to override input.
#
# Pre-v0.0.13 configs default to "usb" if absent. v0.0.13+ shipped
# configs and the dataclass default both flip to "auto".
_HMI_KEYBOARD_CHOICES: frozenset[str] = frozenset({"auto", "usb", "uart"})

# Common UART baud rates. We don't reject uncommon rates outright
# (someone might want a custom rate for a specific keyboard) but we
# log a warning to surface typos like 11520 vs 115200.
_COMMON_BAUD_RATES: frozenset[int] = frozenset(
    {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}
)


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
    # Configured JS8Call group memberships, e.g. ``("@EMCOMM", "@ARES")``.
    # Each entry is uppercase, prefixed with '@', and matches the
    # GROUP_RE format. We use a frozen tuple so this dataclass stays
    # hashable for the existing equality-based dirty-checking in
    # UIState. ``@ALLCALL`` and ``@HB`` are NEVER stored here —
    # every station is implicitly in them and we deduplicate at
    # load time to avoid double-counting in the address set.
    groups: tuple[str, ...] = ()

    @property
    def is_configured(self) -> bool:
        """True if both callsign and grid have been set to real values."""
        return (
            self.callsign != UNCONFIGURED_CALLSIGN
            and self.callsign != ""
            and self.grid != ""
        )


@dataclass(frozen=True)
class HmiConfig:
    """HMI (human-machine interface) backend configuration.

    Selects which keyboard backend(s) the daemon uses. Three choices
    via the ``keyboard`` field — see ``_HMI_KEYBOARD_CHOICES`` for
    canonical docs.

    ``"auto"`` (default since v0.0.13): the daemon attempts BOTH USB
    and UART backends at startup. Each gracefully handles its own
    absent device, so the rig is plug-and-play: connect a USB keyboard
    OR a Cardputer ADV via UART (or both), and whichever is present
    drives the UI. Events flow through the same router; identical
    ``KeyEvent`` shape from either source.

    ``"usb"``: explicit single-backend — discover only USB / TCA8418
    keyboards. The ``uart_*`` fields are ignored.

    ``"uart"``: explicit single-backend — open only the UART. Used
    when paired with an M5Stack Cardputer ADV running the
    microjs8-cardputer-link firmware via its EXT 14-pin header. The
    USB discovery path is skipped entirely. See
    ``docs/CARDPUTER_LINK.md`` for the hardware setup.
    """

    keyboard: str = "auto"
    uart_device: str = "/dev/serial0"
    uart_baud: int = 115200


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

    # v0.0.12: HMI backend selection (USB vs UART keyboard).
    hmi: HmiConfig = field(default_factory=HmiConfig)

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


def _validate_groups(value: Any) -> tuple[str, ...]:
    """Normalize and validate a list of JS8Call group callsigns.

    Accepts:
      - A TOML array: ``groups = ["@EMCOMM", "@ARES"]``
      - A single comma-separated string: ``groups = "@EMCOMM, @ARES"``
        (the Setup UI writes this format; we accept both to keep
        hand-edited config.toml from being surprising)

    Each entry is uppercased, stripped of surrounding whitespace,
    and matched against ``_GROUP_RE``. Implicit groups (@ALLCALL,
    @HB) are silently dropped — they're never explicit config.
    Duplicates are removed preserving first-seen order. Returns a
    tuple capped at ``MAX_GROUPS``; raises ConfigError if more
    entries appear after deduplication or any entry is malformed.
    """
    if value is None or value == "":
        return ()

    # Accept either an array or a comma-separated string.
    if isinstance(value, str):
        candidates = [s for s in (p.strip() for p in value.split(",")) if s]
    elif isinstance(value, (list, tuple)):
        candidates = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigError(
                    f"groups entries must be strings, got "
                    f"{type(item).__name__} in {value!r}"
                )
            stripped = item.strip()
            if stripped:
                candidates.append(stripped)
    else:
        raise ConfigError(
            f"groups must be an array or comma-separated string, "
            f"got {type(value).__name__}"
        )

    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        normalized = raw.upper()
        if normalized in _IMPLICIT_GROUPS:
            # Silently drop — every station is in these by default.
            continue
        if not _GROUP_RE.match(normalized):
            raise ConfigError(
                f"group {raw!r} is not a valid format "
                f"(expected '@NAME' with 1-8 letters/digits/slashes, "
                f"e.g. '@EMCOMM', '@DX/NA')"
            )
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)

    if len(out) > MAX_GROUPS:
        raise ConfigError(
            f"too many groups configured: {len(out)} > {MAX_GROUPS} max "
            f"(got {out!r})"
        )
    return tuple(out)


def _validate_hmi_keyboard(value: Any) -> str:
    """Validate the [hmi] keyboard backend name."""
    if not isinstance(value, str):
        raise ConfigError(
            f"[hmi] keyboard must be a string, got {type(value).__name__}"
        )
    normalized = value.strip().lower()
    if normalized not in _HMI_KEYBOARD_CHOICES:
        raise ConfigError(
            f"[hmi] keyboard {value!r} not recognized; "
            f"valid choices: {sorted(_HMI_KEYBOARD_CHOICES)}"
        )
    return normalized


def _validate_uart_device(value: Any) -> str:
    """Validate the [hmi] uart_device path.

    Must be an absolute path. We don't stat() the file — the device
    may not exist at config-load time (e.g. UART hardware not yet
    enabled in /boot/firmware/config.txt), and the keyboard thread
    handles missing devices gracefully at start.
    """
    if not isinstance(value, str):
        raise ConfigError(
            f"[hmi] uart_device must be a string path, "
            f"got {type(value).__name__}"
        )
    if not value:
        raise ConfigError("[hmi] uart_device must not be empty")
    if not value.startswith("/"):
        raise ConfigError(
            f"[hmi] uart_device must be an absolute path, got {value!r}"
        )
    return value


def _validate_uart_baud(value: Any) -> int:
    """Validate the [hmi] uart_baud rate.

    Common rates are listed in _COMMON_BAUD_RATES; uncommon values
    pass but log a warning so typos like 11520 (missing zero) are
    surfaced without forcing a hard reject.
    """
    # Reject bool first since bool is a subclass of int in Python and
    # we don't want True/False sneaking through.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"[hmi] uart_baud must be an integer, got {type(value).__name__}"
        )
    if value <= 0:
        raise ConfigError(f"[hmi] uart_baud must be positive, got {value}")
    if value not in _COMMON_BAUD_RATES:
        _log.warning(
            "[hmi] uart_baud=%d is unusual; common rates are %s",
            value, sorted(_COMMON_BAUD_RATES),
        )
    return value


def _from_dict(data: dict[str, Any], source_path: Path) -> Config:
    """Build a Config from a parsed TOML dict, validating each field."""
    station_data = data.get("station", {})
    if not isinstance(station_data, dict):
        raise ConfigError("[station] section must be a table")

    station = StationConfig(
        callsign=_validate_callsign(station_data.get("callsign", UNCONFIGURED_CALLSIGN)),
        grid=_validate_grid(station_data.get("grid", "")),
        groups=_validate_groups(station_data.get("groups", ())),
    )

    units = _validate_units(data.get("units_distance", "miles"))

    # [radio] section (Step 6). Defaults to "qdx". Validates against
    # the embedded radios.py registry so a typo'd radio_id fails at
    # config load, not at the moment we try to start CAT.
    radio_data = data.get("radio", {})
    if not isinstance(radio_data, dict):
        raise ConfigError("[radio] section must be a table")
    radio_id = _validate_radio_id(radio_data.get("id", "qdx"))

    # [hmi] section (v0.0.12). Defaults: keyboard="auto" (since
    # v0.0.13), uart_device="/dev/serial0", uart_baud=115200. The
    # defaults are plug-and-play: both USB and UART backends are
    # attempted, whichever has a device drives the UI. Operators
    # who want strict single-backend behavior can set "usb" or "uart"
    # explicitly. Pre-v0.0.13 configs that hardcoded "usb" or "uart"
    # continue to behave identically — only the absent-section
    # default changed.
    hmi_data = data.get("hmi", {})
    if not isinstance(hmi_data, dict):
        raise ConfigError("[hmi] section must be a table")
    hmi = HmiConfig(
        keyboard=_validate_hmi_keyboard(hmi_data.get("keyboard", "auto")),
        uart_device=_validate_uart_device(hmi_data.get("uart_device", "/dev/serial0")),
        uart_baud=_validate_uart_baud(hmi_data.get("uart_baud", 115200)),
    )

    return Config(
        station=station,
        units_distance=units,
        radio_id=radio_id,
        hmi=hmi,
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
            "config loaded: callsign=%s grid=%s units=%s hmi.keyboard=%s",
            config.station.callsign,
            config.station.grid,
            config.units_distance,
            config.hmi.keyboard,
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
    groups: Optional[tuple[str, ...]] = None,
    hmi: Optional[HmiConfig] = None,
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
    groups : optional tuple of str
        Configured JS8Call group memberships. When None (default),
        reads the current live config to preserve whatever groups
        are already set. Same preservation rationale as radio_id —
        a single-field edit shouldn't blow away unrelated fields.
    hmi : optional HmiConfig
        HMI backend configuration. When None (default), reads the
        current live config to preserve whatever was last written.
        Same preservation rationale as radio_id and groups — the
        Setup UI doesn't expose HMI mode editing today, so its
        save_atomic calls leave hmi at None and preserve whatever
        the operator hand-edited in /etc/microjs8/config.toml.

    The write target is ``paths.config_path()``, NOT the
    ``shipped_default_path()`` — defaults are read-only on the image.
    """
    # Validate first; throw cleanly before touching the filesystem.
    callsign = _validate_callsign(callsign)
    grid = _validate_grid(grid)
    units = _validate_units(units)

    # If any non-station field wasn't explicitly supplied, preserve
    # whatever the current live config has (or fall back to defaults).
    # Avoids the trap of "edit callsign → next restart, [radio] or
    # [hmi] is gone".
    if radio_id is None or groups is None or hmi is None:
        try:
            current = load()
            if radio_id is None:
                radio_id = current.radio_id
            if groups is None:
                groups = current.station.groups
            if hmi is None:
                hmi = current.hmi
        except ConfigError:
            if radio_id is None:
                radio_id = "qdx"  # safe fallback — same as Config dataclass default
            if groups is None:
                groups = ()
            if hmi is None:
                hmi = HmiConfig()
    radio_id = _validate_radio_id(radio_id)
    groups = _validate_groups(groups)
    # Re-validate hmi fields in case the caller constructed an
    # HmiConfig with bypassed validators (e.g. via dataclasses.replace).
    hmi = HmiConfig(
        keyboard=_validate_hmi_keyboard(hmi.keyboard),
        uart_device=_validate_uart_device(hmi.uart_device),
        uart_baud=_validate_uart_baud(hmi.uart_baud),
    )

    live = paths.config_path()
    paths.ensure_writable_dirs()

    # Serialize groups as a TOML array — the canonical form, easier
    # to round-trip than a comma-separated string. If empty, omit
    # the line entirely so config.toml stays clean.
    if groups:
        groups_toml = "groups = [" + ", ".join(f'"{g}"' for g in groups) + "]\n"
    else:
        groups_toml = ""

    # Only emit [hmi] if it differs from defaults — keeps config.toml
    # clean for operators on the USB-keyboard path who never touch
    # the UART backend. We still validate non-default writes above.
    if hmi == HmiConfig():
        hmi_toml = ""
    else:
        hmi_toml = (
            "\n"
            "[hmi]\n"
            f'keyboard = "{hmi.keyboard}"\n'
            f'uart_device = "{hmi.uart_device}"\n'
            f"uart_baud = {hmi.uart_baud}\n"
        )

    body = (
        "# MiniJS8 — live configuration (written by setup wizard)\n"
        f'units_distance = "{units}"\n'
        "\n"
        "[station]\n"
        f'callsign = "{callsign}"\n'
        f'grid = "{grid}"\n'
        f"{groups_toml}"
        "\n"
        "[radio]\n"
        f'id = "{radio_id}"\n'
        f"{hmi_toml}"
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
        "config saved: callsign=%s grid=%s units=%s radio=%s groups=%s hmi.keyboard=%s",
        callsign, grid, units, radio_id, list(groups), hmi.keyboard,
    )
    # Re-load to return a parsed Config — also catches the (unlikely)
    # case where what we just wrote doesn't round-trip cleanly.
    return load()
