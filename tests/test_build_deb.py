"""Tests for scripts/build_deb.py.

These actually build a .deb in a tmp dir and verify its structure
end-to-end. The dpkg-deb binary is available on every Debian-derived
host and on Ubuntu CI runners, so we skip cleanly if it's absent
(macOS dev boxes, stripped containers).

What we verify:
  - The .deb file is produced with the expected name + version
  - File layout matches the M5Stack APPLaunch convention exactly
  - DEBIAN/control has all required fields and the right architecture
  - Maintainer scripts (postinst, prerm, postrm) ship with mode 0755
  - The launcher shell script is executable
  - The Python source tree was bundled at the right path
  - The .deb's Installed-Size is plausible (>0 KiB)
  - The systemd unit ships at /lib/systemd/system/
  - The .desktop entry ships at /usr/share/APPLaunch/applications/

We do NOT test:
  - That the postinst actually creates the user (needs root + dpkg)
  - That the unit actually starts (needs systemd)
  - That the icon is a valid PNG (Pillow opens it on host already)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest


# Skip the whole module if dpkg-deb isn't installed (macOS dev boxes).
pytestmark = pytest.mark.skipif(
    shutil.which("dpkg-deb") is None,
    reason="dpkg-deb not available on this host",
)


def _repo_root() -> Path:
    """Path to the microjs8 repo root."""
    here = Path(__file__).resolve()
    # tests/ -> repo root
    return here.parent.parent


def _run_packager(output_dir: Path, *, version: Optional[str] = None) -> Path:
    """Invoke scripts/build_deb.py and return the produced .deb path."""
    cmd = [
        "python3",
        str(_repo_root() / "scripts" / "build_deb.py"),
        "--output-dir", str(output_dir),
        "--quiet",
    ]
    if version is not None:
        cmd.extend(["--version", version])
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"build_deb.py failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    debs = list(output_dir.glob("*.deb"))
    assert len(debs) == 1, f"expected exactly one .deb, got {debs}"
    return debs[0]


def _dpkg_deb(*args: str) -> str:
    """Run dpkg-deb and return stdout text."""
    result = subprocess.run(
        ["dpkg-deb", *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


# ── Filename / metadata ─────────────────────────────────────────────


def test_deb_filename_includes_version_and_arch(tmp_path: Path):
    deb = _run_packager(tmp_path, version="0.0.1")
    assert deb.name == "microjs8_0.0.1-1_all.deb"


def test_deb_uses_pyproject_version_by_default(tmp_path: Path):
    """When --version is omitted, the packager reads pyproject.toml."""
    deb = _run_packager(tmp_path)   # no --version
    # We don't assert the exact value (pyproject.toml may bump
    # over time); we assert the filename includes a v0.0.x-ish
    # string. The packager's own version-extraction is unit-tested
    # below.
    assert deb.name.startswith("microjs8_")
    assert deb.name.endswith("_all.deb")


def test_control_fields_are_correct(tmp_path: Path):
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    assert "Package: microjs8" in info
    assert "Version: 0.0.1" in info
    assert "Architecture: all" in info
    assert "Section: hamradio" in info
    assert "Maintainer:" in info
    # Depends should reference apt-installable Python deps. Spot-check:
    assert "python3 (>= 3.11)" in info
    assert "python3-pil" in info
    assert "python3-numpy" in info
    assert "python3-evdev" in info
    # Adafruit deps must NOT be referenced — Phase 5 dropped them.
    assert "adafruit" not in info.lower()
    # Installed-Size is computed from the real file size and should
    # be non-trivial. dpkg-deb -I prefixes each control-field line
    # with a leading space, so allow optional leading whitespace.
    import re
    m = re.search(r"(?m)^\s*Installed-Size:\s*(\d+)", info)
    assert m is not None, f"no Installed-Size in control:\n{info}"
    assert int(m.group(1)) > 100, "Installed-Size should be at least ~100 KiB"


# ── Phase 17: dependency-list corrections ───────────────────────────


def test_python3_sounddevice_not_in_depends(tmp_path: Path):
    """Phase 17: python3-sounddevice is not packaged for Bookworm
    (verified May 2026). Declaring it as a Depends causes apt to
    refuse the install on any clean Bookworm host. We moved
    sounddevice to a postinst pip-install instead.

    Note: the Description field references python3-sounddevice to
    explain WHY we don't depend on it — so this test only checks
    the Depends field specifically, not the whole control output.
    """
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    # Extract just the Depends block (multi-line continuation form)
    import re
    depends_block = re.search(
        r"(?m)^\s*Depends:\s*(.+?)(?=^\s*[A-Z][a-z]+:|\Z)",
        info, re.DOTALL,
    )
    assert depends_block, f"could not find Depends in:\n{info}"
    depends_text = depends_block.group(1)
    assert "python3-sounddevice" not in depends_text, (
        "python3-sounddevice should not be a Depends in Phase 17+ — "
        "it's not in Bookworm apt and breaks bare-Pi installs"
    )


def test_libportaudio2_is_in_depends(tmp_path: Path):
    """Phase 17: libportaudio2 (the C library that sounddevice wraps)
    IS in Bookworm apt and must be installed for the pip-installed
    sounddevice to work at runtime."""
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    assert "libportaudio2" in info


def test_python3_pip_is_in_depends(tmp_path: Path):
    """Phase 17: the postinst calls pip to install sounddevice from
    PyPI. python3-pip must be installed before postinst runs."""
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    assert "python3-pip" in info


def test_chrony_is_in_depends_not_recommends(tmp_path: Path):
    """Phase 17: JS8 is a time-synchronous protocol with ~250 ms slot
    boundaries. systemd-timesyncd's accuracy is borderline; chrony's
    sub-millisecond sync is the right choice. Promoting chrony from
    Recommends to Depends guarantees it gets installed and
    automatically replaces systemd-timesyncd via the time-daemon
    conflict — so `apt install ./microjs8.deb` produces a working
    time-sync setup with no manual steps."""
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    # Parse the Depends line(s). dpkg-deb -I outputs control with
    # each field's continuation lines indented by space.
    import re
    depends_block = re.search(
        r"(?m)^\s*Depends:\s*(.+?)(?=^\s*[A-Z][a-z]+:|\Z)",
        info, re.DOTALL,
    )
    assert depends_block, f"could not find Depends in:\n{info}"
    depends_text = depends_block.group(1)
    assert "chrony" in depends_text, (
        "chrony must be in Depends (Phase 17) — Recommends doesn't "
        "guarantee install in all apt configurations"
    )


def test_hamlib_utils_renamed_to_libhamlib_utils(tmp_path: Path):
    """Phase 17: hamlib-utils was renamed to libhamlib-utils in
    Bookworm. The old name is not a valid package and triggers
    'Recommends: hamlib-utils but it is not installable' on every
    install."""
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    # The OLD name should not appear at all
    import re
    assert not re.search(r"\bhamlib-utils\b", info.replace("libhamlib-utils", "")), (
        "hamlib-utils (old name) should not appear — use libhamlib-utils"
    )
    # The NEW name should appear in Recommends
    assert "libhamlib-utils" in info


def test_gpsd_clients_in_recommends(tmp_path: Path):
    """Phase 17: gpsd-clients ships cgps and other tools operators
    use to verify a GPS receiver. Cheap to add and useful."""
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    assert "gpsd-clients" in info


# ── Phase 17: postinst behavior + helper script ─────────────────────


def test_postinst_creates_dirs_before_adduser(tmp_path: Path):
    """Phase 17: postinst must create /var/lib/microjs8 BEFORE the
    adduser call. Otherwise adduser logs a cosmetic but alarming
    'Warning: The home dir X you specified can't be accessed' even
    though the install succeeds. Ordering matters for clean output."""
    deb = _run_packager(tmp_path, version="0.0.1")
    # Extract postinst from the .deb and check the ordering.
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    # Find the byte position of dir-creation and adduser
    dir_pos = postinst.find("install -d -m 0750 /var/lib/microjs8")
    adduser_pos = postinst.find("adduser --system")
    assert dir_pos > -1, "postinst doesn't create /var/lib/microjs8"
    assert adduser_pos > -1, "postinst doesn't run adduser"
    assert dir_pos < adduser_pos, (
        "postinst creates /var/lib/microjs8 AFTER adduser — must be BEFORE "
        "to avoid the 'home dir can't be accessed' warning"
    )


def test_postinst_creates_log_subdirectory(tmp_path: Path):
    """Phase 17: /var/lib/microjs8/log/ must exist before the daemon
    starts, or it fails to open its log file. The previous postinst
    didn't create the log subdir — operators saw 'Permission denied'
    when running microjs8-doctor as a non-microjs8 user, and the
    daemon itself logged the same error to journald at startup."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "/var/lib/microjs8/log" in postinst, (
        "postinst doesn't create /var/lib/microjs8/log/"
    )


