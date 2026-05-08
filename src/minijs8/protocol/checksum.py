"""JS8 protocol checksum validation.

JS8Call appends a 3-character base-41-packed CRC-16/KERMIT checksum
to the body of every "buffered" directed command (MSG, MSG TO:,
QUERY MSGS, QUERY CALL, QUERY, relay, CMD — all except GRID and
@APRSIS-targeted messages, which skip the checksum). The receiving
station validates the checksum to confirm the multi-frame message
arrived intact, and only then auto-ACKs.

We do NOT need to *generate* the checksum — gfsk8.pack() does that
automatically (verified in gfsk8/src/Varicode.cpp's buildMessageFrames).
This module provides validation-only helpers used by the receive-
side reassembly layer.

Algorithm details (from gfsk8/src/Varicode.cpp, identical to upstream
JS8Call's varicode.cpp):

- CRC-16/KERMIT (a.k.a. CRC-16/CCITT-KERMIT)
  - polynomial:    0x1021
  - initial value: 0x0000
  - reflect input:  true
  - reflect output: true
  - XOR output:    0x0000

- The 16-bit CRC is packed via base-41 into a 3-character string from
  the alphabet "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+-./?". Three
  base-41 characters can encode up to 41**3 = 68,921 values, which is
  more than enough for the 16-bit (65,536-value) range.

- The 3-char checksum is appended to the body with a leading space:
      "<body> <checksum>"

Verification (computed in this session against W5DMH's actual on-air
TX from KD8PGB):
- body:     "HELLO FROM REFERENCE"
- expected: "J6X"
- computed: "J6X"  (matches via this module's checksum16)

We implement CRC-16/KERMIT directly rather than relying on crcmod
(or python-crc) because:

1. Zero external dependencies on the Pi (one less wheel to keep
   pinned through pip resolver upgrades).
2. The implementation is ~15 lines of obvious code; no opaque
   table-lookup matters when each input is at most a few dozen
   characters.
3. We can write tests with known-good vectors directly.

Performance is not a concern — each call processes <200 bytes and
runs at most a few times per slot.
"""

from __future__ import annotations

from typing import Optional


# Base-41 alphabet from gfsk8/JS8Call. Order matters — the digit
# character set 0-9, then A-Z, then "+-./?" — matches Varicode::pack16bits.
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+-./?"
_NALPHABET = 41

# CRC-16/KERMIT lookup: precompute the per-byte transition table
# at module load. Reflected polynomial of 0x1021 → 0x8408. Computed
# once; reused for every checksum.
_CRC16_KERMIT_POLY_REFLECTED = 0x8408


def _build_crc16_kermit_table() -> tuple[int, ...]:
    """Build the CRC-16/KERMIT lookup table (256 entries, one per byte)."""
    table: list[int] = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ _CRC16_KERMIT_POLY_REFLECTED
            else:
                crc = crc >> 1
        table.append(crc)
    return tuple(table)


_CRC16_KERMIT_TABLE = _build_crc16_kermit_table()


def crc16_kermit(data: bytes) -> int:
    """Compute CRC-16/KERMIT of ``data``.

    Reference vector (from gfsk8 + verified on-air against KD8PGB's
    TX of "HELLO FROM REFERENCE"):

        crc16_kermit(b"HELLO FROM REFERENCE") == 0x7DDA
        pack16bits(0x7DDA) == "J6X"  (the on-air checksum we observed)

    Returns the 16-bit CRC value (0–65535).
    """
    crc = 0x0000
    for byte in data:
        crc = (crc >> 8) ^ _CRC16_KERMIT_TABLE[(crc ^ byte) & 0xFF]
    return crc & 0xFFFF


def pack16bits(value: int) -> str:
    """Pack a 16-bit value into the 3-char base-41 representation.

    Mirrors Varicode::pack16bits in gfsk8/JS8Call exactly. The output
    is always 3 characters, even for small values (which use leading
    "0" digits in the base-41 alphabet).
    """
    if not (0 <= value <= 0xFFFF):
        # The caller passed something that can't fit in 16 bits.
        # Truncate rather than raise — JS8Call's implementation does
        # the same (it would silently overflow on the C side too).
        value = value & 0xFFFF

    out = []
    tmp = value // (_NALPHABET * _NALPHABET)
    out.append(_ALPHABET[tmp])
    tmp = (value - (tmp * (_NALPHABET * _NALPHABET))) // _NALPHABET
    out.append(_ALPHABET[tmp])
    tmp = value % _NALPHABET
    out.append(_ALPHABET[tmp])
    return "".join(out)


def checksum16(body: str) -> str:
    """Compute the 3-char CRC-16/KERMIT checksum for ``body``.

    Provided for symmetry / testing — we don't compute checksums for
    transmission (gfsk8 does that). This function exists so we can
    write known-good test vectors and so it's available if a future
    feature ever needs to validate a generated checksum (e.g. an
    integration test that round-trips through the protocol layer).

    The body is encoded as Latin-1 to match the C++ implementation
    (toLocal8Bit on the Qt side), which preserves all 0x00-0xFF
    byte values and matches the behavior for the printable-ASCII
    bodies used in JS8.
    """
    crc = crc16_kermit(body.encode("latin-1"))
    return pack16bits(crc)


def verify_checksum16(body_with_checksum: str) -> Optional[str]:
    """Validate the trailing 3-char CRC-16/KERMIT checksum.

    Given ``"<body> <CHK>"`` where ``<CHK>`` is the trailing 3-char
    checksum (with one space separator), returns the body without
    the checksum suffix on success, or None on mismatch / malformed
    input.

    Edge cases handled:

    - Whitespace at the very end of the line (a partial trailing
      char from a flaky decode) is rstripped before parsing.
    - A body shorter than 4 characters (3 checksum chars + 1 space)
      is automatically a mismatch — we return None rather than
      raising, so the assembler can keep waiting for more frames.
    - A body that doesn't contain a space-separated final token is
      a mismatch — same behavior.
    - All checksum chars must be in the base-41 alphabet; non-
      alphabet chars in the trailing token mean the message hasn't
      finished arriving yet (or the decode is corrupt).
    """
    if not isinstance(body_with_checksum, str):
        return None

    # Trim any trailing whitespace artifacts from the decode (rare,
    # but cheap to guard against).
    text = body_with_checksum.rstrip()

    # The minimum valid form is "X CHK" — 5 chars including the
    # space and 3-char checksum. Anything shorter can't be valid.
    if len(text) < 5:
        return None

    # The checksum is the last 3 characters, preceded by exactly one
    # space. Find that space; if there isn't one, no match.
    if text[-4] != " ":
        return None

    checksum = text[-3:]
    body = text[:-4]

    # Defensive: the checksum chars must all be in the base-41
    # alphabet. If not, the message is mid-flight (a partial
    # checksum char hasn't arrived yet) or corrupt.
    for ch in checksum:
        if ch not in _ALPHABET:
            return None

    # Recompute and compare. A mismatch returns None — the assembler
    # treats this as "still arriving" and waits for more frames.
    expected = checksum16(body)
    if expected != checksum:
        return None
    return body
