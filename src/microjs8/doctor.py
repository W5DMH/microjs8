"""Single-shot hardware + configuration diagnostic for MicroJS8.

Invoked via ``microjs8 --doctor``. Designed to be the first thing an
operator runs after ``dpkg -i microjs8_*.deb`` on a fresh CardputerZero,
and the first thing they re-run when something's gone wrong.

What gets checked
-----------------

Every subsystem the daemon touches at runtime, grouped by the phase
that introduced it (so a Phase 5 report-line maps to a Phase 5
component on disk):

  - Phase 5 framebuffer    : /proc/fb, /sys/class/graphics/fbN/, /dev/fbN
  - Phase 3 keyboard       : /dev/input/by-path/...
  - Phase 6 battery        : /sys/class/power_supply/bq27*/
  - Phase 3 backlight      : /sys/class/backlight/backlight/
  - Audio                  : sounddevice / PortAudio device enumeration
  - Time source            : chrony reachability (delegates to TxSafetyGate)
  - User identity / groups : os.getgroups vs the systemd unit's expected set
  - Configuration          : config.load() + station.is_configured

Architecture
------------

Each subsystem has a ``check_<name>`` function that returns a list of
``Check`` records. Functions are pure: they take an injectable root
path or callable for their dependencies, so unit tests can pass
``tmp_path`` instead of touching real ``/sys``, ``/proc``, ``/dev``.

The renderer prints a coloured (or plain-ASCII when stdout isn't a
TTY) report grouped by subsystem. ``run_diagnostic_report`` returns
an integer exit code suitable for shell scripting:

  0 — all checks passed or are not-applicable on this host
  1 — at least one FAIL: the subsystem will not work on this host
  2 — at least one WARN but no FAIL: degraded but operational

Not 1 unless something a CardputerZero owner needs to fix.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Final, Optional

_log = logging.getLogger(__name__)


# ── Status enum and Check dataclass ──────────────────────────────────


# We use string constants rather than an enum to keep the ``status``
# field cleanly serialisable (json, yaml) for any future tooling that
# wants to consume doctor output programmatically.
STATUS_OK: Final = "ok"
STATUS_WARN: Final = "warn"
STATUS_FAIL: Final = "fail"
STATUS_INFO: Final = "info"


@dataclasses.dataclass(frozen=True)
class Check:
    """One diagnostic line in the doctor's report.

    Attributes:
        subsystem  The phase / component group ("display", "battery",
                   "keyboard", "audio", etc.). Used to group lines.
        title      Short human-readable summary ("framebuffer found"
                   / "no fb_st7789v in /proc/fb").
        status     One of the STATUS_* constants.
        detail     Extra context, multi-line OK; rendered indented
                   under the title.
        fix_hint   Action the operator can take, or None if the line
                   is informational.
    """

    subsystem: str
    title: str
    status: str
    detail: Optional[str] = None
    fix_hint: Optional[str] = None


# ── Phase 5 framebuffer ──────────────────────────────────────────────


def check_display(
    *,
    proc_fb: Optional[Path] = None,
    sysfs_root: Optional[Path] = None,
    name: str = "fb_st7789v",
) -> list[Check]:
    """Probe the framebuffer the Phase 5 ``DisplayDevice`` will use.

    The defaults (``None``) mean "use the real /proc/fb and
    /sys/class/graphics" — these resolve to the same constants that
    ``ui.display.DisplayDevice.open`` uses, so any divergence between
    the daemon and the doctor would itself surface as an FAIL here.
    """
    # Lazy imports keep the doctor module importable on hosts where
    # numpy / Pillow / etc. aren't present (the apt deps haven't
    # been installed yet, or we're running on a stripped image).
    from microjs8.ui.display import (
        DEFAULT_FB_NAME,
        DEFAULT_PROC_FB,
        DEFAULT_SYSFS_ROOT,
        find_fbdev_index,
        read_fb_info,
    )

    proc_fb_path = proc_fb if proc_fb is not None else DEFAULT_PROC_FB
    sysfs_path = sysfs_root if sysfs_root is not None else DEFAULT_SYSFS_ROOT
    if name == "fb_st7789v":
        name = DEFAULT_FB_NAME      # match daemon's actual default

    checks: list[Check] = []

    # 1. Is /proc/fb readable at all?
    if not proc_fb_path.exists():
        checks.append(Check(
            subsystem="display",
            title=f"{proc_fb_path} not present",
            status=STATUS_WARN,
            detail=(
                "Kernel framebuffer support not enabled, or running on "
                "a Pi 4/5 with KMS-only graphics. Daemon will run "
                "headless on this host."
            ),
            fix_hint=(
                "Expected on the Build Pi. On a CardputerZero this "
                "indicates a missing fb_st7789v device-tree overlay."
            ),
        ))
        return checks

    # 2. Find the right index for our panel.
    idx = find_fbdev_index(name, proc_fb=proc_fb_path)
    if idx is None:
        # Show what /proc/fb DID contain so the operator can see if
        # it's a name mismatch (e.g. the kernel calls it 'fb_panel').
        contents = proc_fb_path.read_text().strip() or "(empty)"
        checks.append(Check(
            subsystem="display",
            title=f"no '{name}' framebuffer in {proc_fb_path}",
            status=STATUS_WARN,
            detail=f"/proc/fb contents:\n{contents}",
            fix_hint=(
                f"Expected on the Build Pi. On a CardputerZero, override "
                f"the expected name by passing name=... to "
                f"DisplayDevice.open() if the kernel uses a different "
                f"identifier."
            ),
        ))
        return checks

    # 3. Read the sysfs attributes for that index.
    try:
        info = read_fb_info(idx, name, sysfs_root=sysfs_path)
    except RuntimeError as exc:
        checks.append(Check(
            subsystem="display",
            title=f"fb{idx} sysfs read failed",
            status=STATUS_FAIL,
            detail=str(exc),
            fix_hint=(
                "Check that /sys/class/graphics/fb<N>/{virtual_size,"
                "stride,bits_per_pixel} are all present and readable."
            ),
        ))
        return checks

    # 4. Verify dimensions match what screens.py is rendering.
    expected_w, expected_h = 320, 170    # Phase 4 panel
    if (info.width, info.height) != (expected_w, expected_h):
        checks.append(Check(
            subsystem="display",
            title=f"fb{idx}: unexpected dimensions {info.width}×{info.height}",
            status=STATUS_FAIL,
            detail=(
                f"Phase 4 retuned the renderer for {expected_w}×{expected_h}; "
                f"a different panel size will produce off-canvas content."
            ),
            fix_hint=(
                "Update theme.SCREEN_W/SCREEN_H to match this panel, "
                "or report an issue if you expected the CardputerZero's "
                "320×170 panel."
            ),
        ))
        return checks

    if info.bpp != 16:
        checks.append(Check(
            subsystem="display",
            title=f"fb{idx}: unsupported bpp={info.bpp}",
            status=STATUS_FAIL,
            detail="DisplayDevice supports only 16bpp (RGB565).",
            fix_hint=(
                "Reconfigure the framebuffer driver for 16bpp via "
                "device-tree overlay parameters."
            ),
        ))
        return checks

    # 5. Can the user actually open /dev/fbN for writing?
    devnode = Path(f"/dev/fb{idx}")
    if not devnode.exists():
        checks.append(Check(
            subsystem="display",
            title=f"{devnode} missing",
            status=STATUS_FAIL,
            detail=(
                f"sysfs reported fb{idx} exists but /dev/fb{idx} does "
                f"not. Probably udev hasn't created the node yet — "
                f"reboot may resolve."
            ),
            fix_hint="reboot, or `sudo udevadm trigger`",
        ))
        return checks

    if not os.access(devnode, os.R_OK | os.W_OK):
        checks.append(Check(
            subsystem="display",
            title=f"{devnode} not read+writable by current user",
            status=STATUS_FAIL,
            detail=(
                f"The daemon runs as user 'microjs8' which needs "
                f"membership in the 'video' group to access {devnode}."
            ),
            fix_hint="ensure 'microjs8' is in the 'video' group (handled by postinst)",
        ))
        return checks

    checks.append(Check(
        subsystem="display",
        title=f"fb{idx}: {info.name} {info.width}×{info.height}@{info.bpp}bpp",
        status=STATUS_OK,
        detail=(
            f"line_length={info.line_length}, frame_bytes={info.frame_bytes}, "
            f"node={devnode}"
        ),
    ))
    return checks


# ── Phase 3 keyboard ─────────────────────────────────────────────────


_DEFAULT_KEYBOARD_PATH = Path(
    "/dev/input/by-path/platform-3f804000.i2c-event"
)


def check_keyboard(*, evdev_path: Path = _DEFAULT_KEYBOARD_PATH) -> list[Check]:
    """Probe the CardputerZero's TCA8418 evdev keyboard."""
    checks: list[Check] = []

    if not evdev_path.exists():
        # On the dev box, this evdev path is meaningless. Mark warn
        # rather than fail, with a clear "expected on dev host"
        # disclaimer.
        checks.append(Check(
            subsystem="keyboard",
            title=f"{evdev_path} not present",
            status=STATUS_WARN,
            detail="No CardputerZero TCA8418 keyboard at the expected path.",
            fix_hint=(
                "Expected on the Build Pi. On a CardputerZero, check "
                "`ls /dev/input/by-path/` and override "
                "MICROJS8_KEYBOARD_PATH if the kernel chose a different "
                "path."
            ),
        ))
        return checks

    if not os.access(evdev_path, os.R_OK):
        checks.append(Check(
            subsystem="keyboard",
            title=f"{evdev_path} not readable by current user",
            status=STATUS_FAIL,
            fix_hint="ensure 'microjs8' is in the 'input' group (handled by postinst)",
        ))
        return checks

    checks.append(Check(
        subsystem="keyboard",
        title=f"{evdev_path}: present and readable",
        status=STATUS_OK,
    ))

    # Phase 3 placeholder warning — the Fn+B and Fn+Q scancodes haven't
    # been confirmed on hardware yet.
    fn_b = os.environ.get("MICROJS8_FN_B_KEYCODE")
    fn_q = os.environ.get("MICROJS8_FN_Q_KEYCODE")
    if not fn_b or not fn_q:
        checks.append(Check(
            subsystem="keyboard",
            title="Fn+B and Fn+Q scancodes use placeholder defaults (87/88)",
            status=STATUS_WARN,
            detail=(
                "Phase 3 left the Fn-key scancodes as placeholders. On "
                "real hardware they almost certainly differ from the "
                "defaults."
            ),
            fix_hint=(
                "Run `sudo evtest %s` (or microjs8-capture-scancodes), "
                "press Fn+B and Fn+Q, then add the real values to "
                "/etc/systemd/system/microjs8.service.d/override.conf:\n"
                "  [Service]\n"
                "  Environment=MICROJS8_FN_B_KEYCODE=<int>\n"
                "  Environment=MICROJS8_FN_Q_KEYCODE=<int>"
            ) % evdev_path,
        ))
    else:
        checks.append(Check(
            subsystem="keyboard",
            title=f"Fn key overrides set: FN_B={fn_b}, FN_Q={fn_q}",
            status=STATUS_OK,
        ))

    return checks