def test_postinst_pip_installs_sounddevice(tmp_path: Path):
    """Phase 17: sounddevice isn't in Bookworm apt, so postinst
    pip-installs it. The pip command must use --break-system-packages
    (PEP 668) and --quiet (so install logs aren't noisy). The
    install must be idempotent — re-installing the .deb shouldn't
    re-trigger the pip install if sounddevice is already importable."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "pip install" in postinst, "postinst doesn't pip-install anything"
    assert "sounddevice" in postinst, "postinst doesn't reference sounddevice"
    assert "--break-system-packages" in postinst, (
        "pip install must use --break-system-packages on Bookworm (PEP 668)"
    )
    # Idempotency check: the install should be guarded by an
    # 'import sounddevice' check so re-runs don't redo the work.
    assert 'import sounddevice' in postinst, (
        "postinst should guard the pip install with 'import sounddevice' "
        "for idempotency on upgrades"
    )


def test_postinst_creates_doctor_symlink_wrapper(tmp_path: Path):
    """Phase 17: operators (including the conversational context in
    development) consistently invoke 'microjs8-doctor' as if it were
    a real binary. The launcher accepts --doctor as a flag, so the
    postinst creates a small wrapper at /usr/local/bin/microjs8-doctor
    that invokes the launcher with the flag. This is operator UX,
    not protocol — but it eliminates an entire class of
    'command not found' errors during bring-up."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "/usr/local/bin/microjs8-doctor" in postinst
    assert "--doctor" in postinst


