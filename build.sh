#!/bin/bash
# MicroJS8 Raspberry Pi Image Builder
# ===================================
# Builds a flashable SD card image for a MicroJS8 node (Pi Zero 2W target).
#
# Run on a Pi 4 (native arm64) or x86-64 Linux host with qemu-user-static
# binfmt registered, as root:
#
#   sudo bash build.sh
#
# Input:  bookworm-lite-arm64.img   (Raspberry Pi OS Bookworm Lite, arm64)
#                                    placed in $PROJECT_DIR
#
# Output: output/microjs8-YYYYMMDD.img.xz       (compressed, ~600-700 MB)
#         output/microjs8-YYYYMMDD.img.xz.sha256
#         output/build-YYYYMMDD.log
#
# Flash:
#   xz -dk output/microjs8-YYYYMMDD.img.xz
#   sudo dd if=output/microjs8-YYYYMMDD.img of=/dev/sdX bs=4M status=progress
#   # or use Raspberry Pi Imager — select the .xz file directly
#
# After flashing and booting on the Pi Zero 2W:
#   - Check the daemon is running:
#       ssh microjs8@<ip> 'systemctl status microjs8'
#   - Tail the journal:
#       ssh microjs8@<ip> 'journalctl -u microjs8 -f'

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
INPUT_IMG="$PROJECT_DIR/bookworm-lite-arm64.img"
OUTPUT_DIR="$PROJECT_DIR/output"
DATE_STR="$(date +%Y%m%d)"
OUTPUT_IMG="$OUTPUT_DIR/microjs8-${DATE_STR}.img"
WORK_IMG="$OUTPUT_DIR/microjs8-work.img"
MOUNT_DIR="/mnt/microjs8-build"
LOG="$OUTPUT_DIR/build-${DATE_STR}.log"
BOOT_MNT="$MOUNT_DIR/boot"
ROOT_MNT="$MOUNT_DIR/root"

LOOP_BOOT=""
LOOP_ROOT=""

# ── GFSK8 modem (pinned to a specific commit on the W5DMH fork) ────
# We build the modem .so inside the chroot at image-build time. There
# are no upstream wheels, the build uses CMake + pybind11, and we
# require ABI compatibility with Bookworm's libstdc++ — so chroot-
# build is the right answer despite the time cost (~3-5 min on Pi 4).
# Update GFSK8_COMMIT deliberately when you want to pull a new revision.
GFSK8_REPO="https://github.com/W5DMH/gfsk8-modem-clean.git"
GFSK8_COMMIT="6586f492e19f79f8accae057a890fba9a491839f"