# ── Phase 6 battery ──────────────────────────────────────────────────


def check_battery(
    *,
    base_path: Optional[Path] = None,
    name_fragment: str = "bq27",
) -> list[Check]:
    """Probe the BQ27220 fuel gauge."""
    from microjs8.power.battery import (
        DEFAULT_POWER_SUPPLY_DIR,
        find_battery_dir,
        read_battery_state,
    )

    base = base_path if base_path is not None else DEFAULT_POWER_SUPPLY_DIR
    checks: list[Check] = []

    battery_dir = find_battery_dir(base_path=base, name_fragment=name_fragment)
    if battery_dir is None:
        # Show what IS in /sys/class/power_supply so the operator can
        # see whether a different name pattern would match.
        try:
            others = sorted(p.name for p in base.iterdir() if p.is_dir())
        except (OSError, FileNotFoundError):
            others = []
        detail = (
            f"power_supply contents: {', '.join(others) if others else '(none)'}\n"
            "Daemon will treat battery as 'unknown' (HOME shows '--', "
            "TX gate ignores battery)."
        )
        checks.append(Check(
            subsystem="battery",
            title=f"no '{name_fragment}*' directory in {base}",
            status=STATUS_WARN,
            detail=detail,
            fix_hint=(
                "Expected on the Build Pi. On a CardputerZero, check "
                "`ls /sys/class/power_supply/` and adjust the "
                "name_fragment kwarg of BatteryReader if needed."
            ),
        ))
        return checks

    try:
        state = read_battery_state(battery_dir)
    except RuntimeError as exc:
        checks.append(Check(
            subsystem="battery",
            title=f"{battery_dir.name}: parse failed",
            status=STATUS_FAIL,
            detail=str(exc),
            fix_hint=(
                "Check that capacity, status, voltage_now, current_now, "
                "and temp under the battery directory are all readable."
            ),
        ))
        return checks

    checks.append(Check(
        subsystem="battery",
        title=(
            f"{battery_dir.name}: {state.capacity}% "
            f"{state.status_glyph} ({state.status})"
        ),
        status=STATUS_OK,
        detail=(
            f"voltage={state.voltage_v:.2f}V, "
            f"current={state.current_ma:+.0f}mA, "
            f"temp={state.temperature_c:.1f}°C"
        ),
    ))
    return checks