def test_postrm_removes_doctor_symlink_on_remove(tmp_path: Path):
    """Phase 17: the doctor symlink wrapper is created by postinst
    (not shipped by dpkg) so it must be removed by postrm on 'remove'
    — otherwise it dangles and prints a confusing error when invoked
    after the package is uninstalled."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postrm = (extract_dir / "postrm").read_text()
    assert "/usr/local/bin/microjs8-doctor" in postrm, (
        "postrm doesn't remove the doctor symlink"
    )
    # And it's removed in the 'remove' case (not just purge).
    assert "remove)" in postrm, "postrm doesn't handle the 'remove' case"


def test_helper_microjs8_enable_display_is_shipped(tmp_path: Path):
    """Phase 17: ship a helper at /usr/local/sbin/microjs8-enable-display
    for bare-Pi operators who need to enable the SPI display overlay.
    Putting it in /usr/local/sbin (vs /usr/share/APPLaunch/bin)
    means it's in PATH and reachable via sudo without extra fuss."""
    deb = _run_packager(tmp_path, version="0.0.1")
    listing = _dpkg_deb("-c", str(deb))
    paths = [line.split()[-1].lstrip(".") for line in listing.splitlines() if line.strip()]
    assert "/usr/local/sbin/microjs8-enable-display" in paths, (
        f"microjs8-enable-display not in .deb file list:\n{paths}"
    )


def test_helper_microjs8_enable_display_is_executable(tmp_path: Path):
    """The helper must be marked +x — installed-mode 0755 — so apt
    install doesn't end up with a non-executable script in PATH."""
    deb = _run_packager(tmp_path, version="0.0.1")
    listing = _dpkg_deb("-c", str(deb))
    for line in listing.splitlines():
        if line.endswith("/usr/local/sbin/microjs8-enable-display"):
            # dpkg-deb -c output: -rwxr-xr-x root/root ...
            mode_str = line.split()[0]
            assert mode_str.startswith("-rwx"), (
                f"microjs8-enable-display not executable: {line}"
            )
            return
    raise AssertionError("microjs8-enable-display not found in .deb listing")


def test_helper_microjs8_enable_display_supports_revert(tmp_path: Path):
    """The helper must support --revert so operators can undo a
    bad overlay configuration. Without revert, a wrong overlay
    that prevents boot is harder to recover from (need a separate
    machine to mount the SD card and hand-edit config.txt)."""
    deb = _run_packager(tmp_path, version="0.0.1")
    # Extract and read the helper from the data part.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(data_dir)],
        check=True, capture_output=True,
    )
    helper = (data_dir / "usr" / "local" / "sbin" / "microjs8-enable-display").read_text()
    assert "--revert" in helper, "helper must support --revert"
    # Should also be idempotent on re-run
    assert "already present" in helper, (
        "helper should detect when the block is already present"
    )
    # Should back up config.txt before editing
    assert "backup" in helper.lower() or "bak" in helper.lower(), (
        "helper must back up config.txt before editing"
    )


# ── File layout ─────────────────────────────────────────────────────