# ── Logging ───────────────────────────────────────────────────────────────────
mkdir -p "$OUTPUT_DIR"
exec > >(tee -a "$LOG") 2>&1

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] ✓ $*"; }
warn() { echo "[$(date '+%H:%M:%S')] ⚠ $*"; }
err()  { echo "[$(date '+%H:%M:%S')] ✗ $*"; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
log "=================================================="
log " MicroJS8 Image Builder"
log " $(date)"
log "=================================================="
log " Project:  $PROJECT_DIR"
log " Input:    $INPUT_IMG"
log " Output:   ${OUTPUT_IMG}.xz"
log "=================================================="

[ "$(id -u)" -eq 0 ] || err "Must run as root: sudo bash build.sh"

for cmd in losetup mount umount chroot rsync xz parted \
           e2fsck resize2fs truncate sha256sum; do
    command -v "$cmd" &>/dev/null || err "Missing required tool: $cmd"
done

[ -f "$INPUT_IMG" ] || err "Base image not found: $INPUT_IMG
  Download Raspberry Pi OS Bookworm Lite (arm64) from
    https://www.raspberrypi.com/software/operating-systems/
  and place it as:
    $INPUT_IMG"

# Verify our project files are all here before we start mounting things.
REQUIRED_FILES=(
    "src/microjs8/__init__.py"
    "src/microjs8/__main__.py"
    "src/microjs8/app.py"
    "src/microjs8/config.py"
    "src/microjs8/logging_setup.py"
    "src/microjs8/paths.py"
    "src/microjs8/version.py"
    "pyproject.toml"
    "systemd/microjs8.service"
    "systemd/rigctld.service"
    "udev/99-microjs8-qdx.rules"
    "udev/99-microjs8-gps.rules"
    "udev/99-microjs8-audio.rules"
    "polkit/50-microjs8-poweroff.rules"
    "boot-config/microjs8.conf"
    "etc-defaults/config.toml"
    "etc-defaults/gpsd/gpsd"
    "etc-defaults/chrony/microjs8-gps.conf"
)
for f in "${REQUIRED_FILES[@]}"; do
    [ -f "$PROJECT_DIR/$f" ] || err "Missing project file: $f"
done

ok "Preflight checks passed"

# ── Copy base image ───────────────────────────────────────────────────────────
log "Copying base image to working copy..."
cp "$INPUT_IMG" "$WORK_IMG"
ok "Working image: $WORK_IMG ($(du -h "$WORK_IMG" | cut -f1))"

# ── Expand image by 2 GB ──────────────────────────────────────────────────────
# Bookworm Lite + Python venv + Pillow/numpy/sounddevice/etc. ≈ 600 MB
# during install. The GFSK8 in-chroot build adds ~150 MB of object
# files temporarily (we delete them after the .so is staged into the
# venv). 2 GB headroom keeps the link step safely above the
# free-space threshold and gives Step 6 (CAT/TX additions) room
# without another resize pass. A 1 GB headroom previously caused a
# ld: "No space left on device" failure during the GFSK8 link step.
# Final image size grows accordingly but xz compression on the trailing
# zeros keeps the .img.xz under control.
log "Expanding image by 2048 MB..."
truncate -s +2048M "$WORK_IMG"

LOOP_DEV=$(losetup --find --show "$WORK_IMG")
log "  Loop device: $LOOP_DEV"
partprobe "$LOOP_DEV" 2>/dev/null || true

parted -s "$LOOP_DEV" resizepart 2 100%
ok "Partition expanded"
losetup -d "$LOOP_DEV"

# ── Mount image ───────────────────────────────────────────────────────────────
SECTOR_SIZE=512
BOOT_OFFSET=$(parted -s "$WORK_IMG" unit s print | awk '/^ 1/{print $2}' | tr -d 's')
ROOT_OFFSET=$(parted -s "$WORK_IMG" unit s print | awk '/^ 2/{print $2}' | tr -d 's')
BOOT_BYTES=$(( BOOT_OFFSET * SECTOR_SIZE ))
ROOT_BYTES=$(( ROOT_OFFSET * SECTOR_SIZE ))

mkdir -p "$BOOT_MNT" "$ROOT_MNT"

LOOP_ROOT=$(losetup --find --show --offset "$ROOT_BYTES" "$WORK_IMG")
log "  Root loop: $LOOP_ROOT"
log "Checking and resizing root filesystem..."
e2fsck -f -y "$LOOP_ROOT" || warn "e2fsck reported issues (continuing)"
resize2fs "$LOOP_ROOT"
ok "Root filesystem resized"

mount "$LOOP_ROOT" "$ROOT_MNT"
LOOP_BOOT=$(losetup --find --show --offset "$BOOT_BYTES" "$WORK_IMG")
mount "$LOOP_BOOT" "$BOOT_MNT"
ok "Image mounted at $MOUNT_DIR"

# ── Cleanup trap ──────────────────────────────────────────────────────────────
cleanup() {
    log "Cleaning up mounts..."
    for dir in dev/pts dev proc sys run; do
        umount "$ROOT_MNT/$dir" 2>/dev/null || true
    done
    umount "$ROOT_MNT/boot/firmware" 2>/dev/null || true
    umount "$BOOT_MNT"              2>/dev/null || true
    umount "$ROOT_MNT"              2>/dev/null || true
    losetup -d "$LOOP_BOOT"         2>/dev/null || true
    losetup -d "$LOOP_ROOT"         2>/dev/null || true
    log "Cleanup complete"
}
trap cleanup EXIT

# ── Bind mounts for chroot ────────────────────────────────────────────────────
log "Setting up chroot bind mounts..."
mount --bind /dev     "$ROOT_MNT/dev"
mount --bind /dev/pts "$ROOT_MNT/dev/pts"
mount --bind /proc    "$ROOT_MNT/proc"
mount --bind /sys     "$ROOT_MNT/sys"
mount --bind /run     "$ROOT_MNT/run"
mkdir -p "$ROOT_MNT/boot/firmware"
mount --bind "$BOOT_MNT" "$ROOT_MNT/boot/firmware"
ok "Bind mounts ready"

# DNS for apt inside chroot
cp "$ROOT_MNT/etc/resolv.conf" "$ROOT_MNT/etc/resolv.conf.bak" 2>/dev/null || true
cp /etc/resolv.conf "$ROOT_MNT/etc/resolv.conf"

run_chroot() { chroot "$ROOT_MNT" /bin/bash -c "$1"; }

# ── Hostname & SSH ────────────────────────────────────────────────────────────
log "Setting hostname to microjs8..."
echo "microjs8" > "$ROOT_MNT/etc/hostname"
sed -i 's/raspberrypi/microjs8/g' "$ROOT_MNT/etc/hosts" 2>/dev/null || true
ok "Hostname: microjs8"

log "Enabling SSH..."
touch "$BOOT_MNT/ssh"
run_chroot "systemctl enable ssh" || true
ok "SSH enabled"

# ── boot/config.txt modifications ─────────────────────────────────────────────
BOOT_CFG="$BOOT_MNT/config.txt"
CMDLINE="$BOOT_MNT/cmdline.txt"

log "Configuring /boot/firmware/config.txt..."
# Append our snippet idempotently (grep before adding).
if ! grep -q "MicroJS8 — boot config" "$BOOT_CFG"; then
    echo "" >> "$BOOT_CFG"
    echo "# === MicroJS8 — boot config (appended by build.sh) ===" >> "$BOOT_CFG"
    cat "$PROJECT_DIR/boot-config/microjs8.conf" >> "$BOOT_CFG"
    ok "config.txt extended"
else
    log "  config.txt already contains MicroJS8 block; skipping"
fi

# Disable USB autosuspend in cmdline (prevents the QDX or USB audio from
# powering down during quiet periods, which would corrupt the first frame
# of the next TX or drop incoming audio samples).
if ! grep -q "usbcore.autosuspend" "$CMDLINE"; then
    sed -i 's/$/ usbcore.autosuspend=-1/' "$CMDLINE"
    ok "USB autosuspend disabled in cmdline.txt"
else
    log "  cmdline.txt already has usbcore.autosuspend; skipping"
fi

# ── User account ──────────────────────────────────────────────────────────────
# Two users:
#   pi        — interactive SSH user (default password "microjs8setup")
#   microjs8   — service account that runs the daemon (no shell login)
log "Configuring user accounts..."
HASHED_PW=$(echo "microjs8setup" | openssl passwd -6 -stdin)
echo "pi:${HASHED_PW}" > "$BOOT_MNT/userconf"
run_chroot "id pi 2>/dev/null || \
    useradd -m -s /bin/bash -G sudo,dialout,audio,plugdev,gpio,i2c,spi,netdev pi"
run_chroot "echo 'pi:microjs8setup' | chpasswd"
run_chroot "usermod -a -G dialout,audio,gpio,spi pi"

# Ensure the 'input' group exists. Bookworm has it pre-created (used
# by udev for /dev/input/event* perms), but if a future stripped base
# image lacks it, we add it here so the useradd below doesn't fail.
run_chroot "getent group input >/dev/null || groupadd --system input"

# Service account — non-interactive, just a /var/lib home so systemd is happy.
run_chroot "id microjs8 2>/dev/null || \
    useradd --system --home-dir /var/lib/microjs8 --shell /usr/sbin/nologin \
            --groups audio,dialout,gpio,spi,input microjs8"

ok "Users configured (pi: SSH; microjs8: service account)"

# Disable Pi first-run wizard (we have our own first-boot logic)
run_chroot "rm -f /etc/xdg/autostart/piwiz.desktop 2>/dev/null || true"
sed -i 's| init=/usr/lib/raspberrypi-sys-mods/firstboot||g' \
    "$BOOT_MNT/cmdline.txt" 2>/dev/null || true

# ── APT packages ──────────────────────────────────────────────────────────────
log "Updating APT package lists..."
run_chroot "apt-get update -qq" || warn "apt-get update had issues"

log "Installing system packages..."
PACKAGES=(
    # Python — Bookworm ships 3.11 by default, satisfying our >=3.11 floor.
    python3
    python3-venv
    python3-dev

    # Audio (ALSA only — no PulseAudio).
    libasound2
    libportaudio2
    alsa-utils

    # Build tools — needed for any wheel that compiles native code at
    # install time (e.g. lgpio, sounddevice if no manylinux wheel).
    gcc
    libffi-dev

    # Pillow native deps — the manylinux wheel covers most cases but
    # libjpeg/libfreetype/zlib are needed for full image format support
    # and the DejaVu fonts the UI loads at startup.
    libjpeg-dev
    zlib1g-dev
    libfreetype6-dev
    fonts-dejavu-core

    # SPI / GPIO native libs for the Mini PiTFT display + buttons.
    # lgpio is the gpiozero backend on Bookworm.
    liblgpio-dev

    # GFSK8 modem build deps (Step 5). build.sh clones and CMake-builds
    # the JS8/FT8 modem in-chroot at image build time; install it into
    # the venv as a single .so. pybind11-dev is the system pybind11
    # headers (no need for the Python pip package — CMake will find
    # /usr/share/cmake/pybind11/).
    cmake
    build-essential
    pybind11-dev

    # Time discipline. GPS dongle → gpsd → chrony in later steps.
    chrony
    gpsd
    gpsd-clients

    # CAT control via rigctld (Step 6). libhamlib-utils provides the
    # rigctld daemon that owns /dev/qdx and exposes a TCP protocol on
    # localhost:4532. Our CatService talks to it. rigctl (the CLI
    # client) is also handy for diagnostics: `rigctl -m 2 -r
    # localhost:4532 f` queries the radio frequency from any shell.
    libhamlib-utils

    # SQLite CLI for diagnostic queries against /var/microjs8/messages.db
    # ("SELECT count(*) FROM heard_stations" etc). The Python sqlite3
    # module is in the stdlib so the daemon doesn't need this — it's
    # purely an operator convenience.
    sqlite3

    # Quality of life on the device.
    git
    curl
    less
    vim-tiny
)

run_chroot "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ${PACKAGES[*]}" \
    || err "APT package installation failed"
ok "System packages installed"

# Remove fake-hwclock — GPS will provide time in later steps. Keeping
# fake-hwclock around fights chrony when GPS lock arrives.
run_chroot "apt-get remove -y fake-hwclock 2>/dev/null || true"
run_chroot "systemctl disable fake-hwclock 2>/dev/null || true"
run_chroot "rm -f /etc/fake-hwclock.data"

# ── Application install ───────────────────────────────────────────────────────
log "Installing MicroJS8 application to /opt/microjs8..."
mkdir -p "$ROOT_MNT/opt/microjs8"

# Stage the package source, the pyproject, and the wheels dir into the
# install root. We use rsync so future revisions only copy deltas.
rsync -a --delete \
    "$PROJECT_DIR/src/" \
    "$ROOT_MNT/opt/microjs8/src/"
cp "$PROJECT_DIR/pyproject.toml" "$ROOT_MNT/opt/microjs8/"
cp "$PROJECT_DIR/README.md"      "$ROOT_MNT/opt/microjs8/" 2>/dev/null || true

# Wheels directory for offline pip installs in later steps (e.g. the
# pinned GFSK8 wheel for your fork). Empty in Step 1 — that's fine.
mkdir -p "$ROOT_MNT/opt/microjs8/wheels"
if compgen -G "$PROJECT_DIR/wheels/*.whl" > /dev/null; then
    cp "$PROJECT_DIR/wheels/"*.whl "$ROOT_MNT/opt/microjs8/wheels/"
    log "  copied $(ls "$PROJECT_DIR/wheels/"*.whl | wc -l) wheel(s)"
fi

ok "Application files staged"

# Create the venv and install the package, all inside the chroot so the
# resulting Python interpreter and any compiled wheels match the target
# architecture.
log "Creating Python virtualenv at /opt/microjs8/venv..."
run_chroot "python3 -m venv --without-pip /opt/microjs8/venv" \
    || err "venv creation failed"

# Bootstrap pip inside the venv. We use ensurepip (stdlib) so we don't
# need network for this step alone.
run_chroot "/opt/microjs8/venv/bin/python -m ensurepip --upgrade" \
    || err "pip bootstrap failed"
run_chroot "/opt/microjs8/venv/bin/python -m pip install --upgrade pip setuptools wheel" \
    || warn "pip self-upgrade failed (continuing)"

log "Installing MicroJS8 package into venv..."
# Install from /opt/microjs8/ where pyproject.toml lives. The
# package-dir = {"" = "src"} entry in pyproject.toml tells setuptools
# to find the actual Python package inside src/.
run_chroot "cd /opt/microjs8 && /opt/microjs8/venv/bin/pip install --no-build-isolation ." \
    || err "MicroJS8 package install failed"

# Smoke test: the installed package must import cleanly and report its version.
log "Smoke test: importing microjs8 inside the venv..."
run_chroot "/opt/microjs8/venv/bin/python -c 'import microjs8; print(\"  microjs8\", microjs8.__version__)'" \
    || err "microjs8 package failed to import after install"
ok "Application installed"

# ── GFSK8 modem (Step 5) ──────────────────────────────────────────────────────
#
# Two installation paths, picked at build time:
#
#   PATH 1 — STAGED PRE-BUILT WHEEL (preferred, fast, deterministic)
#   ----------------------------------------------------------------
#   If a pre-compiled .so is staged at:
#       $PROJECT_DIR/wheels/gfsk8.cpython-311-aarch64-linux-gnu.so
#   we copy it directly into the venv and skip the chroot build.
#
#   The operator builds this once on the dev box (Pi 4) and saves it
#   in the project's wheels/ directory. Each subsequent build picks
#   it up automatically. This is the path validated against real
#   on-air decode/encode for months — it's the exact binary the
#   operator has tested in the field.
#
#   Why this is preferred:
#     - Build time drops by ~3 minutes (no CMake step in chroot)
#     - The wheel binary is identical across rebuilds — no chance
#       of compiler-version, optimization-flag, or chroot-environment
#       drift producing a different (potentially broken) binary
#     - The May 4 image build produced a chroot-built wheel that
#       silently failed: gfsk8.modulate() returned a buffer of all
#       zeros, which our weak smoke test (only checked import +
#       constant read) didn't catch. The TX path then keyed the
#       radio for a few ms with no audio. A pre-built wheel from a
#       known-good source eliminates that class of failure.
#
#   PATH 2 — CHROOT BUILD (fallback, original behavior)
#   ---------------------------------------------------
#   If no pre-built .so is staged, we clone the pinned commit and
#   build it inside the chroot at image-build time. Same code as
#   before — kept as a fallback for clean-room builds where no wheel
#   is available, and so first-time users without a dev box can
#   still build.
#
#   Either way, we run a SMOKE TEST that actually exercises the
#   modulator. If it returns silent audio (the buffer-lifetime UB
#   symptom), the build fails loudly — this is what the May 4 build
#   should have done.
#
#   Update GFSK8_COMMIT (defined near the top of this script) when
#   you intentionally pull a new revision into the chroot fallback
#   path. The staged wheel path doesn't reference this constant.

STAGED_WHEEL="$PROJECT_DIR/wheels/gfsk8.cpython-311-aarch64-linux-gnu.so"
SO_DEST="/opt/microjs8/venv/lib/python3.11/site-packages/gfsk8.cpython-311-aarch64-linux-gnu.so"

if [ -f "$STAGED_WHEEL" ]; then
    log "Using staged GFSK8 .so from $STAGED_WHEEL..."
    # Validate it's an arm64 ELF before installing — catches the
    # case where someone accidentally stages an x86 build or a
    # corrupted file.
    SO_FILE_TYPE="$(file -b "$STAGED_WHEEL" 2>/dev/null || echo unknown)"
    case "$SO_FILE_TYPE" in
        *aarch64*ELF*|*ELF*aarch64*)
            log "  staged .so is aarch64 ELF — good"
            ;;
        *)
            err "staged GFSK8 .so does not look like aarch64 ELF: $SO_FILE_TYPE
  Either rebuild on a Pi 4 (or another aarch64 host) and re-stage,
  or delete $STAGED_WHEEL to fall back to chroot build."
            ;;
    esac
    cp "$STAGED_WHEEL" "$ROOT_MNT$SO_DEST" \
        || err "could not copy staged GFSK8 .so into image venv"
    ok "GFSK8 .so installed from staged wheel"