# ── Phase 3 backlight ────────────────────────────────────────────────


def check_backlight(
    *,
    sysfs_path: Path = Path("/sys/class/backlight/backlight"),
) -> list[Check]:
    checks: list[Check] = []
    if not sysfs_path.is_dir():
        checks.append(Check(
            subsystem="backlight",
            title=f"{sysfs_path} not present",
            status=STATUS_WARN,
            detail="Daemon will run without the Fn+B brightness toggle.",
            fix_hint="Expected on the Build Pi. On a CardputerZero, this directory should exist.",
        ))
        return checks

    bright = sysfs_path / "brightness"
    max_b = sysfs_path / "max_brightness"
    if not bright.exists() or not max_b.exists():
        checks.append(Check(
            subsystem="backlight",
            title=f"{sysfs_path}: brightness/max_brightness missing",
            status=STATUS_FAIL,
            fix_hint="Backlight driver may be misconfigured; check dmesg.",
        ))
        return checks

    if not os.access(bright, os.W_OK):
        checks.append(Check(
            subsystem="backlight",
            title=f"{bright} not writable by current user",
            status=STATUS_FAIL,
            fix_hint=(
                "The daemon needs write access. The systemd unit's "
                "ReadWritePaths=/sys/class/backlight handles this for "
                "the 'microjs8' user."
            ),
        ))
        return checks

    try:
        max_val = int(max_b.read_text().strip())
        cur_val = int(bright.read_text().strip())
    except (OSError, ValueError) as exc:
        checks.append(Check(
            subsystem="backlight",
            title=f"{sysfs_path}: read failed",
            status=STATUS_FAIL,
            detail=str(exc),
        ))
        return checks

    checks.append(Check(
        subsystem="backlight",
        title=f"{sysfs_path.name}: {cur_val}/{max_val}",
        status=STATUS_OK,
    ))
    return checks