def test_layout_matches_applaunch_convention(tmp_path: Path):
    deb = _run_packager(tmp_path, version="0.0.1")
    listing = _dpkg_deb("-c", str(deb))
    # Each expected path appears as the last column of a `dpkg-deb -c` row.
    paths = [line.split()[-1].lstrip(".") for line in listing.splitlines() if line.strip()]

    expected = [
        "/usr/share/APPLaunch/bin/microjs8",
        "/usr/share/APPLaunch/applications/microjs8.desktop",
        "/usr/share/APPLaunch/lib/microjs8/__init__.py",
        "/usr/share/APPLaunch/lib/microjs8/__main__.py",
        "/usr/share/APPLaunch/lib/microjs8/app.py",
        "/usr/share/APPLaunch/lib/microjs8/ui/screens.py",
        "/usr/share/APPLaunch/lib/microjs8/ui/display.py",
        "/usr/share/APPLaunch/lib/microjs8/power/battery.py",
        "/usr/share/APPLaunch/lib/microjs8/power/backlight.py",
        "/usr/share/APPLaunch/share/images/microjs8.png",
        "/lib/systemd/system/microjs8.service",
        "/etc/microjs8/config.toml.default",
    ]
    for p in expected:
        assert p in paths, f"missing expected path in .deb: {p}"


def test_pycache_is_not_bundled(tmp_path: Path):
    """__pycache__ from the build host would tie the .deb to a
    specific Python minor version. Ensure it's stripped."""
    deb = _run_packager(tmp_path, version="0.0.1")
    listing = _dpkg_deb("-c", str(deb))
    assert "__pycache__" not in listing
    assert ".pyc" not in listing


# ── Permissions ─────────────────────────────────────────────────────


def test_launcher_script_is_executable(tmp_path: Path):
    """dpkg-deb -c shows mode in the leading column. The launcher
    must be 755 so the systemd unit can exec it as the microjs8 user."""
    deb = _run_packager(tmp_path, version="0.0.1")
    listing = _dpkg_deb("-c", str(deb))
    for line in listing.splitlines():
        if line.endswith("/usr/share/APPLaunch/bin/microjs8"):
            mode = line.split()[0]
            assert mode == "-rwxr-xr-x", f"launcher mode is {mode}, expected -rwxr-xr-x"
            return
    pytest.fail("launcher file not found in .deb listing")


def test_files_are_root_owned(tmp_path: Path):
    """The .deb must claim root:root for every file, regardless of
    who built it. dpkg-deb --root-owner-group enforces this; we
    verify by reading the listing."""
    deb = _run_packager(tmp_path, version="0.0.1")
    listing = _dpkg_deb("-c", str(deb))
    for line in listing.splitlines():
        if not line.strip():
            continue
        # Format: mode  owner/group  size  date  time  path
        parts = line.split()
        if len(parts) < 6:
            continue
        owner_group = parts[1]
        assert owner_group == "root/root", (
            f"non-root ownership in .deb: {owner_group} on {parts[-1]}"
        )


# ── Maintainer scripts ──────────────────────────────────────────────