else
    log "No staged GFSK8 .so found at $STAGED_WHEEL"
    log "  Falling back to chroot build (clones $GFSK8_REPO @ $GFSK8_COMMIT)"
    log "  Tip: build the .so once on your dev box and copy it to"
    log "       $PROJECT_DIR/wheels/ to skip this ~3-minute step on"
    log "       future builds."

    # IMPORTANT: We delete the tests/ directory before configuring CMake.
    # The repo's CMakeLists.txt unconditionally adds 4 test executables
    # guarded by `if(EXISTS .../tests/test_*.cpp)` — removing those source
    # files is the safest way to opt out without guessing CMake target
    # names. (Trying `cmake --build --target gfsk8` failed with "no rule
    # to make" because the Python module target name in the python/
    # subdirectory's CMakeLists.txt is set via OUTPUT_NAME and may differ
    # from what we'd guess. Letting CMake build everything that REMAINS
    # after we trim sources is robust against future renames.)
    #
    # Each test binary statically links the entire 6+ MB modem library;
    # leaving them in caused linker "No space left on device" errors in
    # the previous build attempt despite the +1GB partition headroom.
    log "Building GFSK8 modem from $GFSK8_REPO @ $GFSK8_COMMIT..."
    run_chroot "rm -rf /tmp/gfsk8-build && mkdir -p /tmp/gfsk8-build" || err "could not prepare GFSK8 build dir"
    run_chroot "git clone --depth 50 '$GFSK8_REPO' /tmp/gfsk8-build/src" \
        || err "could not clone GFSK8 repo"
    run_chroot "cd /tmp/gfsk8-build/src && git checkout '$GFSK8_COMMIT'" \
        || err "could not check out pinned GFSK8 commit"
    # Trim sources we don't need — saves ~250 MB at link time + speeds the build.
    run_chroot "rm -rf /tmp/gfsk8-build/src/tests /tmp/gfsk8-build/src/apps" \
        || err "could not trim GFSK8 test/app sources"
    run_chroot "cd /tmp/gfsk8-build/src && \
        cmake -B build -S . -DCMAKE_BUILD_TYPE=Release \
                           -DBUILD_PYTHON_MODULE=ON \
                           -DPython3_EXECUTABLE=/opt/microjs8/venv/bin/python && \
        cmake --build build -j2" \
        || err "GFSK8 CMake build failed"

    # Locate the produced .so. It's named for the Python ABI tag:
    #   gfsk8.cpython-311-aarch64-linux-gnu.so
    SO_PATH_IN_CHROOT="$(run_chroot "find /tmp/gfsk8-build/src/build/python -name 'gfsk8*.so' -print -quit" 2>/dev/null | tr -d '\r' | tail -1)"
    [ -n "$SO_PATH_IN_CHROOT" ] || err "GFSK8 build did not produce a gfsk8*.so"

    run_chroot "cp '$SO_PATH_IN_CHROOT' /opt/microjs8/venv/lib/python3.11/site-packages/" \
        || err "could not install gfsk8.so into venv"
    # Free ~150 MB of build artifacts so we don't ship them in the final image.
    run_chroot "rm -rf /tmp/gfsk8-build" || warn "could not remove GFSK8 build dir"
    ok "GFSK8 .so installed from chroot build"