# ── Audio ────────────────────────────────────────────────────────────


def check_audio(*, query_devices_fn: Optional[Callable] = None) -> list[Check]:
    """Probe PortAudio device enumeration via sounddevice.

    Reports the count of capture-capable devices since that's what
    the radio audio path needs. We don't try to identify "the QDX"
    or "the DigiRig" by name — too fragile; the daemon's discovery
    code handles that.
    """
    checks: list[Check] = []
    if query_devices_fn is None:
        try:
            import sounddevice  # type: ignore[import-not-found]
            query_devices_fn = sounddevice.query_devices
        except (ImportError, OSError) as exc:
            # OSError covers PortAudio's "no audio backend available"
            # case which import-time on a stripped CI runner hits.
            checks.append(Check(
                subsystem="audio",
                title="sounddevice / PortAudio unavailable",
                status=STATUS_WARN,
                detail=str(exc),
                fix_hint="apt install python3-sounddevice libportaudio2",
            ))
            return checks

    try:
        devices = query_devices_fn()
    except Exception as exc:                                # noqa: BLE001
        checks.append(Check(
            subsystem="audio",
            title="device enumeration failed",
            status=STATUS_WARN,
            detail=str(exc),
        ))
        return checks

    capture_devs = [d for d in devices if d.get("max_input_channels", 0) > 0]
    if not capture_devs:
        checks.append(Check(
            subsystem="audio",
            title="no capture-capable audio device found",
            status=STATUS_WARN,
            detail=(
                "The radio audio path needs a capture device (typically "
                "the radio's USB sound card)."
            ),
            fix_hint="connect the radio and re-run --doctor",
        ))
        return checks

    detail_lines = []
    for d in capture_devs[:8]:    # cap output; very long device lists are noise
        detail_lines.append(
            f"[{d.get('index', '?')}] {d.get('name', '?')}"
        )
    checks.append(Check(
        subsystem="audio",
        title=f"{len(capture_devs)} capture-capable device(s) visible",
        status=STATUS_OK,
        detail="\n".join(detail_lines),
    ))
    return checks