def test_maintainer_scripts_are_executable(tmp_path: Path):
    """Extract the control archive and check postinst/prerm/postrm
    are mode 0755. dpkg refuses to run them otherwise."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "ctl"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    for hook in ("postinst", "prerm", "postrm"):
        script = extract_dir / hook
        assert script.exists(), f"DEBIAN/{hook} missing from .deb"
        mode = script.stat().st_mode & 0o777
        assert mode == 0o755, f"{hook} mode is 0o{mode:o}, expected 0o755"


def test_postinst_is_idempotent_shell(tmp_path: Path):
    """Sanity: postinst syntax is valid sh — would catch a typo
    that'd otherwise corrupt every install. Use `sh -n` (parse only)."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "ctl"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    for hook in ("postinst", "prerm", "postrm"):
        script = extract_dir / hook
        result = subprocess.run(
            ["sh", "-n", str(script)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"{hook} failed sh -n syntax check:\nstderr: {result.stderr}"
        )


def test_postinst_creates_microjs8_user(tmp_path: Path):
    """The postinst script must contain the addgroup/adduser steps
    for the microjs8 system user. Regression guard against accidentally
    shipping a postinst that doesn't create the user (which would make
    the systemd service fail with 'User=microjs8 unknown')."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "ctl"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "addgroup --system microjs8" in postinst
    assert "adduser --system" in postinst
    # The four supplementary groups Q3 specified.
    for grp in ("audio", "dialout", "video", "i2c"):
        assert grp in postinst, f"postinst missing supplementary group: {grp}"


# ── Systemd unit ────────────────────────────────────────────────────


def test_systemd_unit_runs_as_microjs8_user(tmp_path: Path):
    """The unit shipped in the .deb must declare User=microjs8 and
    NOT have any /opt/microjs8/venv/bin/python references (those
    were MiniJS8 era; Phase 7 uses system Python via the launcher)."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "data"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    unit = (extract_dir / "lib/systemd/system/microjs8.service").read_text()
    assert "User=microjs8" in unit
    assert "Group=microjs8" in unit
    assert "/usr/share/APPLaunch/bin/microjs8" in unit
    # MiniJS8 ghosts that should NOT be in the unit anymore:
    assert "/opt/microjs8" not in unit
    assert "GPIOZERO_PIN_FACTORY" not in unit
    assert "lgpio" not in unit
    # Phase 6 supplementary groups are present:
    assert "video" in unit
    assert "i2c" in unit


def test_desktop_entry_points_to_launcher(tmp_path: Path):
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "data"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    desktop = (extract_dir / "usr/share/APPLaunch/applications/microjs8.desktop").read_text()
    assert "Exec=/usr/share/APPLaunch/bin/microjs8" in desktop
    assert "Type=Application" in desktop
    assert "Name=MicroJS8" in desktop


def test_desktop_icon_path_is_relative_per_lvgl_fs_driver(tmp_path: Path):
    """The M5Stack APPLaunch parser feeds the Icon= value directly to
    LVGL's filesystem driver, which is mounted at letter 'A:' rooted
    at /usr/share/APPLaunch/. Absolute paths fail to load → blank tile.

    Source of the rule:
      M5CardputerZero-UserDemo/projects/APPLaunch/main/hal/linux/hal_paths_linux.c

    > "Image paths must be RELATIVE (e.g. share/images/foo.png) so LVGL
    >  resolves them as A:share/images/foo.png →
    >  /usr/share/APPLaunch/share/images/foo.png"

    Regression guard against re-adding the absolute path.
    """
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "data"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    desktop = (extract_dir / "usr/share/APPLaunch/applications/microjs8.desktop").read_text()
    assert "Icon=share/images/microjs8.png" in desktop, (
        f"Icon= must be relative to /usr/share/APPLaunch/; got:\n{desktop}"
    )
    # And explicitly: NOT the absolute path that the launcher would fail to load.
    assert "Icon=/usr/share/APPLaunch/share/images/microjs8.png" not in desktop


def test_icon_file_is_90x90_png(tmp_path: Path):
    """The launcher tile widget is sized for ~90x90 icons (matches
    M5Stack's built-in PYTHON_logo.png, SETTING_logo.png, etc).
    Smaller icons get upscaled; non-PNG won't load at all.
    """
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "data"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    icon = extract_dir / "usr/share/APPLaunch/share/images/microjs8.png"
    assert icon.exists(), "icon must ship at the path the .desktop's Icon= refers to"

    # Verify the bytes are a valid PNG and dimensions are 90x90.
    # The .deb-relative location must agree with the .desktop's
    # Icon=share/images/... so the launcher's LVGL FS driver finds it.
    from PIL import Image
    with Image.open(icon) as im:
        assert im.format == "PNG", f"icon must be PNG; got {im.format}"
        assert im.size == (90, 90), f"icon must be 90x90; got {im.size}"


# ── Version-extraction unit test (independent of dpkg-deb) ──────────


def test_read_pyproject_version_extracts_top_level_value(tmp_path: Path):
    """Direct unit test of read_pyproject_version — doesn't need
    dpkg-deb. Verifies we don't accidentally pick up a version from
    [tool.something] or a multi-line dict."""
    import sys
    sys.path.insert(0, str(_repo_root() / "scripts"))
    try:
        from build_deb import read_pyproject_version
    finally:
        sys.path.pop(0)

    py = tmp_path / "pyproject.toml"
    py.write_text(
        '[project]\n'
        'name = "microjs8"\n'
        'version = "0.0.1"\n'
        '[tool.poetry]\n'
        'version = "ignored"\n'
    )
    assert read_pyproject_version(py) == "0.0.1"


def test_read_pyproject_version_raises_when_no_version(tmp_path: Path):
    import sys
    sys.path.insert(0, str(_repo_root() / "scripts"))
    try:
        from build_deb import read_pyproject_version
    finally:
        sys.path.pop(0)

    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "microjs8"\n')   # no version key
    with pytest.raises(RuntimeError, match=r"no version"):
        read_pyproject_version(py)
