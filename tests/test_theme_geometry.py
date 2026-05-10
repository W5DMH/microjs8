"""Sanity checks on the layout constants in microjs8.ui.theme.

These are not behaviour tests — they assert that the theme's geometry
constants hang together internally, so a future tweak that breaks an
invariant (e.g. setting HEARD_COL_X to numbers wider than SCREEN_W,
or shrinking BODY_H below where HEARD content needs to fit) fails fast
in unit tests rather than at first-render time on hardware.

Phase 4: tuned for the CardputerZero's 320×170 ST7789v3 panel.
"""

from __future__ import annotations

from microjs8.ui import theme


# ── Panel and chrome dimensions ──────────────────────────────────────


def test_panel_size_is_320x170():
    """The CardputerZero LCD is 320×170. Hardcoded here because the
    panel size is a hardware fact, not a configurable choice — if it
    ever changes, every other geometry assumption needs revisiting."""
    assert theme.SCREEN_W == 320
    assert theme.SCREEN_H == 170


def test_header_and_footer_fit_in_panel():
    """Header + footer + a separator line each must leave at least 80
    body pixels — anything less and per-screen content becomes unusable.
    """
    chrome = theme.HEADER_H + theme.FOOTER_H + 2  # 1px separator each side
    assert chrome <= theme.SCREEN_H - 80, (
        f"chrome {chrome}px leaves only {theme.SCREEN_H - chrome}px of body — "
        f"need at least 80px"
    )


def test_body_y_bounds_are_inside_panel():
    """BODY_Y0 and BODY_Y1 must point inside the panel."""
    assert 0 < theme.BODY_Y0 < theme.SCREEN_H
    assert theme.BODY_Y0 < theme.BODY_Y1 < theme.SCREEN_H
    assert theme.BODY_H == theme.BODY_Y1 - theme.BODY_Y0


def test_body_height_matches_panel_arithmetic():
    """BODY_H must equal SCREEN_H minus header, footer, and separator
    pixels — proves there's no off-by-one between the constants."""
    expected = theme.SCREEN_H - theme.HEADER_H - theme.FOOTER_H - 2
    assert theme.BODY_H == expected


# ── HEARD list geometry ──────────────────────────────────────────────


def test_heard_columns_are_left_to_right_within_panel():
    """All HEARD column x-positions are in increasing order and stay
    inside the panel's usable width."""
    cols = theme.HEARD_COL_X
    assert len(cols) == 5, "HEARD has 5 columns: CALL, SNR, GRID, MI, AZ"
    assert cols == tuple(sorted(cols)), "columns must be left-to-right"
    assert cols[0] >= theme.PAD_X
    # Last column starts at cols[-1]; AZ is 4 chars at ~8.4px = ~34px.
    # Conservative cap: last column start + 40px must fit in panel.
    assert cols[-1] + 40 <= theme.SCREEN_W, (
        f"last HEARD column starts at {cols[-1]}; +40px overruns SCREEN_W={theme.SCREEN_W}"
    )


def test_heard_columns_have_minimum_gap():
    """Adjacent HEARD columns need at least 24px of separation so
    monospaced 3-4 char content (CALL=8ch is the widest) doesn't bleed
    into the next column. 24 ≈ 3 mono chars at FONT_BODY=14."""
    cols = theme.HEARD_COL_X
    gaps = [b - a for a, b in zip(cols, cols[1:])]
    for i, gap in enumerate(gaps):
        assert gap >= 24, (
            f"HEARD column gap {i}→{i+1} is only {gap}px — need ≥24px to "
            f"prevent overflow from the wider CALL column"
        )


def test_heard_rows_visible_is_positive_and_fits_body():
    """HEARD_ROWS_VISIBLE must be >= 1 (else operator can't see anything)
    and small enough that the rows fit in BODY_H above the column header."""
    assert theme.HEARD_ROWS_VISIBLE >= 1
    # Column header takes ~16 px (14 px row + 2 px top pad). Remaining
    # body must fit HEARD_ROWS_VISIBLE rows of HEARD_ROW_H each.
    needed = 16 + theme.HEARD_ROWS_VISIBLE * theme.HEARD_ROW_H
    assert needed <= theme.BODY_H, (
        f"HEARD layout needs {needed}px but BODY_H is only {theme.BODY_H}px"
    )


# ── Font sanity ──────────────────────────────────────────────────────


def test_font_sizes_are_in_descending_order():
    """The font hierarchy is large > title > body == clock > small.
    Ensures somebody bumping FONT_TITLE doesn't accidentally make it
    larger than FONT_LARGE (which would break the emergency banner's
    visual prominence)."""
    assert theme.FONT_LARGE >= theme.FONT_TITLE
    assert theme.FONT_TITLE >= theme.FONT_BODY
    assert theme.FONT_BODY >= theme.FONT_SMALL
    # CLOCK is paired with TITLE; in MicroJS8 they're the same size for
    # a balanced header. If a future tweak deliberately shrinks one,
    # this assertion may be relaxed.
    assert theme.FONT_CLOCK == theme.FONT_BODY


def test_title_font_fits_in_header():
    """FONT_TITLE rendered height (size + ~25% TT overhead) must fit
    inside HEADER_H. This is what the spec describes as 'title font
    fits without descender clipping'."""
    rendered_height = int(theme.FONT_TITLE * 1.25) + 1
    assert rendered_height <= theme.HEADER_H, (
        f"FONT_TITLE={theme.FONT_TITLE} renders to ~{rendered_height}px, "
        f"exceeds HEADER_H={theme.HEADER_H}"
    )


def test_small_font_fits_in_footer():
    """FONT_SMALL must fit inside FOOTER_H with similar TT overhead."""
    rendered_height = int(theme.FONT_SMALL * 1.25) + 1
    assert rendered_height <= theme.FOOTER_H, (
        f"FONT_SMALL={theme.FONT_SMALL} renders to ~{rendered_height}px, "
        f"exceeds FOOTER_H={theme.FOOTER_H}"
    )


def test_large_font_fits_in_body():
    """FONT_LARGE is the emergency / shutdown banner. Must fit in BODY_H
    with room for a second line of context text below."""
    rendered_height = int(theme.FONT_LARGE * 1.25) + 1
    assert rendered_height <= theme.BODY_H - 30, (
        f"FONT_LARGE={theme.FONT_LARGE} (renders ~{rendered_height}px) leaves "
        f"insufficient room in BODY_H={theme.BODY_H}px for context text"
    )
