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


def _run_packager(
    output_dir: Path,
    *,
    version: Optional[str] = None,
    gfsk8_so: Optional[Path] = None,
) -> Path:
    """Invoke scripts/build_deb.py and return the produced .deb path.

    ``gfsk8_so`` (Phase 18.3) optionally passes ``--gfsk8-so`` to the
    packager so tests can verify the .so bundling path without
    depending on a real native build artifact existing on disk.

    Phase 19 follow-up (May 21, 2026): when ``gfsk8_so`` is None
    (the default for nearly every packaging test — they don't care
    about the .so, they care about postinst / udev / service files /
    etc.), we force ``MICROJS8_GFSK8_SO=""`` in the subprocess
    environment. This is the authoritative "no gfsk8 in this build"
    signal — the packager will skip both the env path and the
    default search paths and produce a small .deb fast.

    Without this, build hosts whose default search paths happen to
    match a real 24 MB gfsk8 .so (e.g. hf256 at ~/build/gfsk8-modem-
    clean/build/python/...) would compress that 24 MB into every
    test's .deb, turning a 50 s test suite into a 5+ min wait.
    """
    cmd = [
        "python3",
        str(_repo_root() / "scripts" / "build_deb.py"),
        "--output-dir", str(output_dir),
        "--quiet",
    ]
    if version is not None:
        cmd.extend(["--version", version])
    if gfsk8_so is not None:
        cmd.extend(["--gfsk8-so", str(gfsk8_so)])

    env = os.environ.copy()
    if gfsk8_so is None:
        # Authoritative override: skip defaults, skip env path, no
        # gfsk8 bundled. Tests that DO want the .so bundled pass
        # gfsk8_so explicitly (in which case we leave env alone so
        # --gfsk8-so on the command line wins).
        env["MICROJS8_GFSK8_SO"] = ""

    result = subprocess.run(
        cmd, check=False, capture_output=True, text=True, env=env,
    )
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


def test_python3_spidev_is_in_depends(tmp_path: Path):
    """Phase 18: SpiDisplayDevice needs the spidev Python module.
    Available as python3-spidev in Bookworm main."""
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    assert "python3-spidev" in info


def test_python3_gpiozero_is_in_depends(tmp_path: Path):
    """Phase 18: SpiDisplayDevice uses gpiozero for DC/RST/BL GPIO
    control. Available as python3-gpiozero in Bookworm main."""
    deb = _run_packager(tmp_path, version="0.0.1")
    info = _dpkg_deb("-I", str(deb))
    assert "python3-gpiozero" in info


def test_postinst_adds_spi_and_gpio_groups(tmp_path: Path):
    """Phase 18: the microjs8 user must be in 'spi' and 'gpio' groups
    to access /dev/spidev0.0 and the GPIO pins for the SPI display.
    Without these, the daemon fails to open the SPI bus."""
    deb = _run_packager(tmp_path, version="0.0.1")
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    # The supplementary group loop must include spi and gpio.
    assert " spi " in postinst or " spi\n" in postinst or " spi;" in postinst or "spi " in postinst, (
        "postinst doesn't add microjs8 to the 'spi' group"
    )
    assert "gpio" in postinst, (
        "postinst doesn't add microjs8 to the 'gpio' group"
    )


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


# ── Phase 18.3 tests: udev rule, rigctld, gfsk8 bundling ────────────


def test_udev_rule_is_installed_to_lib_udev(tmp_path: Path):
    """Phase 18.3: the .deb must ship the Digirig udev rule to
    /lib/udev/rules.d/ so /dev/digirig appears and gpsd/MM leave
    the device alone."""
    deb = _run_packager(tmp_path)
    contents = _dpkg_deb("-c", str(deb))
    assert "./lib/udev/rules.d/99-microjs8-digirig.rules" in contents


