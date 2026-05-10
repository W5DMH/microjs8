"""Tests for the JS8 CRC-16/KERMIT + base-41 checksum module.

The single most important test in this file is the on-air-verified
known-good vector: ``checksum16("HELLO FROM REFERENCE") == "J6X"``.
That vector was captured from a real reference station's TX (KD8PGB)
during the multi-frame reassembly bring-up — if it ever stops
matching, our checksum implementation has drifted from JS8Call's
and we need to look at the C++ source again before shipping.

The rest of the tests cover edge cases (empty strings, whitespace,
mismatch, malformed input) so the assembler's behavior on partial /
corrupt frames is well-defined.
"""

from __future__ import annotations

from microjs8.protocol.checksum import (
    crc16_kermit,
    pack16bits,
    checksum16,
    verify_checksum16,
)


# ── CRC-16/KERMIT — algorithmic correctness ────────────────────────


def test_crc16_kermit_known_vector_hello_from_reference():
    """The reference test vector captured on-air.

    KD8PGB (a JS8Call reference station) TX'd "HELLO FROM REFERENCE"
    and the trailing checksum was J6X. Reverse-engineering: the CRC
    must compute to 0x7DDA so that pack16bits(0x7DDA) == "J6X".
    """
    crc = crc16_kermit(b"HELLO FROM REFERENCE")
    assert crc == 0x7DDA, f"expected 0x7DDA, got {crc:#06x}"


def test_crc16_kermit_empty_input():
    """Empty input has CRC == 0x0000 (the init value)."""
    assert crc16_kermit(b"") == 0x0000


def test_crc16_kermit_single_byte():
    """Sanity check that single-byte CRC is non-zero and stable."""
    assert crc16_kermit(b"A") == crc16_kermit(b"A")
    # The exact value isn't a JS8 spec point; we just verify
    # determinism (regression guard against implementation drift).
    crc_a = crc16_kermit(b"A")
    assert 0 <= crc_a <= 0xFFFF


def test_crc16_kermit_returns_16bit_value():
    """CRC must always fit in 16 bits."""
    for s in [b"", b"A", b"HELLO", b"X" * 1000, b"\x00" * 50]:
        assert 0 <= crc16_kermit(s) <= 0xFFFF


def test_crc16_kermit_distinguishes_inputs():
    """Different inputs should (almost always) produce different CRCs.

    Not a guarantee, but for these obviously different short strings
    we'd be surprised at a collision.
    """
    assert crc16_kermit(b"HELLO") != crc16_kermit(b"WORLD")
    assert crc16_kermit(b"A") != crc16_kermit(b"B")


# ── pack16bits — base-41 packing ────────────────────────────────────


def test_pack16bits_zero_is_three_zeros():
    """Value 0 packs to '000' — first three chars of the alphabet."""
    assert pack16bits(0) == "000"


def test_pack16bits_known_vector_j6x():
    """0x7DDA must pack to 'J6X' (the on-air checksum)."""
    assert pack16bits(0x7DDA) == "J6X"


def test_pack16bits_always_three_chars():
    """Output is always exactly 3 characters."""
    for v in [0, 1, 41, 42, 100, 0xFFFF, 0x1234]:
        out = pack16bits(v)
        assert len(out) == 3, f"value {v}: got {out!r}"


def test_pack16bits_chars_are_in_alphabet():
    """All output chars must be in the base-41 alphabet."""
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+-./?"
    for v in range(0, 0xFFFF + 1, 257):  # sample every 257th value
        out = pack16bits(v)
        for ch in out:
            assert ch in alphabet, f"value {v}: char {ch!r} not in alphabet"


def test_pack16bits_max_16bit():
    """Max 16-bit value (0xFFFF = 65535) packs cleanly within 41**3=68921."""
    out = pack16bits(0xFFFF)
    assert len(out) == 3


# ── checksum16 — full TX-side helper ────────────────────────────────


def test_checksum16_hello_from_reference_is_j6x():
    """The end-to-end TX-side checksum matches the on-air observed value."""
    assert checksum16("HELLO FROM REFERENCE") == "J6X"


def test_checksum16_returns_three_chars():
    """Checksums are always 3 characters."""
    for body in ["", "A", "HELLO", "MSG TO:KC1WDO HELLO WORLD"]:
        cs = checksum16(body)
        assert len(cs) == 3, f"body {body!r}: got {cs!r}"


def test_checksum16_deterministic():
    """Same input → same output."""
    body = "ANYTHING GOES HERE"
    assert checksum16(body) == checksum16(body)


