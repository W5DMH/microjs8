#!/usr/bin/env python3
"""Build a Debian .deb for MicroJS8 in the M5Stack APPLaunch convention.

Adapts the M5CardputerZero-AppBuilder ``scripts/pack_deb.py`` pattern
(see https://github.com/m5stack/CardputerZero-AppBuilder) for a Python
application: instead of a single compiled binary, we ship the
microjs8 Python package under ``/usr/share/APPLaunch/lib/microjs8/``
and a small launcher shell script at
``/usr/share/APPLaunch/bin/microjs8`` that the systemd unit and the
APPLaunch ``.desktop`` entry both invoke.

Why this layout, in brief:

  - The M5Stack APPLaunch UI scans
    ``/usr/share/APPLaunch/applications/*.desktop`` and shows
    matching apps as launcher tiles. Putting our tile there is the
    whole point of using this layout.
  - The AppBuilder's `INSTALL_PREFIX = 'usr/share/APPLaunch'` is
    a Debian-friendly path (under ``/usr/share``) that survives
    ``dpkg``'s integrity checks. Our .deb declares files in this
    prefix as managed.
  - System Python with apt-installed deps, not a bundled venv.
    Phase 5 retired the Adafruit deps that forced MiniJS8 to
    bundle a venv; current deps are all in Debian (python3-pil,
    python3-numpy, python3-evdev, python3-sounddevice, python3-serial).
    This shrinks the .deb from ~80 MB (bundled venv) to ~1 MB
    (just our source).

The packager is hermetic — no network, no compilers needed beyond
``dpkg-deb``. Run on the dev box to produce a portable .deb that
installs identically on a CardputerZero.

Usage:
    python3 scripts/build_deb.py [--version 0.0.1] [--output-dir dist/]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger("build_deb")


# ── Layout constants (M5Stack APPLaunch convention) ──────────────────


# Trailing slash matters for clarity but we strip it on join.
INSTALL_PREFIX = "usr/share/APPLaunch"
BIN_PATH = f"{INSTALL_PREFIX}/bin"
LIB_PATH = f"{INSTALL_PREFIX}/lib"
SHARE_PATH = f"{INSTALL_PREFIX}/share"
APP_PATH = f"{INSTALL_PREFIX}/applications"
SERVICE_PATH = "lib/systemd/system"
ETC_PATH = "etc/microjs8"

# Phase 18.3 — additional install paths for udev rules, rigctld helpers,
# and the gfsk8 binary extension.
UDEV_RULES_PATH = "lib/udev/rules.d"
LOCAL_BIN_PATH = "usr/local/bin"
# Where the system python looks for native extensions on Bookworm.
# Matches the path used by Debian's python3-* packages.
PYTHON_DIST_PACKAGES = "usr/lib/python3/dist-packages"

# Default locations to look for the gfsk8 .so. The build host (hf256)
# typically has it from build.sh at /opt/microjs8/venv/...; the dev
# host might have it elsewhere. Operators can override via
# --gfsk8-so or by setting MICROJS8_GFSK8_SO in the environment.
DEFAULT_GFSK8_SEARCH_PATHS = (
    "/opt/microjs8/venv/lib/python3.11/site-packages/gfsk8.cpython-311-aarch64-linux-gnu.so",
    "/opt/microjs8/venv/lib/python3.11/site-packages/gfsk8.cpython-311-arm-linux-gnueabihf.so",
)

PACKAGE_NAME = "microjs8"
APP_NAME = "MicroJS8"
BIN_NAME = "microjs8"
ARCHITECTURE = "all"   # pure Python — gfsk8 .so is bundled as data


# ── Utility ──────────────────────────────────────────────────────────


def repo_root() -> Path:
    """Return the repository root, derived from this script's location."""
    return Path(__file__).resolve().parent.parent