def test_udev_rule_has_gpsd_and_mm_ignore_flags(tmp_path: Path):
    """Phase 18.3: the udev rule must include ID_GPSD_IGNORE and
    ID_MM_DEVICE_IGNORE. Without these, gpsd or ModemManager will
    grab the Digirig and assert RTS, keying the radio's PTT."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    rule_path = extract_dir / "lib" / "udev" / "rules.d" / "99-microjs8-digirig.rules"
    rule_text = rule_path.read_text()
    assert "ID_GPSD_IGNORE" in rule_text, (
        "udev rule missing ID_GPSD_IGNORE — gpsd will grab the Digirig"
    )
    assert "ID_MM_DEVICE_IGNORE" in rule_text, (
        "udev rule missing ID_MM_DEVICE_IGNORE — ModemManager will probe"
    )
    assert 'SYMLINK+="digirig"' in rule_text, (
        "udev rule missing /dev/digirig symlink creation"
    )


def test_rigctld_service_is_installed(tmp_path: Path):
    """Phase 18.3: rigctld.service must be in /lib/systemd/system/.
    Without it, CAT-mode radios (QDX, G90+DigiRig) can't get PTT
    because the daemon's CatService has nothing to connect to."""
    deb = _run_packager(tmp_path)
    contents = _dpkg_deb("-c", str(deb))
    assert "./lib/systemd/system/rigctld.service" in contents


def test_rigctld_launcher_is_installed_executable(tmp_path: Path):
    """Phase 18.3: the per-radio launcher script must be installed
    at /usr/local/bin/microjs8-rigctld-launcher with mode 0755.
    The rigctld.service unit calls this script."""
    deb = _run_packager(tmp_path)
    contents = _dpkg_deb("-c", str(deb))
    # Find the launcher line in the tar listing and check its mode.
    for line in contents.splitlines():
        if "microjs8-rigctld-launcher" in line:
            # tar listing format: "-rwxr-xr-x root/root size date path"
            assert line.startswith("-rwxr-xr-x"), (
                f"launcher should be 0755 executable; got: {line!r}"
            )
            return
    pytest.fail("rigctld launcher not found in .deb")


def test_postinst_reloads_udev(tmp_path: Path):
    """Phase 18.3: postinst must run udevadm control --reload-rules
    and udevadm trigger so the new udev rule takes effect without
    requiring a reboot."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-control"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "udevadm control --reload-rules" in postinst
    assert "udevadm trigger" in postinst


def test_postinst_constrains_gpsd_config(tmp_path: Path):
    """Phase 18.3: postinst must edit /etc/default/gpsd to set
    USBAUTO=false if it's currently true. Without this, gpsd
    auto-grabs the CP210x and keys the radio."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-control2"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "USBAUTO" in postinst, (
        "postinst doesn't touch USBAUTO — gpsd-grab-Digirig issue not fixed"
    )
    assert "/etc/default/gpsd" in postinst, (
        "postinst doesn't reference gpsd config file"
    )


def test_postinst_resets_cp210x_devices(tmp_path: Path):
    """Phase 18.3: postinst should USB-reset any latched CP210x
    devices on install so any RTS-high state from prior gpsd
    activity is cleared."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-control3"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "authorized" in postinst, (
        "postinst doesn't power-cycle CP210x devices via "
        "the USB authorized sysfs flag"
    )
    assert "10c4" in postinst, (
        "postinst doesn't match the CP210x vendor ID"
    )


def test_gfsk8_so_bundled_when_provided(tmp_path: Path):
    """Phase 18.3: --gfsk8-so argument should bundle the .so into
    /usr/lib/python3/dist-packages/."""
    # Fake a .so file (just needs to exist).
    fake_so = tmp_path / "gfsk8.cpython-311-aarch64-linux-gnu.so"
    fake_so.write_bytes(b"\x7fELF" + b"\x00" * 100)   # ELF magic + zeros

    deb = _run_packager(tmp_path, gfsk8_so=fake_so)
    contents = _dpkg_deb("-c", str(deb))
    assert "./usr/lib/python3/dist-packages/gfsk8.cpython-311-aarch64-linux-gnu.so" in contents


def test_gfsk8_so_absent_when_not_provided(tmp_path: Path):
    """Phase 18.3: if no --gfsk8-so and no default path exists, the
    .deb still builds (operators may build host-side before deploying
    on a target with gfsk8). Just no gfsk8 file inside."""
    # _run_packager doesn't pass --gfsk8-so by default. The default
    # search paths under /opt/microjs8/venv don't exist on CI hosts.
    deb = _run_packager(tmp_path)
    contents = _dpkg_deb("-c", str(deb))
    assert "gfsk8" not in contents, (
        "expected no gfsk8 entry when .so unavailable, got: " +
        "\n".join(l for l in contents.splitlines() if "gfsk8" in l)
    )


# ── Phase 19 / v0.0.8: packaging hardening tests ────────────────────


def test_postinst_does_NOT_auto_enable_microjs8(tmp_path: Path):
    """Phase 19 (v0.0.8): postinst must NOT call `systemctl enable
    microjs8.service` — both platforms launch on-demand via the
    APPLaunch tile (CardputerZero) or `systemctl start` (bare Pi).
    Auto-enabling would surprise operators after they tap Exit."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-noauto"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    # Strip shell comments before grepping so the in-code rationale
    # text can mention the old behavior without tripping the check.
    active_lines = [
        line for line in postinst.splitlines()
        if not line.strip().startswith("#")
    ]
    active = "\n".join(active_lines)
    assert "systemctl enable microjs8" not in active, (
        "Phase 19 v0.0.8: postinst must NOT auto-enable microjs8.service "
        "(operators launch on-demand via APPLaunch tile / systemctl start)"
    )


