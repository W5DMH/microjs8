"""Tests for microjs8.doctor.

Every check_* function takes injectable paths or callables, so this
module exercises the full report paths against synthetic ``/sys``,
``/proc``, ``/dev`` trees built into a ``tmp_path``. No real hardware
is touched.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

from microjs8.doctor import (
    Check,
    STATUS_FAIL,
    STATUS_INFO,
    STATUS_OK,
    STATUS_WARN,
    check_audio,
    check_backlight,
    check_battery,
    check_config,
    check_display,
    check_keyboard,
    check_time_source,
    check_user_groups,
    render_report,
    run_diagnostic_report,
)


# ── check_display ────────────────────────────────────────────────────


def _setup_fb(tmp_path: Path, *, w: int = 320, h: int = 170, bpp: int = 16,
              stride: Optional[int] = None, name: str = "fb_st7789v",
              idx: int = 0) -> tuple[Path, Path]:
    """Build a fake /proc/fb + /sys/class/graphics tree and return both."""
    proc_fb = tmp_path / "fb"
    proc_fb.write_text(f"{idx} {name}\n")
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    fb_dir = sysfs / f"fb{idx}"
    fb_dir.mkdir()
    (fb_dir / "virtual_size").write_text(f"{w},{h}\n")
    (fb_dir / "stride").write_text(f"{stride if stride is not None else w*bpp//8}\n")
    (fb_dir / "bits_per_pixel").write_text(f"{bpp}\n")
    return proc_fb, sysfs


def test_check_display_warns_when_no_proc_fb(tmp_path: Path):
    """No /proc/fb at all (Pi 4/5 KMS-only or stripped container)."""
    missing_proc = tmp_path / "no_such"
    checks = check_display(proc_fb=missing_proc, sysfs_root=tmp_path)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN
    assert "not present" in checks[0].title


def test_check_display_warns_when_no_st7789v_entry(tmp_path: Path):
    """/proc/fb exists but has no fb_st7789v line — typical on dev hosts."""
    proc_fb = tmp_path / "fb"
    proc_fb.write_text("0 fb_drm_hdmi\n")
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    checks = check_display(proc_fb=proc_fb, sysfs_root=sysfs)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN
    assert "fb_st7789v" in checks[0].title
    # The detail should include what /proc/fb actually contains so
    # the operator can see the mismatch.
    assert "fb_drm_hdmi" in (checks[0].detail or "")


def test_check_display_fails_on_unexpected_dimensions(tmp_path: Path):
    """If /proc/fb finds the panel but it's the wrong size, fail."""
    proc_fb, sysfs = _setup_fb(tmp_path, w=240, h=240)        # MiniJS8 size
    checks = check_display(proc_fb=proc_fb, sysfs_root=sysfs)
    assert any(c.status == STATUS_FAIL for c in checks)
    fail = next(c for c in checks if c.status == STATUS_FAIL)
    assert "240×240" in fail.title or "240x240" in fail.title


def test_check_display_fails_on_non_16_bpp(tmp_path: Path):
    proc_fb, sysfs = _setup_fb(tmp_path, bpp=32)
    checks = check_display(proc_fb=proc_fb, sysfs_root=sysfs)
    assert any(c.status == STATUS_FAIL for c in checks)
    fail = next(c for c in checks if c.status == STATUS_FAIL)
    assert "bpp=32" in fail.title


def test_check_display_fails_when_devnode_missing(tmp_path: Path):
    """sysfs says fb0 exists but /dev/fb0 doesn't.

    We can't easily fake /dev so this test is best-effort: if /dev/fb0
    happens to exist on the test host, skip.
    """
    if Path("/dev/fb0").exists():
        pytest.skip("test host has /dev/fb0; cannot validate the missing-devnode path")
    proc_fb, sysfs = _setup_fb(tmp_path)                       # idx=0 → /dev/fb0
    checks = check_display(proc_fb=proc_fb, sysfs_root=sysfs)
    assert any(c.status == STATUS_FAIL for c in checks)
    fail = next(c for c in checks if c.status == STATUS_FAIL)
    assert "/dev/fb0" in fail.title and "missing" in fail.title