# ── Time source ──────────────────────────────────────────────────────


def check_time_source(*, chrony_ok_fn: Optional[Callable[[], bool]] = None) -> list[Check]:
    """Verify chrony reports a usable time discipline."""
    if chrony_ok_fn is None:
        from microjs8.tx.safety import default_chrony_ok
        chrony_ok_fn = default_chrony_ok

    if chrony_ok_fn():
        return [Check(
            subsystem="time",
            title="chrony reports time-sync OK",
            status=STATUS_OK,
            detail=(
                "Daemon will use chrony as the primary time source for "
                "JS8 slot timing."
            ),
        )]
    return [Check(
        subsystem="time",
        title="chrony not synced (or chronyc unavailable)",
        status=STATUS_WARN,
        detail=(
            "Daemon will fall back to the radio-derived consensus once "
            "≥3 frames are decoded. Until then, TX is gated."
        ),
        fix_hint=(
            "apt install chrony, then `sudo systemctl enable --now chronyd`. "
            "If on a network without NTP access, the consensus fallback "
            "will activate after a few decodes."
        ),
    )]


# ── User identity / supplementary groups ─────────────────────────────


_REQUIRED_GROUPS: Final = ("audio", "dialout", "video", "i2c", "input")


def check_user_groups(*, getgroups_fn: Optional[Callable] = None) -> list[Check]:
    """Verify the running user has every group the systemd unit assumes."""
    import grp
    import pwd

    if getgroups_fn is None:
        getgroups_fn = os.getgroups

    uid = os.getuid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = f"uid={uid}"

    # Resolve group ids → names so the report is readable.
    gids = getgroups_fn()
    group_names: set[str] = set()
    for gid in gids:
        try:
            group_names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass

    missing = [g for g in _REQUIRED_GROUPS if g not in group_names]
    checks: list[Check] = []

    checks.append(Check(
        subsystem="user",
        title=f"running as {username} (uid {uid})",
        status=STATUS_INFO,
        detail=(
            "When deployed via the .deb, the systemd unit runs as user "
            "'microjs8' with groups " + ", ".join(_REQUIRED_GROUPS) + "."
        ),
    ))

    if username != "microjs8":
        # Running as someone else (typical when --doctor is run by the
        # operator interactively). The supplementary-group check still
        # provides useful info but we mark it as info, not fail.
        if missing:
            checks.append(Check(
                subsystem="user",
                title=f"current user lacks groups: {', '.join(missing)}",
                status=STATUS_INFO,
                detail=(
                    "Not a problem — the daemon runs as 'microjs8' which "
                    "is set up by the .deb's postinst."
                ),
            ))
        return checks

    # We ARE the microjs8 user. Now group membership matters.
    if missing:
        checks.append(Check(
            subsystem="user",
            title=f"microjs8 user is NOT in: {', '.join(missing)}",
            status=STATUS_FAIL,
            detail="The daemon will fail at runtime when it tries to access devices in these groups.",
            fix_hint=(
                "Re-run the .deb's postinst (sudo dpkg-reconfigure microjs8) "
                "or manually: sudo adduser microjs8 <group>"
            ),
        ))
    else:
        checks.append(Check(
            subsystem="user",
            title="all required supplementary groups present",
            status=STATUS_OK,
        ))
    return checks