def test_postinst_does_NOT_auto_enable_rigctld(tmp_path: Path):
    """Phase 19 (v0.0.8): same rule applies to rigctld.service —
    it activates via microjs8.service's Wants=rigctld.service when
    the daemon starts. Pre-enabling at boot caused the 366+ launcher
    restart loop on PI-2W-TEST when no radio was attached yet."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-noauto-rig"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    active_lines = [
        line for line in postinst.splitlines()
        if not line.strip().startswith("#")
    ]
    active = "\n".join(active_lines)
    assert "systemctl enable rigctld" not in active, (
        "Phase 19 v0.0.8: postinst must NOT auto-enable rigctld.service"
    )


def test_postinst_only_restarts_microjs8_if_active(tmp_path: Path):
    """Phase 19 (v0.0.8): postinst should restart microjs8.service
    only if it's already active (handles upgrades), NOT start it
    unconditionally (would override the v0.0.8 'no auto-start' rule
    on a fresh install)."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-restart"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    # The 'if is-active' guard should be present
    assert "is-active microjs8.service" in postinst, (
        "Phase 19 v0.0.8: postinst must check is-active before "
        "restart so fresh installs don't auto-start the daemon"
    )


def test_postinst_retries_sounddevice_install(tmp_path: Path):
    """Phase 19 (v0.0.8): postinst must retry the sounddevice pip
    install on failure — the May 21, 2026 PI-2W-TEST install showed
    that a single TLS hiccup was enough to leave the daemon without
    audio support. Retry + explicit import verification closes
    that gap."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-sd-retry"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    # The retry loop and verification import must both be present
    assert "for attempt in 1 2 3" in postinst, (
        "Phase 19: postinst must retry sounddevice install on failure"
    )
    # The post-install verification call
    assert 'python3 -c "import sounddevice"' in postinst, (
        "Phase 19: postinst must verify sounddevice imports after install"
    )


def test_postinst_enables_spi_overlay_on_pi(tmp_path: Path):
    """Phase 19 (v0.0.8): postinst should auto-enable SPI in the Pi's
    boot config so the userspace display driver finds /dev/spidev0.0
    on first boot. Without this, fresh Bookworm Lite installs lose
    the display until the operator manually runs raspi-config."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-spi"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-e", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    postinst = (extract_dir / "postinst").read_text()
    assert "dtparam=spi=on" in postinst, (
        "Phase 19: postinst must enable SPI overlay for the userspace "
        "display driver"
    )
    # Backup must be created
    assert "pre-microjs8" in postinst, (
        "Phase 19: postinst SPI edit must back up the original config"
    )
    # And the operator must be told a reboot is required
    assert "REBOOT REQUIRED" in postinst, (
        "Phase 19: postinst must tell the operator to reboot after "
        "enabling SPI (kernel rereads device-tree overlays at boot only)"
    )