# ── check_keyboard ───────────────────────────────────────────────────


def test_check_keyboard_warns_when_evdev_missing(tmp_path: Path):
    missing = tmp_path / "no_keyboard"
    checks = check_keyboard(evdev_path=missing)
    # Phase 16: now 2 checks — the multi-source discovery probe
    # AND the legacy single-path probe. Both should WARN on dev
    # host where neither path resolves to a real keyboard.
    assert len(checks) == 2
    # Find the legacy single-path check (the one with "not present"
    # in the title — it includes the path).
    legacy_check = next(c for c in checks if "not present" in c.title)
    assert legacy_check.status == STATUS_WARN
    # The new discovery probe should also WARN (no keyboards on dev host).
    discovery_check = next(c for c in checks if "discovered" in c.title)
    assert discovery_check.status == STATUS_WARN


def test_check_keyboard_warns_about_placeholder_scancodes(tmp_path: Path, monkeypatch):
    """Even when the evdev device exists, surface the Phase 3
    placeholder warning since real scancodes haven't been captured."""
    fake = tmp_path / "evdev"
    fake.write_bytes(b"")
    fake.chmod(0o444)
    # Ensure the env vars are NOT set — they trigger the alternate
    # "scancodes already overridden" branch.
    monkeypatch.delenv("MICROJS8_FN_B_KEYCODE", raising=False)
    monkeypatch.delenv("MICROJS8_FN_Q_KEYCODE", raising=False)

    checks = check_keyboard(evdev_path=fake)
    titles = [c.title for c in checks]
    # The 'present and readable' check should be OK
    assert any("present and readable" in t for t in titles)
    # AND we should also surface the placeholder warning
    placeholder_check = next(
        (c for c in checks if "placeholder" in c.title.lower()), None
    )
    assert placeholder_check is not None
    assert placeholder_check.status == STATUS_WARN


def test_check_keyboard_acknowledges_overridden_scancodes(tmp_path: Path, monkeypatch):
    """When the operator has overridden via env vars (common after
    capturing real scancodes from hardware), surface that as OK."""
    fake = tmp_path / "evdev"
    fake.write_bytes(b"")
    fake.chmod(0o444)
    monkeypatch.setenv("MICROJS8_FN_B_KEYCODE", "162")
    monkeypatch.setenv("MICROJS8_FN_Q_KEYCODE", "166")

    checks = check_keyboard(evdev_path=fake)
    override_check = next(
        (c for c in checks if "overrides set" in c.title), None
    )
    assert override_check is not None
    assert override_check.status == STATUS_OK
    assert "162" in override_check.title
    assert "166" in override_check.title


# ── check_battery ────────────────────────────────────────────────────


def test_check_battery_warns_when_no_bq27(tmp_path: Path):
    psupply = tmp_path / "power_supply"
    psupply.mkdir()
    (psupply / "AC").mkdir()
    checks = check_battery(base_path=psupply)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN
    # Detail should include the names of supplies that ARE there so
    # the operator can see what the kernel called the battery.
    assert "AC" in (checks[0].detail or "")


def test_check_battery_warns_when_psupply_missing(tmp_path: Path):
    """/sys/class/power_supply itself doesn't exist (e.g. stripped container)."""
    missing = tmp_path / "no_power_supply"
    checks = check_battery(base_path=missing)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN


def test_check_battery_reports_state_when_found(tmp_path: Path):
    psupply = tmp_path / "power_supply"
    bdir = psupply / "bq27220-0"
    bdir.mkdir(parents=True)
    (bdir / "capacity").write_text("87\n")
    (bdir / "status").write_text("Charging\n")
    (bdir / "voltage_now").write_text("4050000\n")     # 4.05 V
    (bdir / "current_now").write_text("250000\n")       # +250 mA
    (bdir / "temp").write_text("285\n")                 # 28.5 °C

    checks = check_battery(base_path=psupply)
    assert len(checks) == 1
    assert checks[0].status == STATUS_OK
    assert "87%" in checks[0].title
    assert "Charging" in checks[0].title
    assert "↑" in checks[0].title
    assert "4.05" in (checks[0].detail or "")