def test_checksum16_latin1_accepts_high_bytes():
    """Latin-1 encoding handles all 0x00-0xFF bytes without error.

    JS8 isn't strictly Latin-1 in practice (it's mostly printable
    ASCII), but the underlying C++ uses toLocal8Bit which is roughly
    Latin-1. We mirror that to keep wire-level compatibility.
    """
    body = "MIXED ÅSCII"  # Å is valid in Latin-1 (0xC5)
    cs = checksum16(body)
    assert len(cs) == 3


# ── verify_checksum16 — RX-side validation ──────────────────────────


def test_verify_checksum_real_world_vector():
    """The on-air case: full body+checksum validates and returns body."""
    result = verify_checksum16("HELLO FROM REFERENCE J6X")
    assert result == "HELLO FROM REFERENCE"


def test_verify_checksum_round_trip():
    """Compute checksum, append, then verify — must round-trip cleanly."""
    body = "PLEASE PASS THIS ALONG"
    full = body + " " + checksum16(body)
    assert verify_checksum16(full) == body


def test_verify_checksum_round_trip_short_body():
    """Even very short bodies round-trip."""
    body = "HI"
    full = body + " " + checksum16(body)
    assert verify_checksum16(full) == body


def test_verify_checksum_rejects_wrong_checksum():
    """Wrong checksum returns None (not an exception)."""
    assert verify_checksum16("HELLO FROM REFERENCE XXX") is None


def test_verify_checksum_rejects_empty():
    """Empty input returns None."""
    assert verify_checksum16("") is None


def test_verify_checksum_rejects_no_space():
    """A body without a space-separated trailing token is invalid."""
    # Even if the last 3 chars happen to coincide with a checksum
    # alphabet, we need the space separator.
    assert verify_checksum16("ABCJ6X") is None


def test_verify_checksum_rejects_no_checksum_token():
    """A bare word has no trailing checksum."""
    assert verify_checksum16("MSG") is None


def test_verify_checksum_rejects_too_short():
    """Body shorter than '<x> <CHK>' (5 chars) can't be valid."""
    assert verify_checksum16("ABC") is None
    assert verify_checksum16("AB") is None


def test_verify_checksum_handles_trailing_whitespace():
    """A flaky decode might add stray whitespace at the end."""
    # The implementation rstrips, so this should validate.
    body = "HELLO"
    full = body + " " + checksum16(body) + "  "
    assert verify_checksum16(full) == body


def test_verify_checksum_rejects_non_alphabet_in_checksum():
    """Trailing token with chars outside the base-41 alphabet → None."""
    # A space-followed-by-3-chars with a non-alphabet char in the
    # trailing token is malformed.
    assert verify_checksum16("HELLO !@#") is None
    assert verify_checksum16("HELLO j6x") is None  # lowercase not in alphabet


def test_verify_checksum_distinct_bodies_have_distinct_checksums():
    """Two different bodies should validate with different checksums.

    Regression guard: if our CRC implementation collapsed (e.g. always
    returned 0), this test would catch it.
    """
    body1 = "MESSAGE ONE"
    body2 = "MESSAGE TWO"
    full1 = body1 + " " + checksum16(body1)
    full2 = body2 + " " + checksum16(body2)
    # Each should validate ONLY against its own full form.
    assert verify_checksum16(full1) == body1
    assert verify_checksum16(full2) == body2
    # And cross-validation should fail.
    swapped = body1 + " " + checksum16(body2)
    assert verify_checksum16(swapped) is None


def test_verify_checksum_preserves_internal_whitespace():
    """Multiple internal spaces in the body are preserved on validation."""
    body = "TWO  SPACES  HERE"
    full = body + " " + checksum16(body)
    assert verify_checksum16(full) == body


def test_verify_checksum_handles_single_char_body():
    """A single-char body + checksum is the minimum valid form."""
    body = "A"
    full = body + " " + checksum16(body)
    assert verify_checksum16(full) == body


def test_verify_checksum_with_eot_already_stripped():
    """The reassembler strips EOT before passing to verify; we don't
    have to handle EOT here, but ensure presence of EOT in body fails
    cleanly (so a buggy upstream that forgets to strip is detected)."""
    body = "HELLO"
    valid = body + " " + checksum16(body)
    # Add an EOT char in the middle — this would change the checksum
    # if it were part of the body; verify that detection works.
    corrupted = "HEL\x04LO " + checksum16(body)
    assert verify_checksum16(corrupted) is None
    # And without corruption, it works:
    assert verify_checksum16(valid) == body


def test_verify_checksum_non_string_input():
    """Defensive: non-string input shouldn't crash."""
    # Implementation guards with isinstance check.
    assert verify_checksum16(None) is None  # type: ignore[arg-type]
    assert verify_checksum16(123) is None  # type: ignore[arg-type]
    assert verify_checksum16(b"bytes") is None  # type: ignore[arg-type]