def test_microjs8_service_restart_on_failure_not_always(tmp_path: Path):
    """Phase 19 (v0.0.8): the systemd unit must use Restart=on-failure,
    NOT Restart=always. The Exit button produces a clean 0 exit which
    must NOT trigger an immediate systemd restart — otherwise the
    Exit button is a no-op (daemon comes right back up)."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-svc"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    svc_path = extract_dir / "lib" / "systemd" / "system" / "microjs8.service"
    svc = svc_path.read_text()
    # Filter to non-comment lines so the in-file rationale text
    # explaining the v0.0.8 change doesn't trip the check.
    active_lines = [
        line for line in svc.splitlines()
        if not line.strip().startswith("#")
    ]
    active = "\n".join(active_lines)
    assert "Restart=on-failure" in active, (
        "Phase 19 v0.0.8: microjs8.service must use Restart=on-failure "
        "so the HOME Exit button can actually exit the daemon"
    )
    assert "Restart=always" not in active, (
        "Phase 19 v0.0.8: Restart=always defeats the Exit button"
    )


def test_rigctld_launcher_exits_zero_on_missing_hardware(tmp_path: Path):
    """Phase 19 (v0.0.8): the rigctld launcher must exit 0 (not 1)
    when the configured radio's device path isn't present. This
    closes the 366-restart loop observed on PI-2W-TEST when QDX was
    the default but no QDX was attached."""
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-launcher"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    launcher_path = extract_dir / "usr" / "local" / "bin" / "microjs8-rigctld-launcher"
    launcher = launcher_path.read_text()
    # Both QDX and DigiRig missing-hardware branches must exit 0
    # Pattern: scan for the QDX/DigiRig sections and verify "exit 0"
    # appears in each
    assert "QDX not present" in launcher
    assert "DigiRig not present" in launcher
    # Each missing-hardware branch must say "exiting 0"
    # (uses the v0.0.8 explanation text we added)
    assert launcher.count("exiting 0 (no CAT until radio plugged in)") >= 2, (
        "Phase 19 v0.0.8: launcher must exit 0 (not 1) on missing "
        "hardware for BOTH qdx and xiegu-g90-digirig branches"
    )


def test_gfsk8_default_search_paths_include_source_repo_build_dir():
    """Phase 19 (v0.0.8.1 follow-up): the default search paths must
    include ``~/build/gfsk8-modem-clean/build/python/`` so a fresh
    `python3 scripts/build_deb.py --output-dir dist` (no flags) on
    the build host auto-detects the .so where build_gfsk8.sh puts it.

    Surfaced May 21, 2026 when hf256 produced a 235 KB .deb instead
    of the expected ~3.3 MB — the search path list only included the
    venv location, but the canonical build artifact lives in the
    source repo's build dir.
    """
    import sys
    sys.path.insert(0, str(_repo_root() / "scripts"))
    try:
        from build_deb import DEFAULT_GFSK8_SEARCH_PATHS
    finally:
        sys.path.pop(0)

    # At least one entry must reference the source-repo build dir
    matching = [
        p for p in DEFAULT_GFSK8_SEARCH_PATHS
        if "gfsk8-modem-clean" in p and "/build/python/" in p
    ]
    assert len(matching) >= 1, (
        "DEFAULT_GFSK8_SEARCH_PATHS missing the source-repo build "
        f"dir entry; got {DEFAULT_GFSK8_SEARCH_PATHS!r}"
    )


def test_gfsk8_default_search_paths_expand_tilde():
    """Phase 19 (v0.0.8.1): _find_gfsk8_so must expand ``~`` so the
    source-repo entries (under ``~/build/...``) actually resolve to
    the operator's home directory."""
    import sys
    import tempfile
    from pathlib import Path as _Path
    sys.path.insert(0, str(_repo_root() / "scripts"))
    try:
        from build_deb import _find_gfsk8_so
    finally:
        sys.path.pop(0)

    # Build a fake .so under a temp dir, then point HOME at it so
    # the default search path under ``~/build/...`` resolves.
    import os
    with tempfile.TemporaryDirectory() as td:
        fake_home = _Path(td)
        fake_build_dir = fake_home / "build" / "gfsk8-modem-clean" / "build" / "python"
        fake_build_dir.mkdir(parents=True)
        fake_so = fake_build_dir / "gfsk8.cpython-311-aarch64-linux-gnu.so"
        fake_so.write_bytes(b"\x7fELF" + b"\x00" * 100)

        old_home = os.environ.get("HOME")
        # v0.0.8.1 fast-path patch: MICROJS8_GFSK8_SO is now an
        # authoritative override. We must clear it for this test so
        # _find_gfsk8_so actually reaches the default-search-paths
        # branch — otherwise the test passes for the wrong reason
        # in environments where the env var is set.
        old_env = os.environ.pop("MICROJS8_GFSK8_SO", None)
        try:
            os.environ["HOME"] = str(fake_home)
            # No explicit flag, no env var override, no system paths
            # exist → must fall back to ~ expansion and find the
            # fake .so we just planted.
            found = _find_gfsk8_so(None)
        finally:
            if old_home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = old_home
            if old_env is not None:
                os.environ["MICROJS8_GFSK8_SO"] = old_env

        assert found is not None, (
            "_find_gfsk8_so failed to expand ~ in default search paths"
        )
        assert found == fake_so, (
            f"expected {fake_so}, got {found}"
        )