# ── check_backlight ──────────────────────────────────────────────────


def test_check_backlight_warns_when_missing(tmp_path: Path):
    missing = tmp_path / "no_backlight"
    checks = check_backlight(sysfs_path=missing)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN


def test_check_backlight_reports_brightness(tmp_path: Path):
    bl = tmp_path / "backlight"
    bl.mkdir()
    (bl / "max_brightness").write_text("255\n")
    (bl / "brightness").write_text("128\n")
    checks = check_backlight(sysfs_path=bl)
    assert len(checks) == 1
    assert checks[0].status == STATUS_OK
    assert "128/255" in checks[0].title


def test_check_backlight_fails_when_files_missing(tmp_path: Path):
    bl = tmp_path / "backlight"
    bl.mkdir()
    # Only max_brightness; no brightness file
    (bl / "max_brightness").write_text("255\n")
    checks = check_backlight(sysfs_path=bl)
    assert any(c.status == STATUS_FAIL for c in checks)


# ── check_audio ──────────────────────────────────────────────────────


def test_check_audio_warns_when_no_capture_devices():
    """sounddevice returns only output devices."""
    fake_devices = [
        {"index": 0, "name": "Output Only", "max_input_channels": 0},
    ]
    checks = check_audio(query_devices_fn=lambda: fake_devices)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN


def test_check_audio_lists_capture_devices():
    fake_devices = [
        {"index": 0, "name": "Output Only", "max_input_channels": 0},
        {"index": 1, "name": "QDX Audio", "max_input_channels": 2},
        {"index": 2, "name": "DigiRig", "max_input_channels": 1},
    ]
    checks = check_audio(query_devices_fn=lambda: fake_devices)
    assert len(checks) == 1
    assert checks[0].status == STATUS_OK
    assert "2 capture-capable" in checks[0].title
    assert "QDX Audio" in (checks[0].detail or "")
    assert "DigiRig" in (checks[0].detail or "")


def test_check_audio_handles_query_failure():
    def bad_query():
        raise RuntimeError("portaudio segfaulted")
    checks = check_audio(query_devices_fn=bad_query)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN
    assert "portaudio segfaulted" in (checks[0].detail or "")


# ── check_time_source ────────────────────────────────────────────────


def test_check_time_source_ok():
    checks = check_time_source(chrony_ok_fn=lambda: True)
    assert len(checks) == 1
    assert checks[0].status == STATUS_OK


def test_check_time_source_warn_when_chrony_unhappy():
    checks = check_time_source(chrony_ok_fn=lambda: False)
    assert len(checks) == 1
    assert checks[0].status == STATUS_WARN
    assert "consensus" in (checks[0].detail or "")


# ── check_user_groups ────────────────────────────────────────────────


def test_check_user_groups_reports_running_user():
    """At minimum, user_groups should always emit the 'running as ...' info line."""
    checks = check_user_groups()
    assert any(c.status == STATUS_INFO and "running as" in c.title for c in checks)


# ── check_config ─────────────────────────────────────────────────────


def test_check_config_warns_on_unconfigured_callsign(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[station]\n'
        'callsign = "N0CALL"\n'
        'grid = ""\n'
        'units_distance = "miles"\n'
    )
    checks = check_config(config_path=cfg_file)
    titles_statuses = [(c.title, c.status) for c in checks]
    assert any("N0CALL" in t and s == STATUS_WARN for t, s in titles_statuses), (
        f"expected a WARN about N0CALL, got: {titles_statuses}"
    )
    assert any("grid" in t.lower() and s == STATUS_WARN for t, s in titles_statuses), (
        f"expected a WARN about grid, got: {titles_statuses}"
    )


