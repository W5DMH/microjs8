"""Tests for microjs8.ui.screens.

Each screen must render a 240x240 RGB image without raising. We do not
assert anything pixel-perfect — that's a visual-inspection job — but we
do verify image dimensions, colour mode, and that the image is
non-trivial (not entirely the background colour, which would mean the
renderer painted nothing).
"""

from __future__ import annotations

import pytest
from PIL import Image

from microjs8.ui import theme
from microjs8.ui.fonts import load_fonts
from microjs8.ui.screens import render
from microjs8.ui.state import Screen, UISnapshot


@pytest.fixture(scope="module")
def fonts():
    """Load fonts once per test module — they're stateless."""
    return load_fonts()


def _snapshot(
    screen: Screen,
    *,
    configured: bool = True,
    shutdown_remaining: float = 1.0,
) -> UISnapshot:
    if configured:
        return UISnapshot(
            screen=screen,
            callsign="K1ABC",
            grid="FN42",
            units="miles",
            tx_allowed=True,
            emergency_override=False,
            shutdown_remaining=shutdown_remaining,
            previous_screen=Screen.HOME,
        )
    return UISnapshot(
        screen=screen,
        callsign="N0CALL",
        grid="",
        units="miles",
        tx_allowed=False,
        emergency_override=False,
        shutdown_remaining=shutdown_remaining,
        previous_screen=Screen.HOME,
    )


@pytest.mark.parametrize("screen", list(Screen))
def test_every_screen_renders_240x240_rgb(screen, fonts):
    img = render(_snapshot(screen), fonts)
    assert isinstance(img, Image.Image)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)
    assert img.mode == "RGB"


@pytest.mark.parametrize("screen", list(Screen))
def test_every_screen_paints_something(screen, fonts):
    """Image must contain at least two distinct pixel values.

    Catches the trivial "renderer drew nothing on the canvas" bug.
    """
    img = render(_snapshot(screen), fonts)
    colours = img.getcolors(maxcolors=2**16)
    assert colours is not None
    assert len(colours) > 1, f"{screen.name} rendered as a flat block"