fi

# ── GFSK8 smoke test ──────────────────────────────────────────────────────────
# This is the load-bearing safety net. The previous smoke test only
# imported the module and read a constant — that passes even when the
# modulator returns a buffer of all zeros (the May 4 chroot-build
# regression). The new test calls modulate() against a real packed
# message and verifies the audio buffer contains non-zero samples.
#
# If this fails, no .img.xz is produced. Operator either stages a
# known-good wheel in $PROJECT_DIR/wheels/, or investigates the
# chroot build (LTO interaction, pybind11 version, etc).
log "Smoke test: importing gfsk8 + verifying modulate() produces real audio..."
run_chroot "/opt/microjs8/venv/bin/python -c '
import gfsk8
import numpy as np

# 1) Module imports and the constant is sane.
assert gfsk8.RX_SAMPLE_RATE == 12000, (
    f\"unexpected RX_SAMPLE_RATE: {gfsk8.RX_SAMPLE_RATE}\"
)
print(\"  gfsk8 RX rate:\", gfsk8.RX_SAMPLE_RATE)

# 2) pack() returns at least one frame for a trivial message.
frames = gfsk8.pack(\"W5DMH\", \"EN83\", \"SMOKE TEST\", gfsk8.Submode.Normal)
if not frames:
    raise SystemExit(\"pack() returned no frames\")
print(f\"  pack returned {len(frames)} frame(s)\")

# 3) modulate() returns audio with non-zero amplitude. This is the
#    load-bearing check. The chroot build on May 4 produced a wheel
#    that returned a correctly-sized buffer of all zeros — silent
#    TX. Catch that here so the build fails before producing an
#    image.
audio = gfsk8.modulate(
    gfsk8.Submode.Normal, frames[0].frame_type, frames[0].payload, 1500.0,
)
arr = np.array(audio, dtype=np.float32)
if len(arr) < 1000:
    raise SystemExit(f\"modulate produced too few samples: {len(arr)}\")
peak = float(np.max(np.abs(arr)))
if peak < 0.01:
    raise SystemExit(
        f\"modulate produced silent audio (peak={peak:.6f}). \"
        f\"This is the gfsk8 buffer-lifetime UB symptom. The build \"
        f\"environment is producing a broken wheel. Stage a known-good \"
        f\".so at $PROJECT_DIR/wheels/gfsk8.cpython-311-aarch64-linux-gnu.so \"
        f\"to bypass the chroot build, or investigate the build flags.\"
    )
print(f\"  modulate produced {len(arr)} samples, peak amplitude={peak:.3f}\")
print(\"  smoke test: PASS\")
'" || err "GFSK8 smoke test FAILED — wheel does not work"
ok "GFSK8 modem installed and verified"

# Set ownership so the systemd service can stat the install root.
run_chroot "chown -R root:root /opt/microjs8"
run_chroot "chmod -R a+rX     /opt/microjs8"

# ── Default shipped config ────────────────────────────────────────────────────
log "Installing /etc/microjs8/config.toml..."
mkdir -p "$ROOT_MNT/etc/microjs8"
cp "$PROJECT_DIR/etc-defaults/config.toml" "$ROOT_MNT/etc/microjs8/config.toml"
chmod 644 "$ROOT_MNT/etc/microjs8/config.toml"
ok "Default config installed"

# ── gpsd / chrony GPS time discipline ─────────────────────────────────────────
log "Configuring gpsd to read /dev/gps..."
cp "$PROJECT_DIR/etc-defaults/gpsd/gpsd" "$ROOT_MNT/etc/default/gpsd"
chmod 644 "$ROOT_MNT/etc/default/gpsd"
# Enable gpsd at boot. It auto-binds to /dev/gps when the udev symlink
# arrives (either on boot if dongle is present, or on hot-plug).
run_chroot "systemctl enable gpsd.socket gpsd.service" \
    || warn "gpsd unit enable failed (continuing)"
ok "gpsd configured"

log "Configuring chrony to read GPS time from gpsd..."
mkdir -p "$ROOT_MNT/etc/chrony/conf.d"
cp "$PROJECT_DIR/etc-defaults/chrony/microjs8-gps.conf" \
   "$ROOT_MNT/etc/chrony/conf.d/microjs8-gps.conf"
chmod 644 "$ROOT_MNT/etc/chrony/conf.d/microjs8-gps.conf"
# Confirm chrony's main config sources conf.d (Bookworm default does,
# but we double-check so the config is actually applied).
if ! grep -q "include /etc/chrony/conf.d" "$ROOT_MNT/etc/chrony/chrony.conf"; then
    echo "" >> "$ROOT_MNT/etc/chrony/chrony.conf"
    echo "# MicroJS8: include drop-ins" >> "$ROOT_MNT/etc/chrony/chrony.conf"
    echo "include /etc/chrony/conf.d/*.conf" >> "$ROOT_MNT/etc/chrony/chrony.conf"
fi
ok "chrony configured to read GPS time"

# ── Writable runtime directory ────────────────────────────────────────────────
log "Creating writable runtime directory /var/microjs8..."
mkdir -p "$ROOT_MNT/var/microjs8/log"
run_chroot "chown -R microjs8:microjs8 /var/microjs8"
run_chroot "chmod 755 /var/microjs8 /var/microjs8/log"
ok "Runtime directory ready"

# ── udev rules ────────────────────────────────────────────────────────────────
log "Installing udev rules..."
mkdir -p "$ROOT_MNT/etc/udev/rules.d"
cp "$PROJECT_DIR/udev/99-microjs8-qdx.rules" "$ROOT_MNT/etc/udev/rules.d/"
cp "$PROJECT_DIR/udev/99-microjs8-gps.rules" "$ROOT_MNT/etc/udev/rules.d/"
cp "$PROJECT_DIR/udev/99-microjs8-audio.rules" "$ROOT_MNT/etc/udev/rules.d/"
cp "$PROJECT_DIR/udev/99-microjs8-digirig.rules" "$ROOT_MNT/etc/udev/rules.d/"
chmod 644 "$ROOT_MNT/etc/udev/rules.d/99-microjs8-"*.rules
ok "udev rules installed (/dev/qdx, /dev/gps, /dev/digirig, audio)"

# ── polkit rule ───────────────────────────────────────────────────────────────
# Without this, the unprivileged microjs8 service user gets
# "Interactive authentication required" from systemd-logind when it
# tries to invoke `systemctl poweroff` from the both-buttons gesture.
log "Installing polkit rule for microjs8 power-off..."
mkdir -p "$ROOT_MNT/etc/polkit-1/rules.d"
cp "$PROJECT_DIR/polkit/50-microjs8-poweroff.rules" \
   "$ROOT_MNT/etc/polkit-1/rules.d/50-microjs8-poweroff.rules"
chown root:root "$ROOT_MNT/etc/polkit-1/rules.d/50-microjs8-poweroff.rules"
chmod 644 "$ROOT_MNT/etc/polkit-1/rules.d/50-microjs8-poweroff.rules"
ok "polkit rule installed"

# ── systemd unit ──────────────────────────────────────────────────────────────
log "Installing microjs8.service..."
cp "$PROJECT_DIR/systemd/microjs8.service" "$ROOT_MNT/etc/systemd/system/microjs8.service"
chmod 644 "$ROOT_MNT/etc/systemd/system/microjs8.service"
run_chroot "systemctl enable microjs8.service" \
    || err "failed to enable microjs8.service"
ok "microjs8.service enabled (will start on boot)"

# rigctld — runs at boot and via Restart=on-failure for hot-plug recovery.
# The launcher script (microjs8-rigctld-launcher) reads /etc/microjs8/config.toml
# and picks the right rigctld args for the configured radio:
#   - qdx              → TS-480 emulation, ttyACM
#   - xiegu-g90-digirig → G90 driver + RTS-PTT on DigiRig serial port
#   - digirig-rts-only → exits 0 (no rigctld needed; RtsPttService handles PTT)
log "Installing microjs8-rigctld-launcher (multi-radio rigctld picker)..."
mkdir -p "$ROOT_MNT/usr/local/bin"
cp "$PROJECT_DIR/systemd/microjs8-rigctld-launcher" \
   "$ROOT_MNT/usr/local/bin/microjs8-rigctld-launcher"
chmod 755 "$ROOT_MNT/usr/local/bin/microjs8-rigctld-launcher"
ok "microjs8-rigctld-launcher installed"

log "Installing rigctld.service (Step 6 CAT control)..."
cp "$PROJECT_DIR/systemd/rigctld.service" "$ROOT_MNT/etc/systemd/system/rigctld.service"
chmod 644 "$ROOT_MNT/etc/systemd/system/rigctld.service"
run_chroot "systemctl enable rigctld.service" \
    || err "failed to enable rigctld.service"
ok "rigctld.service enabled (launcher picks rigctld args based on configured radio)"

# ── Disable / mask services we don't need ─────────────────────────────────────
log "Disabling unused services..."
for svc in bluetooth hciuart triggerhappy avahi-daemon; do
    run_chroot "systemctl disable $svc 2>/dev/null || true"
done
ok "Unused services disabled"

# ModemManager is special: per the QDX manual, it tries to send Hayes
# AT commands to USB CDC devices including the QDX, which can break
# CAT or even momentarily key PTT. `disable` isn't enough — udev rules
# can re-trigger it on hot-plug. `mask` makes it impossible to start.
#
# We're a single-purpose appliance; we never want ModemManager running.
log "Masking ModemManager (QDX manual recommendation)..."
run_chroot "systemctl mask ModemManager 2>/dev/null || true"
ok "ModemManager masked"

# Disable swap to reduce SD wear (Pi Zero 2W has 512 MB RAM, which is
# enough for our workload — see spec §3.2 / §4.4).
log "Disabling swap..."
run_chroot "systemctl disable dphys-swapfile 2>/dev/null || true"
run_chroot "apt-get remove -y dphys-swapfile 2>/dev/null || true"
ok "Swap disabled"

# ── APT cleanup ───────────────────────────────────────────────────────────────
log "Cleaning APT cache..."
run_chroot "apt-get clean"
run_chroot "rm -rf /var/lib/apt/lists/*"
ok "APT cache cleaned"

# Restore resolv.conf
mv "$ROOT_MNT/etc/resolv.conf.bak" "$ROOT_MNT/etc/resolv.conf" 2>/dev/null || \
    echo "nameserver 1.1.1.1" > "$ROOT_MNT/etc/resolv.conf"

# ── Version stamp ─────────────────────────────────────────────────────────────
APP_VER=$(grep -E '^__version__' "$PROJECT_DIR/src/microjs8/version.py" | \
    sed -E 's/.*"([^"]+)".*/\1/')
echo "MicroJS8 ${APP_VER} built ${DATE_STR}" > "$ROOT_MNT/etc/microjs8-version"
chmod 644 "$ROOT_MNT/etc/microjs8-version"
log "Version stamp: MicroJS8 ${APP_VER} (${DATE_STR})"

# ── Unmount ───────────────────────────────────────────────────────────────────
log "Unmounting image..."
umount "$ROOT_MNT/boot/firmware" 2>/dev/null || true
for dir in dev/pts dev proc sys run; do
    umount "$ROOT_MNT/$dir" 2>/dev/null || true
done
umount "$BOOT_MNT" 2>/dev/null || true
sync
umount "$ROOT_MNT"
sync
losetup -d "$LOOP_BOOT" 2>/dev/null || true
losetup -d "$LOOP_ROOT" 2>/dev/null || true
ok "Image unmounted cleanly"

trap - EXIT  # manual cleanup done

# ── Finalise ──────────────────────────────────────────────────────────────────
log "Finalising image..."
mv "$WORK_IMG" "$OUTPUT_IMG"

log "Generating SHA-256 checksum..."
sha256sum "$OUTPUT_IMG" > "${OUTPUT_IMG}.sha256"
ok "Checksum: $(awk '{print $1}' "${OUTPUT_IMG}.sha256")"

log "Compressing with xz (this takes 5-15 minutes)..."
xz -T0 -v "$OUTPUT_IMG"
sha256sum "${OUTPUT_IMG}.xz" > "${OUTPUT_IMG}.xz.sha256"
ok "Compressed: ${OUTPUT_IMG}.xz ($(du -h "${OUTPUT_IMG}.xz" | cut -f1))"

log "=================================================="
log " MicroJS8 Image Build Complete"
log " $(date)"
log "=================================================="
log ""
log " Output:    ${OUTPUT_IMG}.xz"
log " Checksum:  ${OUTPUT_IMG}.xz.sha256"
log " Log:       $LOG"
log ""
log " Flash:"
log "   xz -dk ${OUTPUT_IMG}.xz"
log "   sudo dd if=${OUTPUT_IMG} of=/dev/sdX bs=4M status=progress conv=fsync"
log "   (or use Raspberry Pi Imager — select the .xz directly)"
log ""
log " Default SSH credentials:"
log "   ssh pi@microjs8.local"
log "   Password: microjs8setup"
log "   Change with: passwd"
log ""
log " Verify daemon running after first boot:"
log "   sudo systemctl status microjs8"
log "   sudo journalctl -u microjs8 -b"
log "=================================================="