# ── Configuration ────────────────────────────────────────────────────


def check_config(*, config_path: Optional[Path] = None) -> list[Check]:
    """Validate config.toml — parse + station-configured check."""
    from microjs8 import config as config_mod

    checks: list[Check] = []

    # config.load() looks at a hardcoded path. To make this testable
    # we accept an optional override. When provided, we skip the
    # daemon's normal load() and parse directly.
    if config_path is not None:
        # Test path: parse a specific file. We bypass _ensure_live_config_exists
        # by calling the private _from_dict on parsed TOML.
        try:
            import tomllib
            with config_path.open("rb") as fh:
                data = tomllib.load(fh)
            cfg = config_mod._from_dict(data, config_path)
        except (OSError, tomllib.TOMLDecodeError, config_mod.ConfigError) as exc:
            return [Check(
                subsystem="config",
                title=f"{config_path}: parse failed",
                status=STATUS_FAIL,
                detail=str(exc),
                fix_hint=f"Edit {config_path} and re-run --doctor.",
            )]
    else:
        try:
            cfg = config_mod.load()
        except config_mod.ConfigError as exc:
            return [Check(
                subsystem="config",
                title="config.load() failed",
                status=STATUS_FAIL,
                detail=str(exc),
                fix_hint="Check syntax of /etc/microjs8/config.toml.",
            )]

    checks.append(Check(
        subsystem="config",
        title=f"loaded {cfg.source_path}",
        status=STATUS_OK,
    ))

    if cfg.station.callsign == "N0CALL" or not cfg.station.callsign:
        checks.append(Check(
            subsystem="config",
            title=f"callsign is '{cfg.station.callsign}' — TX disabled",
            status=STATUS_WARN,
            fix_hint=(
                "Set your callsign via the Setup screen, OR edit "
                f"{cfg.source_path} and restart microjs8."
            ),
        ))
    else:
        checks.append(Check(
            subsystem="config",
            title=f"callsign: {cfg.station.callsign}",
            status=STATUS_OK,
        ))

    if not cfg.station.grid:
        checks.append(Check(
            subsystem="config",
            title="grid not set — TX disabled",
            status=STATUS_WARN,
            fix_hint="Set your Maidenhead grid (e.g. EN82) via the Setup screen.",
        ))
    else:
        checks.append(Check(
            subsystem="config",
            title=f"grid: {cfg.station.grid}",
            status=STATUS_OK,
        ))

    checks.append(Check(
        subsystem="config",
        title=f"radio_id: {cfg.radio_id}",
        status=STATUS_INFO,
    ))
    return checks


# ── Renderer ─────────────────────────────────────────────────────────


# ANSI colour codes — used only when stdout is a TTY. Plain ASCII
# fallback keeps the output readable when piped to a file or grep.
_GLYPHS_COLOR = {
    STATUS_OK:   "\x1b[32m✓\x1b[0m",
    STATUS_WARN: "\x1b[33m!\x1b[0m",
    STATUS_FAIL: "\x1b[31m✗\x1b[0m",
    STATUS_INFO: "\x1b[34m·\x1b[0m",
}
_GLYPHS_PLAIN = {
    STATUS_OK:   "[ OK ]",
    STATUS_WARN: "[WARN]",
    STATUS_FAIL: "[FAIL]",
    STATUS_INFO: "[INFO]",
}