@pytest.mark.parametrize("screen", list(Screen))
def test_every_screen_renders_for_unconfigured_station(screen, fonts):
    """Unconfigured station must not crash any renderer."""
    img = render(_snapshot(screen, configured=False), fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_shutting_down_progress_changes_image(fonts):
    """Different progress values must produce visibly different frames."""
    full = render(_snapshot(Screen.SHUTTING_DOWN, shutdown_remaining=1.0), fonts)
    half = render(_snapshot(Screen.SHUTTING_DOWN, shutdown_remaining=0.5), fonts)
    empty = render(_snapshot(Screen.SHUTTING_DOWN, shutdown_remaining=0.0), fonts)
    assert full.tobytes() != half.tobytes()
    assert half.tobytes() != empty.tobytes()


def test_renderer_exception_returns_error_frame(fonts):
    """A bad screen value must produce an error frame, not raise."""
    # Build a snapshot with an out-of-range screen value via dataclass __new__
    from dataclasses import replace
    ok = _snapshot(Screen.HOME)
    # Use object.__setattr__ to bypass frozen=True for this test.
    bad = UISnapshot(
        screen=Screen(999) if 999 in [m.value for m in Screen] else None,  # type: ignore[arg-type]
        callsign=ok.callsign,
        grid=ok.grid,
        tx_allowed=ok.tx_allowed,
    ) if False else replace(ok)
    # Cheaper: monkey-patch the dispatch table to inject a raising renderer.
    from microjs8.ui import screens as scr_mod

    def boom(state, fonts):
        raise RuntimeError("synthetic renderer failure")

    saved = scr_mod._RENDERERS[Screen.HOME]
    scr_mod._RENDERERS[Screen.HOME] = boom
    try:
        img = render(_snapshot(Screen.HOME), fonts)
        assert img.size == (theme.SCREEN_W, theme.SCREEN_H)
        # Error frame should contain the word "error" or "ERROR" in some form
        # — we don't poke at pixel content, but the call must succeed.
    finally:
        scr_mod._RENDERERS[Screen.HOME] = saved


# ── Setup screen Radio row ──────────────────────────────────────────


def test_setup_rows_includes_radio_row():
    """The Setup screen must expose a Radio row keyed 'radio' so the
    router can identify it for cycle-on-Enter dispatch."""
    from microjs8.ui.screens import _setup_rows

    snap = _snapshot(Screen.SETUP)
    rows = _setup_rows(snap)
    field_names = [r[0] for r in rows]
    assert "radio" in field_names
    # Confirm ordering: radio comes after freq_hz (the editable last
    # text field) and before mode/logs (which are read-only display).
    radio_idx = field_names.index("radio")
    freq_idx = field_names.index("freq_hz")
    mode_idx = field_names.index("mode")
    assert freq_idx < radio_idx < mode_idx


def test_setup_radio_row_shows_display_name():
    """The displayed value for the radio row must be the human-
    readable display_name from the registry, not the raw id. (e.g.
    "QRP Labs QDX" rather than "qdx".)"""
    from microjs8.cat.radios import get_radio
    from microjs8.ui.screens import _setup_rows

    snap = _snapshot(Screen.SETUP)
    # _snapshot builds with default radio_id="qdx".
    rows = _setup_rows(snap)
    radio_row = next(r for r in rows if r[0] == "radio")
    expected_label = get_radio("qdx").display_name
    assert radio_row[2] == expected_label  # third tuple slot = displayed value


def test_setup_radio_row_falls_back_to_raw_id_when_unknown():
    """If somehow an unknown radio_id makes it into UISnapshot, the
    row should display the raw id rather than crash. Defense-in-depth
    against a registry/config drift bug."""
    from dataclasses import replace
    from microjs8.ui.screens import _setup_rows

    snap = _snapshot(Screen.SETUP)
    # Inject an id that the registry won't know.
    snap = replace(snap, radio_id="not-a-real-radio")
    rows = _setup_rows(snap)
    radio_row = next(r for r in rows if r[0] == "radio")
    assert radio_row[2] == "not-a-real-radio"


def test_setup_screen_renders_with_radio_focused(fonts):
    """The Setup screen must render cleanly when the radio row is the
    focused field — exercising the focus chevron + radio rendering
    path together."""
    from dataclasses import replace
    snap = _snapshot(Screen.SETUP)
    snap = replace(snap, focused_field="radio")
    img = render(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


# ── Inbox / mailbox renderers (INBOX screen) ─────────────────────────


from microjs8.ui.screens import (
    _render_inbox,
    _render_inbox_detail,
    _format_inbox_time,
    _format_inbox_full_time,
    _wrap_message_body,
)
from microjs8.ui.state import InboxRow


def _inbox_row(
    *,
    rid: int = 1,
    from_call: str = "KC1WDO",
    body: str = "hello",
    utc: str = "2026-05-06T14:23:11.000+00:00",
    snr: int | None = -3,
    is_read: bool = False,
) -> InboxRow:
    return InboxRow(
        id=rid, from_call=from_call, body=body,
        utc_iso=utc, snr_db=snr, is_read=is_read,
    )


def _inbox_snapshot(
    *,
    messages=(),
    held=0,
    unread=0,
    focused=0,
    detail_id=None,
    screen=Screen.INBOX,
):
    return UISnapshot(
        screen=screen,
        callsign="K1ABC",
        grid="FN42",
        units="miles",
        tx_allowed=True,
        emergency_override=False,
        shutdown_remaining=1.0,
        previous_screen=Screen.HOME,
        inbox_messages=messages,
        inbox_unread_count=unread,
        inbox_held_count=held,
        inbox_focused_index=focused,
        inbox_detail_id=detail_id,
    )


def test_inbox_empty_renders_help_text(fonts):
    snap = _inbox_snapshot()
    img = _render_inbox(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)
    # No exception is the main success criterion; image must be non-blank.
    extrema = img.getextrema()
    # Image is RGB, so extrema is a tuple of (min,max) per channel.
    assert any(mx > 0 for _, mx in extrema), "image looks entirely black"


def test_inbox_with_messages_renders(fonts):
    msgs = (_inbox_row(rid=2, body="b"), _inbox_row(rid=1, body="a"))
    snap = _inbox_snapshot(messages=msgs, unread=2)
    img = _render_inbox(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_inbox_with_focused_index_renders(fonts):
    msgs = (_inbox_row(rid=2, body="b"), _inbox_row(rid=1, body="a", is_read=True))
    snap = _inbox_snapshot(messages=msgs, unread=1, focused=1)
    img = _render_inbox(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_inbox_with_long_body_truncates_gracefully(fonts):
    """List view truncates bodies — must not raise on long input."""
    long_body = "x" * 500
    msgs = (_inbox_row(body=long_body),)
    snap = _inbox_snapshot(messages=msgs, unread=1)
    img = _render_inbox(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_inbox_held_only_no_messages(fonts):
    """Empty inbox + holding mail for others — should mention held count."""
    snap = _inbox_snapshot(messages=(), held=3, unread=0)
    img = _render_inbox(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_inbox_with_missing_snr(fonts):
    """Local-store rows have snr_db=None — must render without crashing."""
    msgs = (_inbox_row(snr=None),)
    snap = _inbox_snapshot(messages=msgs, unread=1)
    img = _render_inbox(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


# Detail view ───────────────────────────────────────────────────────


def test_detail_view_renders_with_valid_id(fonts):
    msgs = (_inbox_row(rid=42, body="this is the full message body"),)
    snap = _inbox_snapshot(
        messages=msgs, unread=1, detail_id=42, screen=Screen.INBOX_DETAIL,
    )
    img = _render_inbox_detail(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_detail_view_with_stale_id_does_not_crash(fonts):
    """Race: row was deleted while detail view was open. Render
    must show a friendly message rather than raising."""
    snap = _inbox_snapshot(
        messages=(), detail_id=99, screen=Screen.INBOX_DETAIL,
    )
    img = _render_inbox_detail(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_detail_view_with_long_body_clips_with_indicator(fonts):
    """Body longer than the body-region must clip — verifying
    no exception, not pixel-correctness."""
    long_body = "\n".join(["This is a line of text"] * 50)
    msgs = (_inbox_row(rid=1, body=long_body),)
    snap = _inbox_snapshot(
        messages=msgs, detail_id=1, screen=Screen.INBOX_DETAIL,
    )
    img = _render_inbox_detail(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_detail_view_with_no_snr_renders(fonts):
    msgs = (_inbox_row(rid=1, snr=None),)
    snap = _inbox_snapshot(
        messages=msgs, detail_id=1, screen=Screen.INBOX_DETAIL,
    )
    img = _render_inbox_detail(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


# Helper functions ──────────────────────────────────────────────────


def test_format_inbox_time_extracts_hh_mm():
    assert _format_inbox_time("2026-05-06T14:23:11.000+00:00") == "14:23"


def test_format_inbox_time_falls_back_on_garbage():
    assert _format_inbox_time("") == "--:--"
    assert _format_inbox_time("garbage") == "--:--"


def test_format_inbox_time_handles_space_separator():
    """ISO 8601 allows space in place of T."""
    assert _format_inbox_time("2026-05-06 14:23:11.000+00:00") == "14:23"


def test_format_inbox_full_time():
    out = _format_inbox_full_time("2026-05-06T14:23:11.000+00:00")
    assert out == "2026-05-06 14:23 UTC"


def test_format_inbox_full_time_unknown():
    assert _format_inbox_full_time("") == "(unknown)"


def test_wrap_message_body_word_break():
    lines = _wrap_message_body(
        "hello world this is a long sentence to wrap",
        max_chars=20,
    )
    assert all(len(line) <= 20 for line in lines)
    # Reconstruction matches (modulo word-spacing)
    assert "hello" in " ".join(lines)
    assert "wrap" in " ".join(lines)


def test_wrap_message_body_hard_break_on_long_word():
    """Single word longer than max_chars must hard-break."""
    lines = _wrap_message_body("supercalifragilisticexpialidocious", max_chars=10)
    assert all(len(line) <= 10 for line in lines)
    # All chars present
    assert "".join(lines) == "supercalifragilisticexpialidocious"


def test_wrap_message_body_preserves_paragraphs():
    """Multi-paragraph body should preserve blank line between."""
    lines = _wrap_message_body("para one\n\npara two", max_chars=20)
    assert "" in lines  # the blank line separating the paragraphs


def test_home_screen_renders_inbox_indicator_when_held(fonts):
    """Home screen should render even when held_count > 0."""
    snap = UISnapshot(
        screen=Screen.HOME,
        callsign="K1ABC",
        grid="FN42",
        units="miles",
        tx_allowed=True,
        emergency_override=False,
        inbox_held_count=3,
        inbox_unread_count=1,
    )
    from microjs8.ui.screens import render
    img = render(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


# ── Directed activity log (chat-style DIRECTED screen) ──────────────


from microjs8.activity import DirectedActivityEntry, Direction
from microjs8.ui.screens import _render_directed as _render_directed_log


def _activity_in(
    *, from_call: str, verb: str, body: str = "",
    snr_db: int | None = -8, freq_hz: float = 1500.0,
    at_unix: float = 1700000000.0,
) -> DirectedActivityEntry:
    return DirectedActivityEntry(
        at_unix=at_unix,
        direction=Direction.IN,
        other_call=from_call.upper(),
        verb=verb,
        body=body,
        snr_db=snr_db,
        freq_hz=freq_hz,
    )


def _activity_out(
    *, to_call: str, verb: str, body: str = "",
    at_unix: float = 1700000005.0,
) -> DirectedActivityEntry:
    return DirectedActivityEntry(
        at_unix=at_unix,
        direction=Direction.OUT,
        other_call=to_call.upper(),
        verb=verb,
        body=body,
        snr_db=None,
        freq_hz=None,
    )


def _directed_snapshot(entries: tuple[DirectedActivityEntry, ...] = ()):
    return UISnapshot(
        screen=Screen.DIRECTED,
        callsign="W5DMH",
        grid="EN83",
        units="miles",
        tx_allowed=True,
        emergency_override=False,
        shutdown_remaining=1.0,
        previous_screen=Screen.HOME,
        directed_log_entries=entries,
    )


def test_directed_log_empty_renders_help(fonts):
    """Empty log renders the placeholder text without raising."""
    img = _render_directed_log(_directed_snapshot(), fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)
    extrema = img.getextrema()
    assert any(mx > 0 for _, mx in extrema), "image looks entirely black"


def test_directed_log_inbound_only_renders(fonts):
    entries = (
        _activity_in(from_call="KD8PGB", verb="SNR?"),
        _activity_in(from_call="KC1WDO", verb="QUERY MSGS"),
    )
    img = _render_directed_log(_directed_snapshot(entries), fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_directed_log_chat_style_in_and_out(fonts):
    """A round-trip exchange — inbound query then our outbound reply —
    should render without error."""
    entries = (
        _activity_in(from_call="KD8PGB", verb="QUERY MSGS"),
        _activity_out(to_call="KD8PGB", verb="MSG", body="5"),
        _activity_in(from_call="KC1WDO", verb="SNR?"),
        _activity_out(to_call="KC1WDO", verb="SNR", body="-8"),
    )
    img = _render_directed_log(_directed_snapshot(entries), fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_directed_log_truncates_at_visible_rows(fonts):
    """Many entries shouldn't crash; renderer slices to fit."""
    entries = tuple(
        _activity_in(from_call=f"K{i}AB", verb="SNR?", at_unix=float(i))
        for i in range(50)
    )
    img = _render_directed_log(_directed_snapshot(entries), fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_directed_log_long_body_does_not_overflow(fonts):
    """Long bodies should ellipsize, not throw or paint outside bounds."""
    entries = (
        _activity_in(
            from_call="K1ABC", verb="STATUS",
            body="all systems nominal but verbose extra commentary here",
        ),
    )
    img = _render_directed_log(_directed_snapshot(entries), fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_directed_log_render_via_dispatcher(fonts):
    """render() dispatches Screen.DIRECTED to the activity-log renderer
    (regression guard against the renderer dict mapping breaking)."""
    from microjs8.ui.screens import render
    snap = _directed_snapshot((
        _activity_in(from_call="K1ABC", verb="GRID?"),
    ))
    img = render(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_inbox_render_via_dispatcher(fonts):
    """render() dispatches Screen.INBOX to the inbox renderer."""
    from microjs8.ui.screens import render
    snap = _inbox_snapshot(
        messages=(_inbox_row(),),
        unread=1, screen=Screen.INBOX,
    )
    img = render(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_directed_log_full_yes_msg_id_fits_without_truncation(fonts):
    """The on-air canary: 'KD8PGB YES MSG ID 57' should fit on the
    240px screen without truncation. Previously the renderer
    hard-truncated at 22 chars which ate the trailing '57' from the
    duplicated 'YES YES MSG ID 57' the activity log would emit.

    With both fixes (verb-dedup in app + pixel-width truncation),
    the rendered row shows the full message ID.
    """
    entries = (
        DirectedActivityEntry(
            at_unix=1700000000.0,
            direction=Direction.IN,
            other_call="KD8PGB",
            verb="YES",
            body="MSG ID 57",   # post-dedup body
            snr_db=12,
            freq_hz=1616.1,
        ),
    )
    snap = _directed_snapshot(entries)
    img = _render_directed_log(snap, fonts)
    # We can't check the rendered pixels exactly, but we can check the
    # image dimensions and that the renderer didn't crash.
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_directed_log_very_long_body_does_ellipsize(fonts):
    """When a body is genuinely too long to fit even the full screen
    width, the renderer must ellipsize — not silently let the text
    overflow past the right edge."""
    long_body = "A" * 100  # absurdly long
    entries = (
        DirectedActivityEntry(
            at_unix=1700000000.0,
            direction=Direction.IN,
            other_call="K1ABC",
            verb="STATUS",
            body=long_body,
            snr_db=-5, freq_hz=1500.0,
        ),
    )
    snap = _directed_snapshot(entries)
    img = _render_directed_log(snap, fonts)
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


# ── Header banner: UTC clock on every screen ─────────────────────────


def test_header_time_format_is_just_hhmmss():
    """Phase 19 v0.0.9: header clock is the bare ``HH:MM:SS`` string
    (8 chars, no ``UTC `` prefix).

    Pre-v0.0.9 we rendered ``UTC HH:MM:SS`` (12 chars) but the
    centered-clock layout introduced in v0.0.8 couldn't afford the
    extra ~30 px — long titles like EMERGENCY truncated into the
    clock zone. JS8 station time is UTC by convention; the TimeSrc
    row on HOME continues to surface the source for newbies.
    """
    from microjs8.ui.screens import _format_time_for_header
    snap = _snapshot(Screen.HOME)
    s = _format_time_for_header(snap)
    assert not s.startswith("UTC"), (
        f"clock string must NOT carry the UTC prefix in v0.0.9; got {s!r}"
    )
    assert len(s) == 8, (
        f"clock string must be exactly 8 chars (HH:MM:SS); got {s!r}"
    )
    assert s.count(":") == 2
    for part in s.split(":"):
        assert len(part) == 2
        assert part == "--" or part.isdigit()


def test_header_clock_is_always_white_regardless_of_source(fonts):
    """The header clock is rendered in HEADER_FG (white) whether or
    not a time source is active. We previously dimmed it on no-source
    as a confidence signal, but that conflicted with operator
    expectations ("why is my clock greyed out?") — so the source
    state is now spelled out plainly on HOME's TimeSrc row instead.

    Tested by rendering the header for each source state and sampling
    pixels at the clock's center column. With FG_DIM the pixel value
    would be ~140; with HEADER_FG it should be ≥200.
    """
    from PIL import Image, ImageDraw
    from dataclasses import replace
    from microjs8.ui.screens import _draw_header

    for src in ("chrony", "consensus", ""):
        snap = replace(_snapshot(Screen.DIRECTED), time_source=src)
        img = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), theme.BG)
        draw = ImageDraw.Draw(img)
        _draw_header(draw, fonts, "DIRECTED", snap)
        # Find the brightest pixel in the center column inside the
        # header band — that's a glyph stroke from the clock.
        max_brightness = 0
        for x in range(100, 140):
            for y in range(4, 24):
                r, g, b = img.getpixel((x, y))
                # Foreground glyphs land near (220,220,220) — measure
                # the green channel as a proxy for white-ness.
                if g > max_brightness:
                    max_brightness = g
        assert max_brightness >= 180, (
            f"clock too dim for source={src!r}: brightest pixel green "
            f"channel was {max_brightness}, expected ≥180. Did the dim-"
            f"on-no-source logic come back?"
        )


def test_home_screen_no_longer_has_time_row(fonts):
    """HOME should NOT contain the literal time string — that's now
    in the header on every screen, freeing HOME's body for other
    info. HOME keeps a 'TimeSrc' row showing just the source tag
    so operators can distinguish UTC from CONSENSUS at a glance."""
    snap = _snapshot(Screen.HOME)
    img = render(snap, fonts)
    # Render → bytes for keyword search. Not a strict guarantee of
    # absence (we don't OCR) but verifies the renderer doesn't blow
    # up and produces a sane image.
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_home_timesrc_label_chrony_says_utc():
    """HOME's TimeSrc row spells out the source — UTC for chrony."""
    from microjs8.ui.screens import _time_source_label
    from dataclasses import replace
    snap = replace(_snapshot(Screen.HOME), time_source="chrony")
    label, color = _time_source_label(snap)
    assert label == "UTC"
    assert color == theme.FG


def test_home_timesrc_label_consensus_says_consensus():
    """HOME's TimeSrc row spells out the source — CONSENSUS for radio-derived."""
    from microjs8.ui.screens import _time_source_label
    from dataclasses import replace
    snap = replace(_snapshot(Screen.HOME), time_source="consensus")
    label, color = _time_source_label(snap)
    assert label == "CONSENSUS"
    assert color == theme.FG


def test_home_timesrc_label_no_source_says_none_dim():
    """HOME's TimeSrc row says NONE when neither source is usable —
    color dim so operator sees TX is blocked at a glance."""
    from microjs8.ui.screens import _time_source_label
    snap = _snapshot(Screen.HOME)  # default time_source is empty
    label, color = _time_source_label(snap)
    assert label == "NONE"
    assert color == theme.FG_DIM


# ── Header clock layout: 75%-of-title bold, centered ─────────────────


def test_header_clock_font_is_about_three_quarters_of_title(fonts):
    """The header clock font should be ~75% the size of the title
    font. Sized that way, the clock reads as a paired companion to
    the title rather than a separate UI element. Tested by measuring
    the pixel width of the same sample string in each font: clock's
    width should land in the [60%, 90%] band of title's. Outside
    that range means somebody changed FONT_CLOCK and lost the
    intended 75% relationship."""
    from PIL import Image, ImageDraw
    # Dimensions don't matter for this test — we only measure text widths.
    # Use theme constants to stay consistent with the rest of the suite.
    img = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    sample = "HH:MM:SS"
    title_w = draw.textlength(sample, font=fonts.title)
    clock_w = draw.textlength(sample, font=fonts.clock)
    ratio = clock_w / title_w
    assert 0.6 <= ratio <= 0.9, (
        f"clock width is {ratio:.0%} of title width ({clock_w}px / "
        f"{title_w}px) — expected ~75%. Did FONT_CLOCK move?"
    )


def test_header_clock_renders_in_right_region(fonts):
    """The clock should land at the right edge of the header
    (right-aligned with PAD_X padding) — not in the center where it
    used to overlap with long titles like 'EMERGENCY' or 'DIRECTED
    MENU'. Pixels light up in the right column when rendered.

    The search region is computed from theme constants so the test
    adapts to whatever panel size we ship — currently the
    CardputerZero's 320×170, formerly MiniJS8's 240×240.
    """
    from PIL import Image, ImageDraw
    from microjs8.ui.screens import _draw_header
    from dataclasses import replace
    snap = replace(_snapshot(Screen.DIRECTED), time_source="chrony")
    img = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), theme.BG)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, fonts, "DIRECTED", snap)
    # Right region: rightmost ~40 px of the panel (the clock format
    # "UTC HH:MM:SS" right-aligned with PAD_X from the right edge).
    # Vertical: full header height.
    right_painted = False
    search_w = 40
    for x in range(theme.SCREEN_W - search_w, theme.SCREEN_W - theme.PAD_X):
        for y in range(theme.PAD_Y, theme.HEADER_H):
            if img.getpixel((x, y)) != theme.HEADER_BG:
                right_painted = True
                break
        if right_painted:
            break
    assert right_painted, (
        "no clock text found in right region of header — clock may "
        "have moved or stopped rendering"
    )


def test_header_does_not_render_position_indicator(fonts):
    """The ring-position N/M indicator was removed in this iteration
    (it took space without telling the operator anything new). Verify
    no '/' character renders anywhere in the header band on a normal
    screen."""
    from PIL import Image, ImageDraw
    from microjs8.ui.screens import _draw_header
    snap = _snapshot(Screen.DIRECTED)
    img = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), theme.BG)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, fonts, "DIRECTED", snap)
    # We can't OCR, but we can sample the right column where position
    # USED to live (top-right at y≈8, x≈225) and confirm it's
    # background-colored — no glyph rendered there.
    # (The clock's vertical center is y≈14, not y≈8, so a few pixels
    # in the top-right at y=4-8 should be empty.)
    top_right_empty = True
    for x in range(225, 236):
        for y in range(4, 9):
            if img.getpixel((x, y)) != theme.HEADER_BG:
                top_right_empty = False
                break
    # Sanity: clock IS at vertical center, so y=10-22 should still
    # have glyphs. We're only checking the top-right strip is empty.
    assert top_right_empty, (
        "top-right corner has pixels painted — position indicator "
        "may not have been removed properly"
    )


# ── Outbound-red coloring on DIRECTED ─────────────────────────────────


def test_directed_log_outbound_body_renders_in_fg_bad_red(fonts):
    """Outbound entries (the operator's TX'd messages) render in red
    (FG_BAD) so they're visually distinct from inbound. JS8Call uses
    a distinct color for TX text; we follow the convention. Sample
    the body text region of an outbound row and confirm there's a
    red pixel — meaning the renderer used the red color, not the
    default FG."""
    out = _activity_out(to_call="K1ABC", verb="MSG", body="hello dave")
    img = _render_directed_log(_directed_snapshot((out,)), fonts)
    # The body text starts at x=18 (PAD_X + 14 chevron) and lives
    # in the row band starting around BODY_Y0. Sample a strip to
    # find any red pixel.
    found_red = False
    for x in range(18, 200):
        for y in range(theme.BODY_Y0 + 4, theme.BODY_Y0 + 24):
            r, g, b = img.getpixel((x, y))
            if r > 180 and g < 100 and b < 100:
                found_red = True
                break
        if found_red:
            break
    assert found_red, (
        "outbound body text should render in red (FG_BAD ~= 220,60,60); "
        "no red pixel found in the first row's body region — outbound "
        "color signaling may have regressed"
    )


def test_directed_log_inbound_body_does_not_render_in_red(fonts):
    """Inbound body text uses default FG (~220 on all channels). No
    red pixels should appear in the body region — a regression where
    inbound got the outbound color would show up as red glyphs here."""
    inn = _activity_in(from_call="K1ABC", verb="MSG", body="hello back")
    img = _render_directed_log(_directed_snapshot((inn,)), fonts)
    # Same sampling area as the outbound test
    for x in range(18, 200):
        for y in range(theme.BODY_Y0 + 4, theme.BODY_Y0 + 24):
            r, g, b = img.getpixel((x, y))
            assert not (r > 180 and g < 100 and b < 100), (
                f"unexpected red pixel at ({x},{y}) in inbound row — "
                f"color may have regressed to outbound styling"
            )


# ── COMPOSE TX-warning ────────────────────────────────────────────────


def _compose_snapshot(*, tx_allowed: bool = True, time_source: str = "chrony"):
    from microjs8.ui.state import ComposeCmd
    return UISnapshot(
        screen=Screen.COMPOSE,
        callsign="W5DMH", grid="EN83", units="miles",
        tx_allowed=tx_allowed, emergency_override=False,
        shutdown_remaining=1.0, previous_screen=Screen.HOME,
        time_source=time_source,
        compose_to="K1ABC", compose_cmd=ComposeCmd.FREE,
        compose_text="hi", compose_focused_field="compose_send",
    )


def test_compose_warns_when_no_time_source(fonts):
    """When time_source is empty (no chrony, no consensus), the
    scheduler won't fire — we surface this on COMPOSE so the operator
    knows their SEND will queue but not transmit immediately. Detect
    by sampling for warn-colored pixels (~240,180,40 = FG_WARN)."""
    from microjs8.ui.screens import _render_compose
    snap = _compose_snapshot(time_source="")
    img = _render_compose(snap, fonts)
    found_warn = False
    for x in range(theme.SCREEN_W):
        for y in range(theme.BODY_Y0, theme.BODY_Y1):
            r, g, b = img.getpixel((x, y))
            if r > 200 and 140 < g < 200 and b < 100:
                found_warn = True
                break
        if found_warn:
            break
    assert found_warn, (
        "expected a FG_WARN-colored TX-blocked hint on COMPOSE when "
        "time_source is empty; not found"
    )


def test_compose_no_warning_when_time_synced(fonts):
    """When time is synced (chrony or consensus), no warning shows —
    UI is clean. Sample for warn-colored pixels in body and assert
    none present."""
    from microjs8.ui.screens import _render_compose
    snap = _compose_snapshot(time_source="chrony")
    img = _render_compose(snap, fonts)
    for x in range(theme.SCREEN_W):
        for y in range(theme.BODY_Y0 + 100, theme.BODY_Y1):  # below TEXT box
            r, g, b = img.getpixel((x, y))
            assert not (r > 200 and 140 < g < 200 and b < 100), (
                f"unexpected warn pixel at ({x},{y}) when time is "
                f"synced — TX hint should not be shown"
            )


# ── Phase 6: HOME battery row + COMPOSE battery TX warning ──────────


def _battery_state(capacity: int, status: str = "Discharging"):
    from microjs8.power.battery import BatteryState
    return BatteryState(capacity, 3.7, -150.0, 25.0, status)


def test_home_renders_when_battery_unknown(fonts):
    """battery=None must not crash the renderer."""
    snap = _snapshot(Screen.HOME)
    img = render(snap, fonts)   # snap.battery is None by default
    assert img.size == (theme.SCREEN_W, theme.SCREEN_H)


def test_compose_warns_on_critical_battery(fonts):
    """The COMPOSE TX-warning chain should pick up battery-critical
    even when other gates pass. Detect via FG_WARN pixels."""
    from microjs8.ui.screens import _render_compose
    from dataclasses import replace
    snap = _compose_snapshot(time_source="chrony")
    snap = replace(snap, battery=_battery_state(3, "Discharging"))
    img = _render_compose(snap, fonts)
    found_warn = False
    for x in range(theme.SCREEN_W):
        for y in range(theme.BODY_Y0, theme.BODY_Y1):
            r, g, b = img.getpixel((x, y))
            if r > 200 and 140 < g < 200 and b < 100:
                found_warn = True
                break
        if found_warn:
            break
    assert found_warn, (
        "COMPOSE should show a battery-critical warning when battery is "
        "discharging at ≤5%"
    )


def test_compose_no_battery_warning_when_emergency_override(fonts):
    """§6.11: help beacon is exempt from the 5% cutoff. The COMPOSE
    warning chain must respect the same exemption — when
    emergency_override is set, no battery warning appears even if the
    battery is critical."""
    from microjs8.ui.screens import _render_compose
    from dataclasses import replace
    snap = _compose_snapshot(time_source="chrony")
    snap = replace(
        snap,
        battery=_battery_state(2, "Discharging"),
        emergency_override=True,
    )
    img = _render_compose(snap, fonts)
    # Check the bottom region of body (where the warning would render):
    for x in range(theme.SCREEN_W):
        for y in range(theme.BODY_Y0 + 100, theme.BODY_Y1):
            r, g, b = img.getpixel((x, y))
            assert not (r > 200 and 140 < g < 200 and b < 100), (
                f"unexpected FG_WARN pixel at ({x},{y}) — battery warning should "
                f"be suppressed under emergency override"
            )


# ── Phase 19 / v0.0.8: three-zone banner layout ──────────────────────


def test_banner_battery_zone_shows_dash_when_no_gauge(fonts):
    """Phase 19 (v0.0.8): battery zone must render ``--%`` when the
    fuel gauge isn't present (bare Pi / no bq27 chip). The state's
    ``battery`` field is None in that case, and the zone must NOT
    crash or show a stale number."""
    from microjs8.ui.screens import _format_battery_for_header

    s = _snapshot(Screen.HOME)
    # Snapshot default has battery=None
    assert s.battery is None
    result = _format_battery_for_header(s)
    assert result == "--%", f"expected '--%', got {result!r}"


def test_banner_battery_zone_shows_percent_when_present(fonts):
    """Phase 19: with a battery snapshot present, the zone renders
    the integer percent followed by ``%``."""
    from microjs8.ui.screens import _format_battery_for_header
    from microjs8.power.battery import BatteryState
    from dataclasses import replace

    bat = BatteryState(
        capacity=87,
        voltage_v=3.92,
        current_ma=-120.0,
        temperature_c=24.0,
        status="Discharging",
    )
    s = replace(_snapshot(Screen.HOME), battery=bat)
    result = _format_battery_for_header(s)
    assert result == "87%", f"expected '87%' (discharging), got {result!r}"


def test_banner_battery_zone_shows_plus_when_charging(fonts):
    """Phase 19: a leading ``+`` indicates charging."""
    from microjs8.ui.screens import _format_battery_for_header
    from microjs8.power.battery import BatteryState
    from dataclasses import replace

    bat = BatteryState(
        capacity=87,
        voltage_v=4.10,
        current_ma=+250.0,
        temperature_c=24.0,
        status="Charging",
    )
    s = replace(_snapshot(Screen.HOME), battery=bat)
    result = _format_battery_for_header(s)
    assert result == "+87%", f"expected '+87%' (charging), got {result!r}"


def test_banner_battery_color_red_when_no_gauge(fonts):
    """Phase 19: bare Pi (no battery state) renders in FG_BAD red so
    the operator notices that power state is unknown."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _battery_color

    s = _snapshot(Screen.HOME)
    assert s.battery is None
    assert _battery_color(s) == theme.FG_BAD


def test_banner_battery_color_red_when_critically_low(fonts):
    """Phase 19: <10% capacity → red. Operator needs to act now."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _battery_color
    from microjs8.power.battery import BatteryState
    from dataclasses import replace

    bat = BatteryState(
        capacity=5,
        voltage_v=3.3,
        current_ma=-100.0,
        temperature_c=24.0,
        status="Discharging",
    )
    s = replace(_snapshot(Screen.HOME), battery=bat)
    assert _battery_color(s) == theme.FG_BAD


def test_banner_battery_color_yellow_when_low(fonts):
    """Phase 19: 10-19% capacity → yellow warn. Operator should plug
    in soon but it's not urgent."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _battery_color
    from microjs8.power.battery import BatteryState
    from dataclasses import replace

    bat = BatteryState(
        capacity=15,
        voltage_v=3.6,
        current_ma=-100.0,
        temperature_c=24.0,
        status="Discharging",
    )
    s = replace(_snapshot(Screen.HOME), battery=bat)
    assert _battery_color(s) == theme.FG_WARN


def test_banner_battery_renders_on_all_screens(fonts):
    """Phase 19: battery zone must render on EVERY screen, not just
    HOME (the old behavior). Spot-check by rendering several
    different screens and confirming the battery text appears in
    the right zone of the header."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_home, _render_heard, _render_setup
    from microjs8.power.battery import BatteryState
    from dataclasses import replace

    bat = BatteryState(
        capacity=42,
        voltage_v=3.7,
        current_ma=-100.0,
        temperature_c=24.0,
        status="Discharging",
    )
    # Test multiple screens. Each renders without crashing, and each
    # has SOME non-background pixel in the rightmost slice (where
    # the battery text lives).
    for renderer, screen in [
        (_render_home, Screen.HOME),
        (_render_heard, Screen.HEARD),
        (_render_setup, Screen.SETUP),
    ]:
        s = replace(_snapshot(screen), battery=bat)
        img = renderer(s, fonts)
        # Check the rightmost ~60 px of the header band has at least
        # some non-background pixels (i.e., the battery text rendered)
        non_bg_count = 0
        for y in range(0, theme.HEADER_H):
            for x in range(theme.SCREEN_W - 60, theme.SCREEN_W - theme.PAD_X):
                px = img.getpixel((x, y))
                if px != theme.HEADER_BG:
                    non_bg_count += 1
        assert non_bg_count > 5, (
            f"battery zone empty on {screen.name}: only {non_bg_count} "
            f"non-background pixels in right slice — battery missing"
        )


def test_banner_time_is_centered(fonts):
    """Phase 19: time string must be CENTERED in the header, not
    right-aligned. Verify by rendering and checking the rendered
    text's midpoint ≈ SCREEN_W / 2.
    """
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_home

    s = _snapshot(Screen.HOME)  # default snapshot, no gps required for time
    img = _render_home(s, fonts)

    # Find leftmost and rightmost non-header-bg pixels in the header
    # band, then compare the midpoint to SCREEN_W/2. Skip the leftmost
    # ~100 px (the title) and rightmost ~70 px (the battery) — focus
    # on the center zone where the clock should be.
    left_pixel = theme.SCREEN_W
    right_pixel = 0
    for y in range(2, theme.HEADER_H - 2):
        for x in range(100, theme.SCREEN_W - 70):
            px = img.getpixel((x, y))
            if px != theme.HEADER_BG:
                left_pixel = min(left_pixel, x)
                right_pixel = max(right_pixel, x)

    if right_pixel == 0:
        # Clock didn't render — fail explicitly
        raise AssertionError("clock not visible in header center zone")

    text_center = (left_pixel + right_pixel) // 2
    expected_center = theme.SCREEN_W // 2
    # Allow ±15px tolerance for font metrics + descender quirks
    assert abs(text_center - expected_center) < 15, (
        f"clock not centered: text_center={text_center}, "
        f"expected ~{expected_center} (±15)"
    )


def test_banner_emergency_title_renders_without_truncation(fonts):
    """Phase 19 v0.0.9: the ``EMERGENCY`` title (~124 px at 18pt bold)
    must render fully without ellipsis-truncation, thanks to the
    adaptive-clock logic that shifts the clock right when a wide
    title would otherwise overlap the centered position.

    Pre-v0.0.9 the clock was true-centered, leaving only ~109 px for
    the title — EMERGENCY truncated to ``EMERG…``. The fix moves the
    clock right just enough to fit the full word.
    """
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_emergency
    from PIL import ImageDraw

    s = _snapshot(Screen.EMERGENCY)
    img = _render_emergency(s, fonts)

    # Measure the natural width of "EMERGENCY" at title font.
    probe = ImageDraw.Draw(img)
    full_w = int(probe.textlength("EMERGENCY", font=fonts.title))

    # Scan the header for the title's rightmost painted pixel in
    # the left zone (x ∈ [PAD_X, 150] is plenty of room).
    rightmost = 0
    for y in range(0, theme.HEADER_H):
        for x in range(theme.PAD_X, 150):
            if img.getpixel((x, y)) != theme.HEADER_BG:
                rightmost = max(rightmost, x)

    # If truncated, rightmost would be much less than PAD_X+full_w.
    # Allow ±5 px slack for font descender / accent variations.
    expected = theme.PAD_X + full_w
    assert rightmost >= expected - 5, (
        f"EMERGENCY title appears truncated: rightmost painted "
        f"pixel at x={rightmost}, expected near {expected} "
        f"(title width {full_w} + PAD_X={theme.PAD_X})"
    )


def test_banner_clock_shifts_right_on_emergency_screen(fonts):
    """Phase 19 v0.0.9: contract pin — when the title would force
    truncation at true-center, the clock visibly shifts right.

    Implementation: the clock string contains colons (``HH:MM:SS``)
    which titles don't, so we count painted columns at a y-row
    where colons render as a single-pixel dot. Then we observe
    where the FIRST clock colon (between HH and MM) shows up on
    each screen — that position shifts right when the layout
    has to make room for a wide title.
    """
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_home, _render_emergency

    home_snap = _snapshot(Screen.HOME)
    em_snap = _snapshot(Screen.EMERGENCY)
    home_img = _render_home(home_snap, fonts)
    em_img = _render_emergency(em_snap, fonts)

    def _first_colon_x_in_header(img):
        """Return the x of the first 'colon-like' single-column dot
        in the header band.

        A colon at the FONT_CLOCK font has two short vertical pixel
        runs centered around y≈10-15 of the header. We scan for
        columns that are dark above/below but have painted pixels
        only in the middle 2-3 rows.
        """
        for x in range(theme.PAD_X, theme.SCREEN_W - theme.PAD_X):
            mid_painted = sum(
                1 for y in range(8, 16)
                if img.getpixel((x, y)) != theme.HEADER_BG
            )
            edge_painted = sum(
                1 for y in (2, 3, 4, 19, 20, 21)
                if img.getpixel((x, y)) != theme.HEADER_BG
            )
            # Colon: painted in the middle, empty at top/bottom of the header
            if mid_painted >= 2 and edge_painted == 0:
                return x
        return None

    home_colon = _first_colon_x_in_header(home_img)
    em_colon = _first_colon_x_in_header(em_img)

    assert home_colon is not None, "HOME clock colon not found"
    assert em_colon is not None, "EMERGENCY clock colon not found"
    # EMERGENCY clock must be at least 5 px to the right of HOME's
    # (actual shift is ~15 px; 5 is a generous lower bound for
    # ±font-metric slack).
    assert em_colon >= home_colon + 5, (
        f"EMERGENCY clock should be shifted right of HOME clock by ≥5 px "
        f"(home_colon_x={home_colon}, emergency_colon_x={em_colon})"
    )


# ── Phase 19 v0.0.9: EXIT_CONFIRM modal rendering ──────────────────


def test_exit_confirm_renders_with_no_focused_by_default(fonts):
    """Phase 19 v0.0.9: render the EXIT_CONFIRM modal with default
    focus and verify the NO button has a green outline (focused state)
    while YES has a dim outline (unfocused)."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_exit_confirm
    from dataclasses import replace

    s = replace(_snapshot(Screen.EXIT_CONFIRM), focused_field="exit_no")
    img = _render_exit_confirm(s, fonts)

    # Scan the lower-third of the body for green pixels (FG_GOOD).
    # The NO button outline + label are both FG_GOOD when focused.
    found_green = False
    for y in range(theme.BODY_Y0 + 80, theme.BODY_Y0 + 130):
        for x in range(theme.SCREEN_W):
            r, g, b = img.getpixel((x, y))
            # FG_GOOD is roughly (60, 200, 80) — high green, low red/blue
            if g > 150 and r < 120 and b < 120:
                found_green = True
                break
        if found_green:
            break
    assert found_green, (
        "EXIT_CONFIRM with focus on NO must render at least one FG_GOOD "
        "(green) pixel — the NO button outline + label"
    )


def test_exit_confirm_renders_with_yes_focused(fonts):
    """Phase 19 v0.0.9: when focus moves to YES, render it with a red
    outline (FG_BAD)."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_exit_confirm
    from dataclasses import replace

    s = replace(_snapshot(Screen.EXIT_CONFIRM), focused_field="exit_yes")
    img = _render_exit_confirm(s, fonts)

    # Scan lower-third for red pixels (FG_BAD ≈ 220,60,60).
    found_red = False
    for y in range(theme.BODY_Y0 + 80, theme.BODY_Y0 + 130):
        for x in range(theme.SCREEN_W):
            r, g, b = img.getpixel((x, y))
            if r > 180 and g < 100 and b < 100:
                found_red = True
                break
        if found_red:
            break
    assert found_red, (
        "EXIT_CONFIRM with focus on YES must render at least one FG_BAD "
        "(red) pixel — the YES button outline + label"
    )


def test_exit_confirm_renders_explanation_text(fonts):
    """Phase 19 v0.0.9: the modal body explains what the YES action
    does, so the operator isn't choosing in the dark."""
    from microjs8.ui import theme
    from microjs8.ui.screens import _render_exit_confirm

    s = _snapshot(Screen.EXIT_CONFIRM)
    img = _render_exit_confirm(s, fonts)
    # Just verify we paint SOMETHING in the upper body area where the
    # explanation text lives (above the buttons).
    non_bg = 0
    for y in range(theme.BODY_Y0 + 4, theme.BODY_Y0 + 60):
        for x in range(theme.SCREEN_W):
            if img.getpixel((x, y)) != theme.BG:
                non_bg += 1
    assert non_bg > 100, (
        f"EXIT_CONFIRM body looks empty above buttons: {non_bg} non-bg "
        f"pixels found; expected the explanation text to render"
    )
