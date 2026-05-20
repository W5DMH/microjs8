#!/bin/sh
# microjs8-enable-display — enable the SPI bus for the userspace
# ST7789V display driver shipped with microjs8 v0.0.6+.
#
# Phase 18 rewrite: drops the broken fbtft dtoverlay (which doesn't
# exist as a standalone .dtbo in stock Raspberry Pi OS Bookworm).
# The new path uses microjs8's own SpiDisplayDevice — a userspace
# SPI driver that needs nothing more than the kernel SPI bus.
#
# What this script does:
#   - Adds 'dtparam=spi=on' to /boot/firmware/config.txt if not
#     already present (idempotent)
#   - Wraps the addition in a BEGIN/END marker block so --revert
#     can remove ONLY what we added, preserving operator hand-edits
#   - Backs up config.txt with a timestamped copy before any edit
#
# What this script does NOT do (vs the Phase 17 version):
#   - No dtoverlay= line. The kernel fbtft driver path is dead on
#     modern Bookworm; we drive the panel from userspace via
#     /dev/spidev0.0 instead.
#
# Usage:
#   sudo microjs8-enable-display          # interactive
#   sudo microjs8-enable-display --yes    # non-interactive
#   sudo microjs8-enable-display --revert # remove what we added

set -eu

CONFIG_PATH="/boot/firmware/config.txt"
LEGACY_PATH="/boot/config.txt"   # pre-Bookworm

# ── Locate the config file ─────────────────────────────────────────
if [ -f "$CONFIG_PATH" ]; then
    CONFIG="$CONFIG_PATH"
elif [ -f "$LEGACY_PATH" ]; then
    CONFIG="$LEGACY_PATH"
else
    echo "error: cannot find Pi boot config.txt at $CONFIG_PATH or $LEGACY_PATH" >&2
    echo "are you running this on a Raspberry Pi?" >&2
    exit 1
fi

# ── Sanity check: is this actually a Pi boot config? ───────────────
if ! grep -qE '^\[(pi|all)|^dtparam=|^dtoverlay=|^arm_64bit=' "$CONFIG"; then
    echo "error: $CONFIG doesn't look like a Pi boot config" >&2
    exit 1
fi

# ── Parse args ─────────────────────────────────────────────────────
NONINTERACTIVE=0
REVERT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) NONINTERACTIVE=1; shift ;;
        --revert) REVERT=1; shift ;;
        --help|-h)
            sed -n '2,/^#$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            echo "usage: $0 [--yes] [--revert]" >&2
            exit 1
            ;;
    esac
done

# ── Need root ──────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (try: sudo $0)" >&2
    exit 1
fi

# ── The block we add ───────────────────────────────────────────────
BLOCK_START="# >>> microjs8-enable-display BEGIN (do not edit between markers)"
BLOCK_END="# <<< microjs8-enable-display END"

read_only_block() {
    cat << 'BLOCK_EOF'
# >>> microjs8-enable-display BEGIN (do not edit between markers)
# Enable SPI bus for microjs8's userspace ST7789V driver.
# The daemon's SpiDisplayDevice opens /dev/spidev0.0 and drives the
# panel directly — no kernel framebuffer driver required.
dtparam=spi=on
# <<< microjs8-enable-display END
BLOCK_EOF
}

# ── Revert path ────────────────────────────────────────────────────
if [ "$REVERT" -eq 1 ]; then
    if ! grep -qF "$BLOCK_START" "$CONFIG"; then
        echo "no microjs8-enable-display block found in $CONFIG — nothing to revert"
        exit 0
    fi
    backup="${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p "$CONFIG" "$backup"
    echo "backed up to $backup"
    # Delete from BEGIN marker through END marker, inclusive.
    sed -i "/$(echo "$BLOCK_START" | sed 's:[][\/.^$*]:\\&:g')/,/$(echo "$BLOCK_END" | sed 's:[][\/.^$*]:\\&:g')/d" "$CONFIG"
    echo "removed microjs8 display block from $CONFIG"
    echo "reboot required for the change to take effect: sudo reboot"
    exit 0
fi

# ── Idempotency check ──────────────────────────────────────────────
if grep -qF "$BLOCK_START" "$CONFIG"; then
    echo "microjs8-enable-display block already present in $CONFIG — nothing to do"
    echo "to re-apply with current defaults: sudo $0 --revert && sudo $0"
    exit 0
fi

# ── Preview the change ─────────────────────────────────────────────
echo
echo "About to append the following block to $CONFIG:"
echo
read_only_block | sed 's/^/  /'
echo

if [ "$NONINTERACTIVE" -ne 1 ]; then
    printf "Proceed? [y/N] "
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *)
            echo "aborted; no changes made."
            exit 1
            ;;
    esac
fi

# ── Apply ──────────────────────────────────────────────────────────
backup="${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
cp -p "$CONFIG" "$backup"
echo "backed up to $backup"

if [ -n "$(tail -c1 "$CONFIG")" ]; then
    printf '\n' >> "$CONFIG"
fi
read_only_block >> "$CONFIG"
echo "added microjs8 display block to $CONFIG"
echo
echo "Reboot required for SPI to become active: sudo reboot"
echo
echo "After reboot, verify with:"
echo "  ls /dev/spidev*                              # expect spidev0.0"
echo "  sudo systemctl restart microjs8.service       # daemon will use SPI driver"
echo "  sudo journalctl -u microjs8.service -f | grep -i SpiDisplayDevice"