def read_pyproject_version(pyproject: Path) -> str:
    """Extract `version = "x.y.z"` from pyproject.toml.

    We don't import ``tomllib`` to keep this script importable on
    Python 3.10 (Debian Bookworm has 3.11 but a future LTS may not).
    A simple regex on the top-level ``version`` key is sufficient
    because pyproject.toml has no nested ``version`` keys we'd
    confuse with the project version.
    """
    text = pyproject.read_text()
    # Anchor to a line that starts with `version = ` (allow
    # leading whitespace, but NOT inside a section like [tool.foo])
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError(f"no version found in {pyproject}")
    return m.group(1)


def render_template(src: Path, substitutions: dict[str, str]) -> str:
    """Read ``src`` and replace ``@KEY@`` tokens with their values.

    A simple in-house template format — Debian build tools use the
    same ``@VAR@`` convention. We don't pull in Jinja2 because:
      (a) We have only ~4 substitutions per file.
      (b) Adding a Jinja dep to the build script would be ironic
          given how careful Phase 5 was about trimming runtime deps.
    """
    text = src.read_text()
    for key, val in substitutions.items():
        text = text.replace(f"@{key}@", val)
    # Catch any remaining @TOKEN@ placeholders — almost certainly a typo.
    leftover = re.findall(r"@([A-Z_]+)@", text)
    if leftover:
        raise RuntimeError(
            f"{src}: unsubstituted template tokens: {sorted(set(leftover))}"
        )
    return text