def test_check_config_ok_when_configured(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[station]\n'
        'callsign = "K1ABC"\n'
        'grid = "FN42"\n'
        'units_distance = "miles"\n'
    )
    checks = check_config(config_path=cfg_file)
    callsign_check = next(c for c in checks if "callsign" in c.title.lower())
    grid_check = next(c for c in checks if c.title.startswith("grid"))
    assert callsign_check.status == STATUS_OK
    assert "K1ABC" in callsign_check.title
    assert grid_check.status == STATUS_OK
    assert "FN42" in grid_check.title


def test_check_config_fails_on_invalid_toml(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[station\nthis is not valid toml\n")
    checks = check_config(config_path=cfg_file)
    assert any(c.status == STATUS_FAIL for c in checks)


# ── render_report formatting ─────────────────────────────────────────


def test_render_report_groups_by_subsystem():
    checks = [
        Check(subsystem="display", title="display ok", status=STATUS_OK),
        Check(subsystem="battery", title="battery missing", status=STATUS_WARN),
        Check(subsystem="display", title="another display line", status=STATUS_INFO),
    ]
    out = render_report(checks, use_color=False)
    # Both display lines should land under the display subsystem title.
    display_block_start = out.index("Display")
    display_block_end = out.find("Battery", display_block_start)
    display_block = out[display_block_start:display_block_end]
    assert "display ok" in display_block
    assert "another display line" in display_block


def test_render_report_summary_counts_each_status():
    checks = [
        Check(subsystem="x", title="a", status=STATUS_OK),
        Check(subsystem="x", title="b", status=STATUS_OK),
        Check(subsystem="x", title="c", status=STATUS_WARN),
        Check(subsystem="x", title="d", status=STATUS_FAIL),
    ]
    out = render_report(checks, use_color=False)
    summary = next(line for line in out.splitlines() if line.startswith("Summary:"))
    assert "2 ok" in summary
    assert "1 warn" in summary
    assert "1 fail" in summary


def test_render_report_plain_ascii_when_no_color():
    checks = [Check(subsystem="x", title="t", status=STATUS_OK)]
    out = render_report(checks, use_color=False)
    # No ANSI escape sequences when colour is disabled
    assert "\x1b[" not in out
    assert "[ OK ]" in out


def test_render_report_unknown_subsystem_still_appears():
    """A future check_xxx() that introduces a new subsystem name
    must not be silently dropped."""
    checks = [Check(subsystem="quantum", title="quantum decoherence ok", status=STATUS_OK)]
    out = render_report(checks, use_color=False)
    assert "Quantum" in out or "quantum" in out
    assert "quantum decoherence ok" in out


# ── run_diagnostic_report end-to-end ─────────────────────────────────


def test_run_diagnostic_report_returns_int(capsys):
    """The end-to-end probe must always succeed (no exceptions) and
    return an integer exit code, regardless of what's on the host."""
    rc = run_diagnostic_report(use_color=False)
    assert isinstance(rc, int)
    captured = capsys.readouterr()
    # Output must not be empty — at least the title line should be there.
    assert "MicroJS8" in captured.out
    assert "Summary:" in captured.out


def test_run_diagnostic_report_isolates_check_failures(capsys, monkeypatch):
    """If one check_* function raises, the others must still run and
    the broken one is reported as a FAIL line."""
    from microjs8 import doctor as doctor_mod

    def boom():
        raise RuntimeError("synthetic failure for testing")

    monkeypatch.setattr(doctor_mod, "check_display", boom)
    rc = run_diagnostic_report(use_color=False)
    captured = capsys.readouterr()
    # Other subsystems still appear (we didn't break them).
    assert "Configuration" in captured.out or "Time source" in captured.out
    # The failure is reported, not raised.
    assert "synthetic failure" in captured.out
    assert rc == 1                      # any FAIL → 1
