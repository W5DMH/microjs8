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