_SUBSYSTEM_TITLES = {
    "display":   "Display (Phase 5 framebuffer)",
    "keyboard":  "Keyboard (Phase 3 TCA8418)",
    "battery":   "Battery (Phase 6 BQ27220)",
    "backlight": "Backlight (Phase 3 sysfs)",
    "audio":     "Audio (radio capture/playback)",
    "time":      "Time source",
    "user":      "User identity & groups",
    "config":    "Configuration",
}

# Order in which subsystems are printed. Display first because it's
# the most user-visible probe; configuration last because it's the
# one the operator most often needs to act on.
_SUBSYSTEM_ORDER = (
    "display", "keyboard", "battery", "backlight",
    "audio", "time", "user", "config",
)


def render_report(checks: list[Check], *, use_color: Optional[bool] = None) -> str:
    """Format a list of Checks as a human-readable report.

    Args:
      use_color  Force colour output on/off. None auto-detects via
                 ``stdout.isatty()``.
    """
    if use_color is None:
        use_color = sys.stdout.isatty()
    glyphs = _GLYPHS_COLOR if use_color else _GLYPHS_PLAIN

    out: list[str] = []
    title_line = "MicroJS8 — diagnostic report"
    out.append(title_line)
    out.append("=" * len(title_line))
    out.append("")

    by_subsystem: dict[str, list[Check]] = {}
    for c in checks:
        by_subsystem.setdefault(c.subsystem, []).append(c)

    # Print known subsystems in defined order, then any that are
    # leftover (so a future check_xxx with a new subsystem name
    # doesn't get silently dropped).
    seen: set[str] = set()
    for sub in _SUBSYSTEM_ORDER:
        if sub not in by_subsystem:
            continue
        seen.add(sub)
        out.extend(_render_subsystem(sub, by_subsystem[sub], glyphs))
    for sub, items in by_subsystem.items():
        if sub in seen:
            continue
        out.extend(_render_subsystem(sub, items, glyphs))

    # Summary line
    counts = {s: 0 for s in (STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_INFO)}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    out.append("")
    out.append(
        f"Summary: {counts[STATUS_OK]} ok, "
        f"{counts[STATUS_WARN]} warn, "
        f"{counts[STATUS_FAIL]} fail, "
        f"{counts[STATUS_INFO]} info"
    )
    return "\n".join(out)


def _render_subsystem(
    sub: str, items: list[Check], glyphs: dict[str, str],
) -> list[str]:
    title = _SUBSYSTEM_TITLES.get(sub, sub.title())
    out = [title, "-" * len(title)]
    for c in items:
        out.append(f"  {glyphs[c.status]} {c.title}")
        if c.detail:
            for line in c.detail.splitlines():
                out.append(f"      {line}")
        if c.fix_hint:
            for line in c.fix_hint.splitlines():
                out.append(f"      → {line}")
    out.append("")
    return out


# ── Entry point used by --doctor ─────────────────────────────────────


def run_diagnostic_report(*, use_color: Optional[bool] = None) -> int:
    """Run all checks against real /sys, /proc, /dev. Print the report.

    Exit code:
      0 — no failures
      1 — at least one FAIL
      2 — at least one WARN but no FAIL
    """
    checks: list[Check] = []
    # Wrap each in try/except so one buggy check can't take down the
    # whole report — every other subsystem still runs.
    for fn in (
        check_display, check_keyboard, check_battery, check_backlight,
        check_audio, check_time_source, check_user_groups, check_config,
    ):
        try:
            checks.extend(fn())
        except Exception as exc:                                # noqa: BLE001
            _log.exception("doctor check %s raised", fn.__name__)
            checks.append(Check(
                subsystem=fn.__name__.replace("check_", ""),
                title=f"check itself crashed: {type(exc).__name__}",
                status=STATUS_FAIL,
                detail=str(exc),
                fix_hint="Please report this with the full traceback.",
            ))

    print(render_report(checks, use_color=use_color))

    if any(c.status == STATUS_FAIL for c in checks):
        return 1
    if any(c.status == STATUS_WARN for c in checks):
        return 2
    return 0