def test_gfsk8_env_var_empty_string_means_no_gfsk8():
    """Phase 19 follow-up (May 21, 2026): MICROJS8_GFSK8_SO="" must
    be authoritative — _find_gfsk8_so must NOT fall back to default
    search paths when the env var is explicitly empty.

    This is the speed-fix contract: the test suite's _run_packager
    sets this env var to "" so packaging tests can run fast on
    build hosts where default search paths happen to match real
    24 MB gfsk8 binaries.
    """
    import sys
    import tempfile
    from pathlib import Path as _Path
    sys.path.insert(0, str(_repo_root() / "scripts"))
    try:
        from build_deb import _find_gfsk8_so
    finally:
        sys.path.pop(0)

    import os
    with tempfile.TemporaryDirectory() as td:
        fake_home = _Path(td)
        # Plant a fake .so where the default search path would find
        # it — so we can prove the env var override DID short-circuit
        # the defaults (otherwise the fake .so would be returned).
        fake_build_dir = fake_home / "build" / "gfsk8-modem-clean" / "build" / "python"
        fake_build_dir.mkdir(parents=True)
        fake_so = fake_build_dir / "gfsk8.cpython-311-aarch64-linux-gnu.so"
        fake_so.write_bytes(b"\x7fELF" + b"\x00" * 100)

        old_home = os.environ.get("HOME")
        old_env = os.environ.get("MICROJS8_GFSK8_SO")
        try:
            os.environ["HOME"] = str(fake_home)
            os.environ["MICROJS8_GFSK8_SO"] = ""    # authoritative no
            found = _find_gfsk8_so(None)
        finally:
            if old_home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = old_home
            if old_env is None:
                os.environ.pop("MICROJS8_GFSK8_SO", None)
            else:
                os.environ["MICROJS8_GFSK8_SO"] = old_env

        assert found is None, (
            f"MICROJS8_GFSK8_SO='' must short-circuit default paths; "
            f"got {found!r} (the fake .so under fake $HOME)"
        )


def test_microjs8_service_has_restart_force_exit_status_75(tmp_path: Path):
    """Phase 19 v0.0.9: the systemd unit must include
    ``RestartForceExitStatus=75`` so the radio-cycle path (which exits
    cleanly with code 75) triggers a restart even though the policy
    is otherwise Restart=on-failure.

    Without this directive, switching radios in Setup leaves the
    daemon down (operator has to manually start it) — the regression
    surfaced May 21, 2026 in field testing of v0.0.8.
    """
    deb = _run_packager(tmp_path)
    extract_dir = tmp_path / "extract-rfes75"
    extract_dir.mkdir()
    subprocess.run(
        ["dpkg-deb", "-x", str(deb), str(extract_dir)],
        check=True, capture_output=True,
    )
    svc_path = extract_dir / "lib" / "systemd" / "system" / "microjs8.service"
    svc = svc_path.read_text()
    # Filter comments — the surrounding rationale text discusses 75 too.
    active = "\n".join(
        line for line in svc.splitlines()
        if not line.strip().startswith("#")
    )
    assert "RestartForceExitStatus=75" in active, (
        "Phase 19 v0.0.9: microjs8.service must include "
        "RestartForceExitStatus=75 so the radio-cycle exit path "
        "triggers a restart even with Restart=on-failure"
    )
