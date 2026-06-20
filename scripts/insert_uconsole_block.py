"""Surgical inserter for v0.0.17 postinst uConsole detection block.

Usage:
    cd ~/microjs8
    python3 /tmp/insert_uconsole_block.py

Pre-requisites:
    - The uConsole block content must be at
      /tmp/postinst_uconsole_block.sh (from
      v0017-postinst_uconsole_block.sh in the patch zip).
    - You must run from the ~/microjs8 repo root.

What it does:
    Reads packaging/postinst.sh, finds the section header containing
    "7.5. gpsd configuration", and inserts the uConsole detection
    block just before it (placing it as section 7.4d, after the v0.0.16
    I2C auto-enable at 7.4b and smbus2 install at 7.4c).

Idempotent:
    If the uConsole block is already present, prints a status message
    and exits 0 without modifying the file.

Robustness:
    Matches the section header by content rather than exact dash
    characters, so it works regardless of decoration style.
"""

from pathlib import Path

POSTINST = Path("packaging/postinst.sh")
BLOCK_SOURCE = Path("/tmp/postinst_uconsole_block.sh")
MARKER_TEXT = "7.5. gpsd configuration"
IDEMPOTENCY_TAG = "7.4d. uConsole detection"


def main() -> int:
    if not POSTINST.exists():
        print(
            f"ERROR: {POSTINST} not found. Run from the ~/microjs8 "
            "repo root (the working directory must contain the "
            "'packaging' subdirectory)."
        )
        return 1

    if not BLOCK_SOURCE.exists():
        print(
            f"ERROR: uConsole block source not found at {BLOCK_SOURCE}. "
            "Copy v0017-postinst_uconsole_block.sh from the patch zip "
            f"to {BLOCK_SOURCE} before running this script."
        )
        return 1

    src = POSTINST.read_text()

    if IDEMPOTENCY_TAG in src:
        print(
            "uConsole block already present in postinst.sh (found "
            f"{IDEMPOTENCY_TAG!r}); nothing to do."
        )
        return 0

    # Find the section header line containing "7.5. gpsd configuration".
    # Robust against em-dash vs box-drawing vs ASCII variants.
    lines = src.splitlines(keepends=True)
    insert_index = None
    for i, line in enumerate(lines):
        if MARKER_TEXT in line and "#" in line:
            insert_index = i
            break

    if insert_index is None:
        print(
            f"ERROR: no comment line found containing {MARKER_TEXT!r}; "
            "the postinst.sh structure may have changed since v0.0.16."
        )
        return 1

    block = BLOCK_SOURCE.read_text()
    block = block.rstrip("\n") + "\n\n"

    lines.insert(insert_index, block)
    POSTINST.write_text("".join(lines))
    print(
        f"OK: inserted uConsole block before line {insert_index + 1} of "
        f"{POSTINST} (just before section 7.5)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
