"""Tests for microjs8.power.backlight.

The Backlight class points at a sysfs directory; tests use ``tmp_path``
to give it a fake directory containing the two files it expects.
That keeps the test suite host-runnable on any Linux/macOS machine
without needing the actual hardware sysfs node.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from microjs8.power.backlight import Backlight


@pytest.fixture
def fake_sysfs(tmp_path: Path) -> Path:
    """Build a directory mimicking /sys/class/backlight/backlight/.

    Initial state: brightness=128, max_brightness=255 (typical for an
    8-bit PWM backlight controller). Tests can override by writing to
    the files directly.
    """
    (tmp_path / "brightness").write_text("128")
    (tmp_path / "max_brightness").write_text("255")
    return tmp_path


# ── Toggle behaviour ─────────────────────────────────────────────────


def test_toggle_from_on_writes_zero_and_caches_previous(fake_sysfs: Path):
    """First toggle from a lit state must write 0 and remember the value."""
    bl = Backlight(fake_sysfs)
    assert bl.is_on()

    bl.toggle()
    assert (fake_sysfs / "brightness").read_text() == "0"
    assert not bl.is_on()


def test_toggle_off_then_on_restores_previous_brightness(fake_sysfs: Path):
    """Operator toggles off, then back on — must restore the cached value."""
    bl = Backlight(fake_sysfs)
    bl.toggle()                                          # 128 -> 0
    bl.toggle()                                          # 0 -> 128 (restored)
    assert (fake_sysfs / "brightness").read_text() == "128"
    assert bl.is_on()


def test_toggle_on_with_no_cache_goes_to_max(fake_sysfs: Path):
    """If the daemon starts with the backlight off, the first 'on' toggle
    has no cached previous-brightness value — must go to max_brightness.
    """
    (fake_sysfs / "brightness").write_text("0")
    bl = Backlight(fake_sysfs)
    assert not bl.is_on()

    bl.toggle()
    assert (fake_sysfs / "brightness").read_text() == "255"
    assert bl.is_on()


def test_three_toggles_cycle_correctly(fake_sysfs: Path):
    """on -> off -> on -> off ends at 0 with 128 cached again."""
    bl = Backlight(fake_sysfs)
    bl.toggle()    # 128 -> 0
    bl.toggle()    # 0 -> 128
    bl.toggle()    # 128 -> 0
    assert (fake_sysfs / "brightness").read_text() == "0"


# ── Robustness ───────────────────────────────────────────────────────


def test_missing_brightness_file_does_not_crash(tmp_path: Path):
    """A missing sysfs node must not crash the daemon — the toggle is a
    best-effort operation and the backlight's job is decidedly less
    important than keeping the radio path alive.
    """
    # max_brightness exists but brightness does not
    (tmp_path / "max_brightness").write_text("255")
    bl = Backlight(tmp_path)
    # Both calls must complete without raising.
    bl.toggle()
    assert not bl.is_on()    # default-on-error returns 0/False


def test_missing_max_brightness_uses_safe_default(tmp_path: Path):
    """If max_brightness is missing too, the constructor must not raise.

    The default falls back to 255, which is the standard 8-bit PWM
    range. Worst case: the operator's "on" target is a slightly-wrong
    integer the kernel will clamp; the daemon stays alive.
    """
    (tmp_path / "brightness").write_text("0")
    bl = Backlight(tmp_path)
    bl.toggle()    # 0 -> 255 (the default fallback max)
    assert (fake_brightness_value(tmp_path)) == 255


def test_garbage_brightness_value_treated_as_unknown(tmp_path: Path):
    """A non-integer brightness file must be handled gracefully (the
    kernel should never produce one, but defensive parsing protects
    us from corrupt sysfs reads or hardware glitches).
    """
    (tmp_path / "brightness").write_text("not-a-number\n")
    (tmp_path / "max_brightness").write_text("255")
    bl = Backlight(tmp_path)
    # is_on() returns False on parse failure (default).
    assert not bl.is_on()
    # toggle() reads -1 sentinel, logs, and returns without writing.
    bl.toggle()
    # Brightness file is unchanged.
    assert (tmp_path / "brightness").read_text() == "not-a-number\n"


# ── Helper ───────────────────────────────────────────────────────────


def fake_brightness_value(path: Path) -> int:
    return int((path / "brightness").read_text().strip())
