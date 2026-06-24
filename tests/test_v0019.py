"""Tests for v0.0.19 features.

Covers:
  - MYLOC compose action: with-fix path (auto-fills text + switches
    to MSG) and no-fix path (sets warning flag, stays on MYLOC).
  - DIRECTED-screen aging color helpers (_age_color, _age_color_tx).
  - DirectedActivityLog default cap raised to 2000.

ASCII-only policy enforced per the v0.0.14 paste-encoding incident.
"""

from __future__ import annotations

import time
import unittest

from microjs8.activity import DEFAULT_MAX_ENTRIES, DirectedActivityLog
from microjs8.gps.types import FixKind, GpsFix, no_fix
from microjs8.ui.screens import _age_color, _age_color_tx
from microjs8.ui.state import (
    ComposeCmd,
    Screen,
    UIState,
)
from microjs8.ui import theme


# -- Activity cap ------------------------------------------------------


class TestActivityDefaultCap(unittest.TestCase):
    def test_default_cap_is_2000(self) -> None:
        # v0.0.19: raised from 200 so a long session keeps DIRECTED
        # rows visible until the session ends (per operator request).
        self.assertEqual(DEFAULT_MAX_ENTRIES, 2000)
        log = DirectedActivityLog()
        self.assertEqual(log.max_entries, 2000)


# -- DIRECTED-screen age color helpers ---------------------------------


class TestAgeColorReceived(unittest.TestCase):
    """Received-row palette: green / yellow / gray."""

    def test_fresh_under_30_min_is_green(self) -> None:
        self.assertEqual(_age_color(0), theme.FG_GOOD)
        self.assertEqual(_age_color(29 * 60), theme.FG_GOOD)

    def test_30_min_to_4_h_is_yellow(self) -> None:
        self.assertEqual(_age_color(30 * 60), theme.FG_WARN)
        self.assertEqual(_age_color(2 * 3600), theme.FG_WARN)
        self.assertEqual(_age_color(4 * 3600 - 1), theme.FG_WARN)

    def test_over_4_h_is_gray(self) -> None:
        self.assertEqual(_age_color(4 * 3600), theme.FG_DIM)
        self.assertEqual(_age_color(24 * 3600), theme.FG_DIM)


class TestAgeColorTransmitted(unittest.TestCase):
    """v0.0.19: transmitted-row palette: red / orange / blue."""

    def test_fresh_under_30_min_is_red(self) -> None:
        self.assertEqual(_age_color_tx(0), theme.FG_BAD)
        self.assertEqual(_age_color_tx(29 * 60), theme.FG_BAD)

    def test_30_min_to_4_h_is_orange(self) -> None:
        # Distinct from FG_WARN (yellow) used for received-row middle.
        self.assertEqual(_age_color_tx(30 * 60), theme.FG_TX_AGING)
        self.assertEqual(_age_color_tx(2 * 3600), theme.FG_TX_AGING)
        self.assertEqual(_age_color_tx(4 * 3600 - 1), theme.FG_TX_AGING)

    def test_over_4_h_is_blue(self) -> None:
        # ACCENT (60,160,220) is the existing focused-field blue;
        # reused here so transmitted rows fade to a visually distant
        # color from any active conversation.
        self.assertEqual(_age_color_tx(4 * 3600), theme.ACCENT)
        self.assertEqual(_age_color_tx(24 * 3600), theme.ACCENT)

    def test_palettes_are_distinct_at_middle_tier(self) -> None:
        # Visual safety: a yellow received-row and an orange
        # transmitted-row must NOT use the same RGB triple.
        self.assertNotEqual(
            _age_color(60 * 60),       # received yellow
            _age_color_tx(60 * 60),    # transmitted orange
        )


# -- MYLOC compose action ----------------------------------------------


def _make_state_with_fix(
    *, lat: float, lon: float, kind: FixKind = FixKind.FIX_3D,
) -> UIState:
    """Construct a UIState with a populated GPS fix for testing.

    UIState requires callsign/grid/tx_allowed as positional args.
    Using realistic non-empty values so we exercise the normal
    (configured station) code paths rather than the unconfigured-
    bypass path -- the MYLOC logic doesn't depend on these but
    we keep them consistent across the test suite.
    """
    state = UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)
    fix = GpsFix(
        kind=kind,
        lat=lat, lon=lon, altitude_m=200.0,
        speed_mps=0.0, track_deg=0.0, hdop=1.0,
        fix_time=time.time(), satellites_used=8,
        received_at=time.monotonic(),
    )
    state.set_gps(fix)
    return state