def write_with_mode(path: Path, content: str, mode: int) -> None:
    """Atomically write ``content`` to ``path`` and set permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def _find_gfsk8_so(explicit: Optional[Path]) -> Optional[Path]:
    """Resolve the gfsk8 .so to bundle into the .deb.

    Resolution order (first existing match wins):
      1. ``explicit`` argument (typically from ``--gfsk8-so`` CLI)
      2. ``$MICROJS8_GFSK8_SO`` environment variable
      3. ``DEFAULT_GFSK8_SEARCH_PATHS`` (the build host's typical
         venv location from build.sh)

    Returns ``None`` if no candidate exists. The caller logs a
    loud warning in that case; we don't fail the build because
    host-side .deb builds (dev laptops without microjs8 deployed)
    still need to produce an installable package.
    """
    # Caller-supplied path wins.
    if explicit is not None:
        explicit = explicit.expanduser().resolve()
        if explicit.exists():
            return explicit
        _log.warning(
            "--gfsk8-so %s does not exist; falling through to other paths",
            explicit,
        )

    # Env var override.
    env_path = os.environ.get("MICROJS8_GFSK8_SO")
    if env_path:
        env_path_p = Path(env_path).expanduser().resolve()
        if env_path_p.exists():
            return env_path_p
        _log.warning(
            "MICROJS8_GFSK8_SO=%s does not exist; falling through",
            env_path,
        )

    # Default search paths (build.sh's typical install location).
    for candidate in DEFAULT_GFSK8_SEARCH_PATHS:
        c = Path(candidate)
        if c.exists():
            return c

    return None


# ── Packager ─────────────────────────────────────────────────────────


def build_deb(
    *,
    version: str,
    output_dir: Path,
    revision: str = "1",
    keep_staging: bool = False,
    gfsk8_so_path: Optional[Path] = None,
) -> Path:
    """Build microjs8_<version>-<revision>_all.deb.

    Returns the path to the produced .deb.

    Args:
      version       — Package version (no leading 'v'). Stamped into
                      DEBIAN/control and the filename.
      output_dir    — Where to place the produced .deb.
      revision      — Debian package revision, defaults to "1".
      keep_staging  — Leave the build's staging tree on disk for
                      inspection. Useful for CI debugging.
      gfsk8_so_path — Phase 18.3: optional explicit path to the
                      gfsk8 .so binary extension. If None, default
                      search paths and the MICROJS8_GFSK8_SO env
                      var are consulted. If still not found, the
                      .deb is built without it (warning logged).
    """
    root = repo_root()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use a deterministic staging dir inside output_dir so
    # subsequent ``dpkg-deb -b`` calls don't race.
    staging = output_dir / f"debian-{PACKAGE_NAME}-{version}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    deb_filename = f"{PACKAGE_NAME}_{version}-{revision}_{ARCHITECTURE}.deb"
    deb_path = output_dir / deb_filename

    # ── 1. Create the directory skeleton ────────────────────────────
    for d in (
        staging / "DEBIAN",
        staging / BIN_PATH,
        staging / LIB_PATH,
        staging / SHARE_PATH / "images",
        staging / APP_PATH,
        staging / SERVICE_PATH,
        staging / ETC_PATH,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # ── 2. Copy the Python package ──────────────────────────────────
    # The whole src/microjs8/ tree goes to /usr/share/APPLaunch/lib/microjs8/
    # so `python3 -m microjs8` finds it via the launcher's PYTHONPATH.
    # Skip __pycache__ — those get regenerated at runtime and would
    # bloat the .deb with content tied to our build host's Python.
    src_pkg = root / "src" / "microjs8"
    if not src_pkg.is_dir():
        raise RuntimeError(f"source package not found at {src_pkg}")
    dst_pkg = staging / LIB_PATH / "microjs8"
    shutil.copytree(
        src_pkg, dst_pkg,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    _log.info("copied Python package: %s → %s", src_pkg, dst_pkg)

    # ── 3. Render and install the launcher shell script ─────────────
    launcher_src = root / "packaging" / "microjs8-launcher.sh.in"
    launcher_dst = staging / BIN_PATH / BIN_NAME
    write_with_mode(
        launcher_dst,
        render_template(launcher_src, {}),
        0o755,
    )
    _log.info("installed launcher: %s", launcher_dst)

    # ── 3b. microjs8-enable-display helper (Phase 17) ───────────────
    # A standalone shell script that safely adds the ST7789V SPI
    # overlay to /boot/firmware/config.txt for bare Pi installs.
    # Installed under /usr/local/sbin so it's in operator PATH (vs
    # the launcher under /usr/share/APPLaunch/bin, which is not).
    # Only useful on bare Pi — on the CardputerZero the kernel
    # ships /dev/fb1 already; running this script there is a no-op
    # since the dtparam/dtoverlay lines would already be ignored.
    helper_src = root / "packaging" / "microjs8-enable-display.sh"
    helper_dst = staging / "usr/local/sbin" / "microjs8-enable-display"
    write_with_mode(
        helper_dst,
        helper_src.read_text(),
        0o755,
    )
    _log.info("installed display-enable helper: %s", helper_dst)

    # ── 4. systemd unit ─────────────────────────────────────────────
    service_src = root / "packaging" / "microjs8.service.in"
    service_dst = staging / SERVICE_PATH / f"{PACKAGE_NAME}.service"
    write_with_mode(
        service_dst,
        render_template(service_src, {}),
        0o644,
    )
    _log.info("installed systemd unit: %s", service_dst)

    # ── 5. .desktop entry ──────────────────────────────────────────
    desktop_src = root / "packaging" / "microjs8.desktop.in"
    desktop_dst = staging / APP_PATH / f"{PACKAGE_NAME}.desktop"
    write_with_mode(
        desktop_dst,
        render_template(desktop_src, {}),
        0o644,
    )
    _log.info("installed APPLaunch entry: %s", desktop_dst)

    # ── 6. Icon ─────────────────────────────────────────────────────
    icon_src = root / "packaging" / "microjs8.png"
    icon_dst = staging / SHARE_PATH / "images" / "microjs8.png"
    if icon_src.exists():
        shutil.copy2(icon_src, icon_dst)
        icon_dst.chmod(0o644)
        _log.info("installed icon: %s", icon_dst)
    else:
        _log.warning("no icon at %s — APPLaunch tile will be blank", icon_src)

    # ── 6.5. udev rule for Digirig (Phase 18.3) ─────────────────────
    # Creates /dev/digirig symlink and tags the device so gpsd and
    # ModemManager leave it alone. Without this:
    #   - gpsd would auto-grab any CP210x and assert RTS at open
    #     time, keying the radio's PTT permanently (chip-latches)
    #   - ModemManager would probe with AT commands, briefly opening
    #     the port and asserting RTS
    # Both failure modes were observed during PI-2W-TEST bring-up
    # (May 20, 2026); the rule shipped here prevents either from
    # happening on a fresh install.
    udev_src = root / "udev" / "99-microjs8-digirig.rules"
    if udev_src.exists():
        udev_dst = staging / UDEV_RULES_PATH / "99-microjs8-digirig.rules"
        write_with_mode(udev_dst, udev_src.read_text(), 0o644)
        _log.info("installed udev rule: %s", udev_dst)
    else:
        _log.warning(
            "no udev rule at %s — install will not auto-create "
            "/dev/digirig (operators must install it manually)",
            udev_src,
        )

    # ── 6.6. rigctld.service + launcher (Phase 18.3) ────────────────
    # The microjs8 daemon's CatService connects to rigctld at
    # 127.0.0.1:4532 for CAT-mode radios (QDX, G90+DigiRig). Without
    # this unit installed, CAT-mode radios can't be keyed; the
    # daemon logs "CAT disconnected; cannot key PTT" and the
    # scheduler abandons TX after 3 retries.
    rigctld_unit_src = root / "systemd" / "rigctld.service"
    if rigctld_unit_src.exists():
        rigctld_unit_dst = staging / SERVICE_PATH / "rigctld.service"
        write_with_mode(rigctld_unit_dst, rigctld_unit_src.read_text(), 0o644)
        _log.info("installed rigctld unit: %s", rigctld_unit_dst)
    else:
        _log.warning(
            "no rigctld.service at %s — CAT-mode radios won't have a "
            "PTT service",
            rigctld_unit_src,
        )

    launcher_src = root / "systemd" / "microjs8-rigctld-launcher"
    if launcher_src.exists():
        launcher_dst = staging / LOCAL_BIN_PATH / "microjs8-rigctld-launcher"
        write_with_mode(launcher_dst, launcher_src.read_text(), 0o755)
        _log.info("installed rigctld launcher: %s", launcher_dst)
    else:
        _log.warning(
            "no rigctld launcher at %s — rigctld.service won't be "
            "able to start",
            launcher_src,
        )

    # ── 6.7. gfsk8 .so (Phase 18.3 — bundle native extension) ───────
    # The JS8 modem is a C++ extension compiled separately by
    # build.sh into a venv at /opt/microjs8/venv/.../gfsk8*.so.
    # We copy it into the .deb so a vanilla `apt install` produces
    # a fully-functional install — no manual scp dance required.
    # If the .so isn't found at any expected path, we log loudly
    # but don't fail the build — host-side .deb builds (e.g. on a
    # dev laptop that doesn't run microjs8) should still produce
    # an installable package, just one where TX/RX won't work.
    gfsk8_src = _find_gfsk8_so(gfsk8_so_path)
    if gfsk8_src is not None:
        gfsk8_dst = staging / PYTHON_DIST_PACKAGES / gfsk8_src.name
        gfsk8_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gfsk8_src, gfsk8_dst)
        gfsk8_dst.chmod(0o644)
        _log.info("installed gfsk8 extension: %s (from %s)", gfsk8_dst, gfsk8_src)
    else:
        _log.warning(
            "no gfsk8 .so found at %s (or any default search path); "
            "the resulting .deb will install but TX/RX will fail "
            "with ModuleNotFoundError until gfsk8 is provided",
            gfsk8_so_path or "<none specified>",
        )

    # ── 7. Default config ───────────────────────────────────────────
    # Shipped read-only; postinst copies to the live config on first
    # install only. Operators edit /etc/microjs8/config.toml; their
    # edits survive package upgrades.
    config_src = root / "etc-defaults" / "config.toml"
    if config_src.exists():
        config_dst = staging / ETC_PATH / "config.toml.default"
        shutil.copy2(config_src, config_dst)
        config_dst.chmod(0o644)
        _log.info("installed default config: %s", config_dst)
    else:
        _log.warning("no default config at %s — install will not seed one", config_src)

    # ── 8. DEBIAN/control ───────────────────────────────────────────
    control_src = root / "packaging" / "control.in"
    control_dst = staging / "DEBIAN" / "control"
    control_text = render_template(control_src, {"VERSION": version})
    # Append computed Installed-Size — Debian convention; omitting it
    # makes apt show "0 B will be used" on install. Computed in KiB.
    installed_size_kb = _staging_installed_kb(staging)
    control_text = control_text.rstrip() + f"\nInstalled-Size: {installed_size_kb}\n"
    control_dst.write_text(control_text)
    control_dst.chmod(0o644)
    _log.info(
        "installed control: %s (Installed-Size=%d KiB)",
        control_dst, installed_size_kb,
    )

    # ── 9. DEBIAN/{postinst,prerm,postrm} ───────────────────────────
    for hook in ("postinst", "prerm", "postrm"):
        hook_src = root / "packaging" / f"{hook}.sh"
        hook_dst = staging / "DEBIAN" / hook
        # 0o755 is Debian-mandatory for maintainer scripts; lintian
        # complains otherwise.
        write_with_mode(hook_dst, hook_src.read_text(), 0o755)
    _log.info("installed maintainer scripts (postinst/prerm/postrm)")

    # ── 10. Build the .deb ──────────────────────────────────────────
    # --root-owner-group ensures the produced .deb claims root:root
    # ownership for every file regardless of who ran the build.
    # Without this, files would be owned by the build user (uid 1000
    # or whatever) and dpkg would happily install them as such.
    cmd = ["dpkg-deb", "--root-owner-group", "-b", str(staging), str(deb_path)]
    _log.info("running: %s", " ".join(cmd))
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"dpkg-deb failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    if not keep_staging:
        shutil.rmtree(staging)

    return deb_path


def _staging_installed_kb(staging: Path) -> int:
    """Sum the installed-on-disk size of all package files in KiB.

    Excludes the DEBIAN/ control directory — that doesn't ship to
    target. Mirrors dpkg-deb's own Installed-Size calculation.
    """
    total_bytes = 0
    for p in staging.rglob("*"):
        if "DEBIAN" in p.parts:
            continue
        if p.is_file():
            total_bytes += p.stat().st_size
    return (total_bytes + 1023) // 1024     # round up to KiB


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a .deb for MicroJS8 in M5Stack APPLaunch layout",
    )
    parser.add_argument(
        "--version", default=None,
        help="Package version (default: read from pyproject.toml)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("dist"),
        help="Where to write the .deb (default: ./dist)",
    )
    parser.add_argument(
        "--revision", default="1",
        help="Debian package revision (default: 1)",
    )
    parser.add_argument(
        "--keep-staging", action="store_true",
        help="Keep the build staging tree for inspection",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress info-level log output",
    )
    parser.add_argument(
        "--gfsk8-so", type=Path, default=None,
        help=(
            "Path to the gfsk8 .so binary extension to bundle into the .deb. "
            "If omitted, looks at default search paths and the MICROJS8_GFSK8_SO "
            "env var. If still not found, builds the .deb without it (TX/RX "
            "will fail with ModuleNotFoundError until gfsk8 is provided)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    version = args.version or read_pyproject_version(repo_root() / "pyproject.toml")
    deb_path = build_deb(
        version=version,
        output_dir=args.output_dir,
        revision=args.revision,
        keep_staging=args.keep_staging,
        gfsk8_so_path=args.gfsk8_so,
    )
    print(f"built: {deb_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