def _make_state_no_fix() -> UIState:
    """Construct a UIState with no GPS fix (the default no_fix sentinel).

    Same callsign/grid/tx_allowed contract as _make_state_with_fix.
    """
    return UIState(callsign="W5DMH", grid="EN83", tx_allowed=True)


def _cycle_to_myloc(state: UIState) -> None:
    """Cycle the CMD dropdown until it would land on MYLOC.

    MYLOC is the LAST entry in COMPOSE_CMD_ORDER; cycling backward
    one step from FREE (the default starting CMD) lands on it.
    """
    state.compose_cycle_cmd(forward=False)


class TestMylocWithFix(unittest.TestCase):
    """v0.0.19: cycling to MYLOC with a 3D fix fills text + switches to MSG."""

    def test_with_fix_text_is_prefilled(self) -> None:
        state = _make_state_with_fix(
            lat=43.279250516, lon=-83.338943893,
        )
        _cycle_to_myloc(state)
        snap = state.snapshot()
        # 4-decimal-place format per v0.0.19 spec (~10 m precision).
        self.assertEqual(snap.compose_text, "MYLOC 43.2793,-83.3389")

    def test_with_fix_cmd_becomes_msg(self) -> None:
        state = _make_state_with_fix(lat=43.279, lon=-83.339)
        _cycle_to_myloc(state)
        snap = state.snapshot()
        self.assertIs(snap.compose_cmd, ComposeCmd.MSG)

    def test_with_fix_no_warning(self) -> None:
        state = _make_state_with_fix(lat=43.279, lon=-83.339)
        _cycle_to_myloc(state)
        snap = state.snapshot()
        self.assertFalse(snap.compose_myloc_no_fix)

    def test_2d_fix_also_works(self) -> None:
        # 2D fix has lat/lon (just no altitude); should still pre-fill.
        state = _make_state_with_fix(
            lat=43.279, lon=-83.339, kind=FixKind.FIX_2D,
        )
        _cycle_to_myloc(state)
        snap = state.snapshot()
        self.assertEqual(snap.compose_text, "MYLOC 43.2790,-83.3390")
        self.assertIs(snap.compose_cmd, ComposeCmd.MSG)


class TestMylocNoFix(unittest.TestCase):
    """v0.0.19: cycling to MYLOC with no fix sets warning, stays on MYLOC."""

    def test_no_fix_sets_warning_flag(self) -> None:
        state = _make_state_no_fix()
        _cycle_to_myloc(state)
        snap = state.snapshot()
        self.assertTrue(snap.compose_myloc_no_fix)

    def test_no_fix_cmd_stays_at_myloc(self) -> None:
        state = _make_state_no_fix()
        _cycle_to_myloc(state)
        snap = state.snapshot()
        self.assertIs(snap.compose_cmd, ComposeCmd.MYLOC)

    def test_no_fix_text_unchanged(self) -> None:
        # Pre-existing text in the body must NOT be overwritten when
        # the operator hit MYLOC and there was no fix.
        state = _make_state_no_fix()
        state.compose_set_text("hello world")
        _cycle_to_myloc(state)
        snap = state.snapshot()
        self.assertEqual(snap.compose_text, "hello world")

    def test_warning_clears_on_next_cycle(self) -> None:
        # After cycling to MYLOC with no fix (warning set), cycling
        # again (away from MYLOC) must clear the warning.
        state = _make_state_no_fix()
        _cycle_to_myloc(state)
        self.assertTrue(state.snapshot().compose_myloc_no_fix)
        # One more backward step lands on QUERY_MSG (one before MYLOC
        # in the cycle order).
        state.compose_cycle_cmd(forward=False)
        snap = state.snapshot()
        self.assertFalse(snap.compose_myloc_no_fix)
        self.assertIs(snap.compose_cmd, ComposeCmd.QUERY_MSG)


class TestMylocSourceFileIsAscii(unittest.TestCase):
    """Per v0.0.14 paste-encoding incident: new test source stays ASCII."""

    def test_source_file_is_ascii(self) -> None:
        with open(__file__, "rb") as f:
            raw = f.read()
        non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        self.assertFalse(non_ascii, (
            f"test_v0019.py contains {len(non_ascii)} non-ASCII "
            "bytes; tests must be pure ASCII per the v0.0.14 "
            "paste-encoding policy"
        ))


if __name__ == "__main__":
    unittest.main()
