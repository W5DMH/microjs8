"""Screen renderers.

Each renderer is a pure function: given a UI snapshot and a Fonts
bundle, it returns a fully-painted 240x240 ``PIL.Image`` ready to be
handed to the ST7789 driver. No global state, no I/O — that makes them
trivially testable on the host.

Layout convention (see ``theme.py``):

    +--- HEADER (28px) ----+
    |  TITLE          7/8  |    "7/8" = position-in-ring indicator
    +----------------------+    (omitted on screens not in the ring)
    |                      |
    |        BODY          |
    |                      |
    +--- FOOTER (18px) ----+
    |  hint text           |
    +----------------------+
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PIL import Image, ImageDraw

from microjs8.ui import theme
from microjs8.ui.fonts import Fonts
from microjs8.ui.state import HB_MODES_ORDERED, HbMode, RING, Screen, UISnapshot

_log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


def _new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A fresh 240x240 RGB image with the body-background colour."""
    img = Image.new("RGB", (theme.SCREEN_W, theme.SCREEN_H), theme.BG)
    draw = ImageDraw.Draw(img)
    return img, draw


def _draw_header(
    draw: ImageDraw.ImageDraw,
    fonts: Fonts,
    title: str,
    state: UISnapshot,
) -> None:
    """Header banner: title left, ``UTC HH:MM:SS`` right.

    Layout (28-px tall header):

      ┌────────────────────────────────────────┐
      │ TITLE                  UTC HH:MM:SS    │
      └────────────────────────────────────────┘
        ^left                  ^right-aligned
        18 pt bold             14 pt bold

    The ring-position indicator (N/M) was removed in this iteration —
    it took space without telling the operator anything they couldn't
    work out from "which screen am I currently on" + the footer hint.
    Right-aligning the clock prevents collision with long titles like
    "EMERGENCY" or "DIRECTED MENU"; centering looked elegant but
    overlapped on titles wider than ~100 px.

    Clock is rendered in HEADER_FG (white) regardless of time-source
    state. The diagnostic ("is timing trusted?") lives on HOME's
    TimeSrc row, which spells it out in plain language. Always-white
    here means the operator never has to wonder why the header
    looked dim.
    """
    draw.rectangle(
        [(0, 0), (theme.SCREEN_W - 1, theme.HEADER_H - 1)],
        fill=theme.HEADER_BG,
    )
    draw.line(
        [(0, theme.HEADER_H), (theme.SCREEN_W - 1, theme.HEADER_H)],
        fill=theme.SEPARATOR,
    )

    # Right column — measure the clock first so we know how much
    # horizontal space the title has. "UTC HH:MM:SS" at 14 pt bold
    # is ~118 px; the longest title ("DIRECTED MENU") is ~166 px so
    # without truncation they'd overlap on a 240-px screen.
    time_str = _format_time_for_header(state)
    try:
        time_w = int(draw.textlength(time_str, font=fonts.clock))
        bbox = fonts.clock.getbbox("0")
        time_h = bbox[3] - bbox[1]
    except Exception:
        time_w = 8 * len(time_str)
        time_h = theme.FONT_CLOCK
    time_x = theme.SCREEN_W - time_w - theme.PAD_X
    time_y = (theme.HEADER_H - time_h) // 2 - 1  # -1 looks more anchored

    # Title goes left, but must fit alongside the clock with at least
    # a 6-px breathing gap. If the operator navigates to a screen
    # whose title is too long, truncate with "…" — losing a few
    # characters is better than overlap on hardware.
    avail_title_w = time_x - theme.PAD_X - 6
    truncated_title = title
    try:
        title_w = int(draw.textlength(truncated_title, font=fonts.title))
        if title_w > avail_title_w:
            ellipsis = "…"
            i = len(truncated_title)
            while i > 0:
                candidate = truncated_title[:i] + ellipsis
                try:
                    cw = int(draw.textlength(candidate, font=fonts.title))
                except Exception:
                    cw = 10 * len(candidate)
                if cw <= avail_title_w:
                    truncated_title = candidate
                    break
                i -= 1
            else:
                truncated_title = ellipsis
    except Exception:
        # textlength unavailable — fall back to character-count guard.
        # Worst case we render the full title and PIL clips at the
        # canvas boundary; not pretty but not crash.
        pass

    draw.text(
        (theme.PAD_X, theme.PAD_Y + 2),
        truncated_title,
        font=fonts.title,
        fill=theme.HEADER_FG,
    )

    # Phase 12: armed-anywhere indicator. While the emergency beacon
    # is armed, the clock area is replaced with a red "SOS" badge so
    # the operator sees the active TX state no matter what screen
    # they're in; UTC time is useful but not as immediately critical,
    # and the operator can always come back to the EMERGENCY screen
    # to see exact countdown timing. Per spec §6.4 (May 2026):
    # "armed-anywhere indicator on every screen header".
    if state.emergency_beacon_armed:
        sos_text = "SOS"
        try:
            sos_w = int(draw.textlength(sos_text, font=fonts.clock))
            bbox = fonts.clock.getbbox("S")
            sos_h = bbox[3] - bbox[1]
        except Exception:
            sos_w = 32
            sos_h = theme.FONT_CLOCK
        # Right-align where the clock was. Add a 2-px border around
        # the text for badge-like visual weight on the dark header.
        badge_pad_x = 4
        badge_pad_y = 2
        sos_x = theme.SCREEN_W - sos_w - theme.PAD_X - badge_pad_x
        sos_y = (theme.HEADER_H - sos_h) // 2 - 1
        # Filled red rectangle behind the text for badge feel.
        draw.rectangle(
            [
                (sos_x - badge_pad_x, sos_y - badge_pad_y),
                (sos_x + sos_w + badge_pad_x - 1,
                 sos_y + sos_h + badge_pad_y - 1),
            ],
            fill=theme.FG_BAD,
        )
        # White text inside the red badge.
        draw.text(
            (sos_x, sos_y),
            sos_text,
            font=fonts.clock,
            fill=theme.HEADER_FG,
        )
    else:
        # Normal-state clock — right-aligned, vertically centered.
        draw.text(
            (time_x, time_y),
            time_str,
            font=fonts.clock,
            fill=theme.HEADER_FG,
        )


def _format_time_for_header(state: UISnapshot) -> str:
    """Return the ``UTC HH:MM:SS`` clock string for the header.

    Source priority for the displayed time:

      1. GPS fix (most authoritative when present — the GPS receiver's
         own pulse-per-second is locked to the UTC second).
      2. System clock as fallback. May be wildly wrong without any
         time source — that's a separate diagnostic surfaced via
         HOME's TimeSrc row, not via the clock display.

    The "UTC " prefix is part of the string returned (not a separate
    label) so the renderer measures + right-aligns it as a single
    unit. On format failure (rare), we render ``UTC --:--:--`` as a
    placeholder.
    """
    from datetime import datetime, timezone

    if state.gps.fix_time is not None:
        try:
            ts = datetime.fromtimestamp(state.gps.fix_time, tz=timezone.utc)
            return "UTC " + ts.strftime("%H:%M:%S")
        except (ValueError, OSError, OverflowError):
            return "UTC --:--:--"
    try:
        ts = datetime.fromtimestamp(time.time(), tz=timezone.utc)
        return "UTC " + ts.strftime("%H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "UTC --:--:--"


def _draw_footer(
    draw: ImageDraw.ImageDraw, fonts: Fonts, hint: str
) -> None:
    """Single-line hint at the bottom of the screen."""
    y0 = theme.SCREEN_H - theme.FOOTER_H
    draw.rectangle(
        [(0, y0), (theme.SCREEN_W - 1, theme.SCREEN_H - 1)],
        fill=theme.FOOTER_BG,
    )
    draw.line(
        [(0, y0 - 1), (theme.SCREEN_W - 1, y0 - 1)], fill=theme.SEPARATOR
    )
    draw.text(
        (theme.PAD_X, y0 + 3), hint, font=fonts.small, fill=theme.FOOTER_FG
    )


def _kv_row(
    draw: ImageDraw.ImageDraw,
    fonts: Fonts,
    y: int,
    label: str,
    value: str,
    *,
    value_color: tuple[int, int, int] = theme.FG,
    label_w: int = 64,
) -> None:
    """Label on the left, value on the right of an x divider at label_w."""
    draw.text((theme.PAD_X, y), label, font=fonts.body, fill=theme.FG_DIM)
    draw.text(
        (theme.PAD_X + label_w, y), value, font=fonts.body, fill=value_color
    )


# ── Per-screen renderers ─────────────────────────────────────────────


def _render_home(state: UISnapshot, fonts: Fonts) -> Image.Image:
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "HOME", state)

    callsign_color = theme.FG if state.callsign != "N0CALL" else theme.FG_BAD
    grid_display = state.grid if state.grid else "(unset)"
    grid_color = theme.FG if state.grid else theme.FG_BAD
    freq_mhz = state.freq_hz / 1_000_000.0

    # GPS-derived fields
    gps_label, gps_color = _gps_status_label(state)
    # Time itself is now in the header banner on every screen — HOME
    # keeps just the source tag so the operator can distinguish UTC
    # (chrony-synced from GPS/NTP) from CONSENSUS (radio-derived
    # median-dt fallback) from --- (no source, TX blocked). The
    # header's color signals confidence at a glance; this row spells
    # out which source is currently driving slot timing.
    time_src_label, time_src_color = _time_source_label(state)

    y = theme.BODY_Y0 + 2
    _kv_row(draw, fonts, y, "Call", state.callsign, value_color=callsign_color)
    y += 18
    # Grid: show configured grid, with GPS-derived in parentheses if
    # different (so the operator can see at a glance "I'm in a different
    # grid than I told the radio").
    grid_text = grid_display
    if state.gps_grid and state.grid and state.gps_grid != state.grid:
        grid_text = f"{grid_display} (gps {state.gps_grid})"
    elif state.gps_grid and not state.grid:
        # Configured grid is unset but GPS knows where we are
        grid_text = f"(unset; gps {state.gps_grid})"
    _kv_row(draw, fonts, y, "Grid", grid_text, value_color=grid_color)
    y += 18
    _kv_row(draw, fonts, y, "TimeSrc", time_src_label, value_color=time_src_color)
    y += 18
    _kv_row(draw, fonts, y, "GPS", gps_label, value_color=gps_color)
    y += 18
    _kv_row(draw, fonts, y, "Freq", f"{freq_mhz:.3f} MHz", value_color=theme.FG)
    y += 18
    # CAT status — green when rigctld is connected and we can transmit,
    # dim "--" when not. Without CAT we can only receive.
    cat_label = "CONNECTED" if state.cat_connected else "--"
    cat_color = theme.FG_GOOD if state.cat_connected else theme.FG_DIM
    _kv_row(draw, fonts, y, "CAT", cat_label, value_color=cat_color)
    y += 18
    # Phase 6 §6.11: battery row. Shows "NN% [↑/↓/=]" where the glyph
    # encodes status. Color follows the §6.11 thresholds:
    #   - FG_BAD (red)   : discharging at ≤ 5% — TX is gated
    #   - FG_WARN (amber): discharging at ≤ 15% — operator should charge
    #   - FG (white)     : everywhere else (charging, healthy, full)
    # When the battery snapshot is None (no fuel gauge or sustained
    # read failure), we render '--' rather than hiding the row.
    if state.battery is None:
        batt_label = "--"
        batt_color = theme.FG_DIM
    else:
        b = state.battery
        batt_label = f"{b.capacity}% {b.status_glyph}"
        if b.is_critical:
            batt_color = theme.FG_BAD
        elif b.is_low:
            batt_color = theme.FG_WARN
        else:
            batt_color = theme.FG
    _kv_row(draw, fonts, y, "Batt", batt_label, value_color=batt_color)

    # Inbox indicator — only rendered when there's something to
    # show, so the operator's eye is drawn to it. Format:
    #   "Inbox  3 unread / 2 held"      (both non-zero)
    #   "Inbox  3 unread"               (only unread)
    #   "Inbox  2 held for others"      (only held)
    # Held = STORE rows we're holding for someone else; unread =
    # UNREAD rows addressed to us. The Phase 4 (Compose) work will
    # add a "+ N delivering" segment once outbound mailbox tracking
    # exists.
    has_unread = state.inbox_unread_count > 0
    has_held = state.inbox_held_count > 0
    if has_unread or has_held:
        y += 18
        if has_unread and has_held:
            label = (
                f"{state.inbox_unread_count} unread "
                f"/ {state.inbox_held_count} held"
            )
        elif has_unread:
            label = f"{state.inbox_unread_count} unread"
        else:
            label = f"{state.inbox_held_count} held for others"
        # Yellow when there are unread (calls operator's attention).
        # Dim when only holding for others (no action required).
        color = theme.FG_WARN if has_unread else theme.FG_DIM
        _kv_row(draw, fonts, y, "Inbox", label, value_color=color)

    if state.emergency_override:
        if state.gps.has_position:
            _draw_footer(draw, fonts, "EMERGENCY MODE — TX ARMED")
        else:
            _draw_footer(draw, fonts, "EMERGENCY MODE — awaiting GPS fix")
    elif not state.tx_allowed:
        _draw_footer(draw, fonts, "TX disabled — set callsign + grid in Setup")
    else:
        _draw_footer(draw, fonts, "← →  cycle screens")
    return img


def _gps_status_label(state: UISnapshot) -> tuple[str, tuple[int, int, int]]:
    """Map GPS fix state to a short label + color for display.

    Shows ``acquiring`` (amber) when the fix kind is 2D/3D but the
    receiver hasn't produced a real position yet. This happens during
    cold-start: gpsd asserts a mode-3 fix as soon as it has the time
    + ephemeris pieces, but the actual coordinates take several more
    seconds to compute. Without this distinction, HOME would
    confidently say "3D fix (6 sat)" while EMERGENCY would show
    "no position" — operator gets mixed signals about whether the
    station is location-ready. The amber acquiring state signals
    "fix declared, position pending" so both screens stay consistent.
    """
    from microjs8.gps.types import FixKind  # local to avoid cycle
    kind = state.gps.kind
    sats = state.gps.satellites_used
    sat_suffix = f" ({sats} sat)" if sats is not None else ""
    if kind == FixKind.FIX_3D:
        if state.gps.has_position:
            return f"3D fix{sat_suffix}", theme.FG_GOOD
        return f"acquiring{sat_suffix}", theme.FG_WARN
    if kind == FixKind.FIX_2D:
        if state.gps.has_position:
            return f"2D fix{sat_suffix}", theme.FG_WARN
        return f"acquiring{sat_suffix}", theme.FG_WARN
    if kind == FixKind.NO_FIX:
        return "no fix" + sat_suffix, theme.FG_DIM
    return "unknown", theme.FG_DIM


def _time_source_label(state: UISnapshot) -> tuple[str, tuple[int, int, int]]:
    """Return (label, color) for the HOME screen's TimeSrc row.

    The header banner now shows the actual time on every screen — this
    helper is just for the source tag, which the operator uses to
    diagnose timing issues:

      - "UTC" — chrony is synced (GPS-disciplined or NTP-peer); slot
                timing is authoritative. TX allowed.
      - "CONSENSUS" — chrony is unavailable but we have radio-derived
                median-dt from observed decodes; slot timing is good
                enough. TX allowed.
      - "NONE" — neither chrony nor consensus is usable; slot
                timing is unknown. TX is blocked. Color FG_DIM so
                operator sees something is wrong at a glance.
    """
    src = state.time_source
    if src == "consensus":
        return "CONSENSUS", theme.FG
    if src == "chrony":
        return "UTC", theme.FG
    return "NONE", theme.FG_DIM


def _render_heard(state: UISnapshot, fonts: Fonts) -> Image.Image:
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "HEARD", state)

    # Column header row
    cols = theme.HEARD_COL_X
    headers = ("CALL", "SNR", "GRID", "MI", "AZ")
    y = theme.BODY_Y0 + 2
    for x, h in zip(cols, headers):
        draw.text((x, y), h, font=fonts.small, fill=theme.FG_DIM)
    y += 14
    draw.line(
        [(0, y), (theme.SCREEN_W - 1, y)], fill=theme.SEPARATOR
    )
    y += 4

    # Render up to HEARD_ROWS_VISIBLE actual heard stations.
    now = time.time()
    rows = state.heard[: theme.HEARD_ROWS_VISIBLE]
    if not rows:
        draw.text(
            (theme.PAD_X, y + 6),
            "No stations heard yet.",
            font=fonts.body, fill=theme.FG_DIM,
        )
        draw.text(
            (theme.PAD_X, y + 28),
            "Tune to 7.078 MHz USB",
            font=fonts.small, fill=theme.FG_DIM,
        )
        draw.text(
            (theme.PAD_X, y + 42),
            "and wait for the next slot.",
            font=fonts.small, fill=theme.FG_DIM,
        )
        _draw_footer(draw, fonts, "← →  cycle screens")
        return img

    for station in rows:
        color = _age_color(now - station.last_heard)
        # Render each column. Numbers right-justified for readability;
        # callsign left-justified.
        snr = f"{station.snr_db:+03d}" if station.snr_db is not None else " --"
        grid = (station.grid or "----")[:4]
        if station.distance_mi is not None:
            d = int(round(station.distance_mi))
            mi = f"{d:5d}"[:5]
        else:
            mi = "  ---"
        if station.bearing_deg is not None:
            az = f"{int(round(station.bearing_deg)):03d}"
        else:
            az = "---"
        cells = (station.callsign[:8], snr, grid, mi.lstrip(), az)
        for x, cell in zip(cols, cells):
            draw.text((x, y), cell, font=fonts.body_mono, fill=color)
        y += theme.HEARD_ROW_H
        if y > theme.BODY_Y1 - theme.HEARD_ROW_H:
            break

    _draw_footer(draw, fonts, f"{len(state.heard)} heard · ← → cycle")
    return img


# Age thresholds for the "freshness" coloring (per Step 5 spec):
#   <30 min  → green  (active)
#   30 min–4 h → yellow (recent)
#   >4 h    → gray   (stale)
_AGE_GREEN_S = 30 * 60
_AGE_YELLOW_S = 4 * 3600


def _age_color(age_seconds: float) -> tuple[int, int, int]:
    if age_seconds < _AGE_GREEN_S:
        return theme.FG_GOOD
    if age_seconds < _AGE_YELLOW_S:
        return theme.FG_WARN
    return theme.FG_DIM


def _render_directed(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """Render the directed-activity log (chat-style).

    Shows the chronological back-and-forth of protocol-level directed
    exchanges between our station and remote stations: SNR?, INFO,
    GRID?, QUERY MSGS, QUERY MSG <id>, ACKs, etc. MSG / MSG TO:
    content lives in the INBOX screen — this view is for the
    surrounding protocol activity.

    Layout per row:
      ▸ KD8PGB QUERY MSGS               -10 dB     ← inbound (green ▸, SNR)
      ◂ KD8PGB MSG 5                    12:35      ← outbound (white ◂, time)

    Newest entry at the bottom (matches operator's natural reading
    flow: read top-to-bottom and the most recent activity is what
    they care about most).

    Empty state: helpful message so first-boot operators know what
    the screen is for.
    """
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "DIRECTED", state)

    entries = state.directed_log_entries
    if not entries:
        y = theme.BODY_Y0 + 8
        draw.text(
            (theme.PAD_X, y),
            "No directed protocol",
            font=fonts.body, fill=theme.FG_DIM,
        )
        y += 22
        draw.text(
            (theme.PAD_X, y),
            "activity yet.",
            font=fonts.body, fill=theme.FG_DIM,
        )
        y += 28
        draw.text(
            (theme.PAD_X, y),
            "SNR?, QUERY MSGS, ACKs",
            font=fonts.small, fill=theme.FG_DIM,
        )
        y += 14
        draw.text(
            (theme.PAD_X, y),
            "appear here.",
            font=fonts.small, fill=theme.FG_DIM,
        )
        _draw_footer(draw, fonts, "← →  cycle screens")
        return img

    # ── List view ────────────────────────────────────────────────
    #
    # Newest at bottom. Bodies that don't fit on one line WRAP onto
    # continuation lines indented under the body (not the chevron),
    # so a long "HEARTBEAT SNR -09 MSG ID 1" or a free-text reply
    # like "thanks for the heartbeat, how's propagation tonight?"
    # stays fully readable. Operators treat this view as live chat,
    # not just protocol summary, so we never ellipsize body content.
    #
    # Per-entry layout (single line):
    #   ▸ KI4HDU HEARTBEAT SNR -9              -13 dB
    #
    # Per-entry layout (wrapped):
    #   ▸ KD8GIJ thanks for the heartbeat,        -21 dB
    #     how's propagation tonight?
    #   ◂ K1ABC ACK                                12:35
    row_h = 16
    body_height = theme.BODY_Y1 - theme.BODY_Y0 - 8
    max_total_rows = max(1, body_height // row_h)
    arrow_x = theme.PAD_X
    text_x = theme.PAD_X + 14
    meta_x_right = theme.SCREEN_W - theme.PAD_X
    # Width budget for the FIRST line of an entry (room for chevron
    # on the left, meta column on the right). Continuation lines get
    # the full body-text width since they have no meta column.
    cont_text_w = meta_x_right - text_x

    # Greedy word-wrap helper. Used per-entry: first call sizes for
    # the first-line budget (which depends on the meta width), then
    # subsequent calls (if any) use the continuation budget.
    def _wrap_body(s: str, first_w: int, cont_w: int) -> list[str]:
        s = (s or "").strip()
        if not s:
            return [""]
        out: list[str] = []
        words = s.split()
        cur = ""
        budget = first_w
        for w in words:
            candidate = w if not cur else (cur + " " + w)
            try:
                px = int(draw.textlength(candidate, font=fonts.small))
            except Exception:
                px = 6 * len(candidate)
            if px <= budget:
                cur = candidate
                continue
            # Word doesn't fit on the current line.
            if cur:
                out.append(cur)
                cur = ""
                budget = cont_w
                # Retry: does the word fit on a fresh line?
                try:
                    pw = int(draw.textlength(w, font=fonts.small))
                except Exception:
                    pw = 6 * len(w)
                if pw <= budget:
                    cur = w
                    continue
            # Single word longer than the line: break on chars.
            remainder = w
            while remainder:
                # Find the longest prefix that fits.
                lo, hi = 1, len(remainder)
                fit = 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    try:
                        wm = int(draw.textlength(
                            remainder[:mid], font=fonts.small
                        ))
                    except Exception:
                        wm = 6 * mid
                    if wm <= budget:
                        fit = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                out.append(remainder[:fit])
                remainder = remainder[fit:]
                budget = cont_w
        if cur:
            out.append(cur)
        return out or [""]

    # Pass 1: build per-entry wrapped-line lists, newest first, until
    # we've accumulated enough lines to fill the screen. Each entry's
    # first-line budget depends on its meta width (right-aligned SNR
    # or time stamp), so we compute meta + first-line budget per
    # entry before wrapping.
    rendered: list[tuple] = []   # (entry, lines, meta, meta_w, is_in)
    rows_consumed = 0
    for entry in reversed(entries):
        is_in = (entry.direction.value == "IN")
        if is_in and entry.snr_db is not None:
            meta = f"{entry.snr_db:+d} dB"
        elif not is_in:
            meta = _format_unix_hhmm(entry.at_unix)
        else:
            meta = ""
        try:
            meta_w = int(draw.textlength(meta, font=fonts.small)) if meta else 0
        except Exception:
            meta_w = 6 * len(meta)
        # First-line budget: text_x → meta_left, with a 4-px gap.
        first_w = (meta_x_right - meta_w - 4) - text_x
        if first_w < 30:
            first_w = 30
        # Compose the body string. "CALL VERB body" with body
        # possibly empty (e.g. ACK with no body). When the entry was
        # addressed to a JS8Call group we belong to (rather than to
        # us personally), tag the sender with the group affiliation
        # so the operator distinguishes a group blast from personal
        # traffic at a glance. The K1ABC@@ARESGA double-'@' form
        # reads cleanly: "K1ABC speaking to the @ARESGA group". The
        # group tag is part of the sender label, not a separate
        # token, so word-wrap keeps it adjacent.
        sender = entry.other_call
        if entry.for_group:
            sender = f"{sender}@{entry.for_group}"
        if entry.body:
            full = f"{sender} {entry.verb} {entry.body}"
        else:
            full = f"{sender} {entry.verb}"
        lines = _wrap_body(full, first_w, cont_text_w)
        # Cap the wrap at the screen height so one runaway entry
        # can't dominate; truncate with ellipsis on the last line
        # when capped.
        if rows_consumed + len(lines) > max_total_rows:
            keep = max_total_rows - rows_consumed
            if keep <= 0:
                break
            if keep < len(lines):
                # Mark the last visible line with an ellipsis so the
                # operator sees that content was cut.
                lines = lines[:keep]
                if lines:
                    last = lines[-1]
                    ellipsis = "…"
                    # Trim to make room for the ellipsis.
                    while last and True:
                        try:
                            w_last = int(draw.textlength(
                                last + ellipsis, font=fonts.small
                            ))
                        except Exception:
                            w_last = 6 * (len(last) + 1)
                        budget = first_w if len(lines) == 1 else cont_text_w
                        if w_last <= budget:
                            break
                        last = last[:-1]
                    lines[-1] = (last + ellipsis) if last else ellipsis
        rendered.append((entry, lines, meta, meta_w, is_in))
        rows_consumed += len(lines)
        if rows_consumed >= max_total_rows:
            break

    # Pass 2: render top-to-bottom in NEWEST-FIRST order. Operator
    # wanted newest at the top of the body region, pushing older
    # entries downward (news-feed style, not chat-style). Pass 1
    # built ``rendered`` newest-first by iterating ``reversed(entries)``
    # so we render directly without re-reversing.
    y = theme.BODY_Y0 + 6

    for entry, lines, meta, meta_w, is_in in rendered:
        # First line: chevron + body + meta.
        arrow = "▸" if is_in else "◂"
        arrow_color = theme.FG_GOOD if is_in else theme.FG
        draw.text((arrow_x, y), arrow, font=fonts.body, fill=arrow_color)
        body_fill = theme.FG if is_in else theme.FG_BAD
        draw.text((text_x, y), lines[0], font=fonts.small, fill=body_fill)
        if meta:
            draw.text(
                (meta_x_right - meta_w, y),
                meta, font=fonts.small, fill=theme.FG_DIM,
            )
        y += row_h
        # Continuation lines: indent at text_x, no chevron, no meta.
        for cont_line in lines[1:]:
            draw.text(
                (text_x, y), cont_line,
                font=fonts.small, fill=body_fill,
            )
            y += row_h
        if y > theme.BODY_Y1 - 2:
            break

    # Footer hint: count + nav. Total reflects what's IN THE LOG, not
    # just what fits on screen — operators want to know if the buffer
    # is filling up.
    n_total = len(entries)
    if n_total >= 200:  # bumping near cap
        footer = f"{n_total} entries (cap) · ← →"
    else:
        footer = f"{n_total} entries · ← →"
    _draw_footer(draw, fonts, footer)
    return img


def _format_unix_hhmm(at_unix: float) -> str:
    """Format a Unix timestamp as ``HH:MM`` in UTC.

    UTC is the right choice here because it matches the timestamps
    JS8Call uses everywhere (slot boundaries are UTC). Local-time
    formatting would require knowing the operator's timezone, which
    on a Pi without RTC is often wrong until NTP syncs.

    Failures (negative, NaN, far-future) silently fall back to
    "--:--" so a corrupt entry doesn't break the rest of the list.
    """
    try:
        import time as _time
        gm = _time.gmtime(at_unix)
        return f"{gm.tm_hour:02d}:{gm.tm_min:02d}"
    except Exception:
        return "--:--"


def _render_inbox(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """Render the inbox / mailbox screen.

    List view of UNREAD + READ rows from inbox_messages, newest first.
    Focus chevron on the focused row, bold for UNREAD (calls
    attention), dim for READ. Enter on focused → detail view (handled
    by router; this renderer just shows the focus state).

    Empty state: a friendly help message so first-boot operators know
    what the screen is for.
    """
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "INBOX", state)

    if not state.inbox_messages:
        y = theme.BODY_Y0 + 8
        draw.text(
            (theme.PAD_X, y),
            "No messages addressed",
            font=fonts.body, fill=theme.FG_DIM,
        )
        y += 22
        draw.text(
            (theme.PAD_X, y),
            f"to {state.callsign} yet.",
            font=fonts.body, fill=theme.FG_DIM,
        )
        y += 28
        draw.text(
            (theme.PAD_X, y),
            "Inbound MSG arrives here",
            font=fonts.small, fill=theme.FG_DIM,
        )
        y += 14
        draw.text(
            (theme.PAD_X, y),
            "automatically.",
            font=fonts.small, fill=theme.FG_DIM,
        )
        if state.inbox_held_count:
            y += 22
            draw.text(
                (theme.PAD_X, y),
                f"({state.inbox_held_count} held for others)",
                font=fonts.small, fill=theme.FG_DIM,
            )
        _draw_footer(draw, fonts, "← →  cycle screens")
        return img

    # ── List view ────────────────────────────────────────────────
    #
    # Each row takes ~32px (header line + body line). At 240px tall
    # body region we can show ~6 rows. A focused row gets a chevron
    # at the leading edge AND a slightly different color (theme.FG
    # rather than theme.FG_DIM) so the operator knows what Enter
    # will open. Unread rows are white (theme.FG); read rows are
    # dim (theme.FG_DIM).
    y = theme.BODY_Y0 + 4
    visible_rows = state.inbox_messages[:6]
    focused = state.inbox_focused_index
    chev_x = theme.PAD_X
    text_x = theme.PAD_X + 12  # leave room for the chevron

    for i, row in enumerate(visible_rows):
        is_focused = (i == focused)

        # Read/unread color — UNREAD pops, READ recedes.
        if row.is_read:
            color = theme.FG_DIM
        else:
            color = theme.FG

        # Focused row gets a chevron and a slight emphasis upgrade
        # if it's already FG_DIM (so READ messages still highlight
        # when focused). For UNREAD, the chevron alone is enough —
        # the row is already at full intensity.
        if is_focused:
            draw.text((chev_x, y), "▸", font=fonts.body, fill=theme.FG_GOOD)
            if row.is_read:
                color = theme.FG  # promote to full white when focused

        # Header line: "FROM_CALL  -SNR  HH:MM"
        # Format SNR: skip when missing (local-store rows have no SNR).
        snr_text = f"{row.snr_db:+03d}" if row.snr_db is not None else " . "
        ts = _format_inbox_time(row.utc_iso)
        hdr = f"{row.from_call:<8s} {snr_text}  {ts}"
        draw.text((text_x, y), hdr, font=fonts.body_mono, fill=color)
        y += 14

        # Body — single-line preview, truncated with ellipsis
        body = row.body
        # Per-row body wrap. The DIRECTED screen rows are rendered in
        # FONT_SMALL; on the 320×170 CardputerZero panel that fits about
        # 38 characters comfortably (vs MiniJS8's 28 on the narrower
        # 240px-wide panel). Anything longer truncates with an ellipsis;
        # the operator can hit Enter to open the full INBOX_DETAIL view
        # for buffered MSG bodies.
        max_chars = 38
        wrapped = body if len(body) <= max_chars else body[: max_chars - 1] + "…"
        draw.text((text_x + 4, y), wrapped, font=fonts.small, fill=color)
        y += 18
        if y > theme.BODY_Y1 - 16:
            break

    n_total = len(state.inbox_messages)
    n_unread = state.inbox_unread_count
    if n_unread > 0:
        footer = f"{n_unread} unread / {n_total} · Enter read · Del delete"
    else:
        footer = f"{n_total} messages · Enter read · Del delete"
    _draw_footer(draw, fonts, footer)
    return img


def _format_inbox_time(utc_iso: str) -> str:
    """Render an ISO-8601 timestamp as ``HH:MM`` for the inbox list.

    Failures (empty or malformed) silently fall back to "--:--" so a
    bad row doesn't break the rest of the list rendering.
    """
    if not utc_iso:
        return "--:--"
    try:
        # ISO 8601 from MailboxStore looks like "2026-05-06T14:23:11.000+00:00".
        # Pull H:M directly from positions 11-16 to avoid the cost of
        # full datetime.fromisoformat() on every render.
        if len(utc_iso) >= 16 and utc_iso[10] in ("T", " "):
            return utc_iso[11:16]
    except Exception:
        pass
    return "--:--"


def _render_inbox_detail(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """Full-screen detail view of one inbox message.

    Entered via Enter on a focused row in the inbox list. The header
    shows from-call + timestamp + SNR; the body wraps to fit the
    panel width. Esc returns to the list (handled in router).

    If the detail-id doesn't match any current row (race: the row
    was deleted while we were viewing), we degrade to a friendly
    "(message no longer available)" message rather than crashing.
    """
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "MESSAGE", state)

    # Look up the row to show. inbox_detail_id should always be
    # set when we're in INBOX_DETAIL screen, but be defensive.
    target_id = state.inbox_detail_id
    target_row = None
    if target_id is not None:
        for row in state.inbox_messages:
            if row.id == target_id:
                target_row = row
                break

    if target_row is None:
        y = theme.BODY_Y0 + 16
        draw.text(
            (theme.PAD_X, y),
            "(message no longer",
            font=fonts.body, fill=theme.FG_DIM,
        )
        y += 22
        draw.text(
            (theme.PAD_X, y),
            "available)",
            font=fonts.body, fill=theme.FG_DIM,
        )
        _draw_footer(draw, fonts, "Esc  return")
        return img

    # ── Header section ───────────────────────────────────────────
    # FROM:  KC1WDO
    # AT:    2026-05-06 14:23 UTC
    # SNR:   -3 dB
    y = theme.BODY_Y0 + 4
    _kv_row(
        draw, fonts, y, "From",
        target_row.from_call or "?",
        value_color=theme.FG, label_w=44,
    )
    y += 22
    _kv_row(
        draw, fonts, y, "At",
        _format_inbox_full_time(target_row.utc_iso),
        value_color=theme.FG, label_w=44,
    )
    y += 22
    if target_row.snr_db is not None:
        _kv_row(
            draw, fonts, y, "SNR",
            f"{target_row.snr_db:+d} dB",
            value_color=theme.FG, label_w=44,
        )
        y += 22
    # Spacer line.
    y += 6

    # ── Body section ─────────────────────────────────────────────
    # Wrap the body to the panel width, leaving the footer reserved.
    # Word-wrap if possible; if a single token is longer than the
    # max line, hard-wrap it. ~42 monospace characters fits the
    # 320px-wide CardputerZero body region with body_mono font (vs
    # 32 on MiniJS8's 240px panel).
    body_text = target_row.body or "(empty body)"
    lines = _wrap_message_body(body_text, max_chars=42)
    body_color = theme.FG
    body_y = y
    max_y = theme.BODY_Y1 - 16
    truncated = False
    for line in lines:
        if body_y + 16 > max_y:
            truncated = True
            break
        draw.text(
            (theme.PAD_X, body_y), line,
            font=fonts.body_mono, fill=body_color,
        )
        body_y += 16
    if truncated:
        # Indicate that more content was clipped. (Future enhancement:
        # ↑/↓ scrolls within the detail view; for now we show a hint.)
        draw.text(
            (theme.PAD_X, max_y - 14), "… (more)",
            font=fonts.small, fill=theme.FG_DIM,
        )

    _draw_footer(draw, fonts, "Esc  return")
    return img


def _format_inbox_full_time(utc_iso: str) -> str:
    """Render ISO timestamp as 'YYYY-MM-DD HH:MM UTC' for the detail view.

    Falls back to the raw string on parse failure rather than ?''s,
    so the operator at least sees the data.
    """
    if not utc_iso:
        return "(unknown)"
    if len(utc_iso) >= 16 and utc_iso[10] in ("T", " "):
        return f"{utc_iso[:10]} {utc_iso[11:16]} UTC"
    return utc_iso


def _wrap_message_body(text: str, max_chars: int) -> list[str]:
    """Word-wrap ``text`` to lines no longer than ``max_chars`` chars.

    Prefers word boundaries; if a single word is longer than max_chars
    it gets hard-broken. Result is suitable for the detail-view body
    region.
    """
    out: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        line = ""
        for word in paragraph.split(" "):
            if not word:
                continue
            # Hard-break a too-long word.
            while len(word) > max_chars:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:max_chars])
                word = word[max_chars:]
            candidate = (line + " " + word).strip() if line else word
            if len(candidate) <= max_chars:
                line = candidate
            else:
                if line:
                    out.append(line)
                line = word
        if line:
            out.append(line)
    return out


def _render_compose(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """COMPOSE screen: TO / CMD / TEXT / SEND with field focus.

    Layout:

      ┌────────────────────────────────────────┐
      │ COMPOSE                  UTC HH:MM:SS  │
      ├────────────────────────────────────────┤
      │  TO    [K1ABC▎          ]              │ ← cursor when focused
      │  CMD    ▾ FREE          (↑↓ to cycle)  │
      │  TEXT  ┌────────────────────────────┐  │
      │        │ hello dave!▎               │  │
      │        └────────────────────────────┘  │
      │              [   SEND   ]              │ ← inverted when focused
      ├────────────────────────────────────────┤
      │ Tab next · Enter send · Esc cancel     │
      └────────────────────────────────────────┘

    Focus styling:
      - The focused field's label is bright green (FG_GOOD), unfocused
        labels are dim (FG_DIM). Same convention used on the SETUP
        screen so the operator's eye is already trained.
      - Text fields show a small caret (▎) at the end of the buffer
        when focused; absent when not.
      - The CMD field has ↕ glyphs flanking the value when focused
        (visual hint that ↑/↓ cycle the dropdown).
      - The SEND button is rendered with an inverted background
        (HEADER_BG fill, FG_GOOD text) when focused — looks like a
        physical button you'd press.

    Footer hint adapts to focused field so the operator always
    knows what their next keypress will do.
    """
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "COMPOSE", state)

    focused = state.compose_focused_field
    label_w = 44

    # Phase 4: 320×170 retune. The TO/CMD/TEXT/SEND stack used to fit
    # comfortably in MiniJS8's 192px body; the CardputerZero's 128px
    # body forces tighter spacing and a 2-line TEXT box (was 3). The
    # TEXT label is now inline-left of the box (matching the docstring
    # diagram above) so we don't burn a full row on the label.
    y = theme.BODY_Y0 + 4

    # ── TO row ──────────────────────────────────────────────────────
    is_to_focus = (focused == "compose_to")
    label_color = theme.FG_GOOD if is_to_focus else theme.FG_DIM
    draw.text((theme.PAD_X, y), "TO", font=fonts.body, fill=label_color)
    # Value box: bordered when focused for clarity, plain when not.
    box_x0 = theme.PAD_X + label_w
    box_x1 = theme.SCREEN_W - theme.PAD_X
    if is_to_focus:
        draw.rectangle(
            [(box_x0 - 2, y - 2), (box_x1, y + 16)],
            outline=theme.FG_GOOD,
        )
    to_text = state.compose_to or ""
    if is_to_focus:
        to_text = to_text + "▎"  # visible cursor at the end
    draw.text(
        (box_x0, y),
        to_text,
        font=fonts.body,
        fill=theme.FG if state.compose_to else theme.FG_DIM,
    )
    y += 18

    # ── CMD row ─────────────────────────────────────────────────────
    is_cmd_focus = (focused == "compose_cmd")
    label_color = theme.FG_GOOD if is_cmd_focus else theme.FG_DIM
    draw.text((theme.PAD_X, y), "CMD", font=fonts.body, fill=label_color)
    cmd = state.compose_cmd
    cmd_label = cmd.value if cmd.value else "(free)"
    if is_cmd_focus:
        # Surround with ↕ glyphs to hint that ↑/↓ cycle.
        cmd_text = f"↕ {cmd_label}"
    else:
        cmd_text = cmd_label
    if is_cmd_focus:
        draw.rectangle(
            [(box_x0 - 2, y - 2), (box_x1, y + 16)],
            outline=theme.FG_GOOD,
        )
    draw.text(
        (box_x0, y),
        cmd_text,
        font=fonts.body,
        fill=theme.FG,
    )
    y += 18

    # ── TEXT row ────────────────────────────────────────────────────
    # Label is inline-left of the box (saves the 18px the prior layout
    # spent on a separate label row). 2 visible lines instead of 3.
    is_text_focus = (focused == "compose_text")
    label_color = theme.FG_GOOD if is_text_focus else theme.FG_DIM
    draw.text((theme.PAD_X, y), "TEXT", font=fonts.body, fill=label_color)
    text_box_y0 = y
    text_box_y1 = text_box_y0 + 36           # 2 visible lines @ 14 pt + padding
    box = (box_x0 - 2, text_box_y0 - 2, theme.SCREEN_W - theme.PAD_X, text_box_y1)
    draw.rectangle(
        box,
        outline=theme.FG_GOOD if is_text_focus else theme.SEPARATOR,
    )
    text_value = state.compose_text or ""
    inner_x = box[0] + 4
    inner_y_top = box[1] + 4
    inner_w = (box[2] - box[0]) - 8
    line_h = 16  # body 14 pt + 2 px leading
    n_lines_visible = max(1, (box[3] - box[1] - 8) // line_h)

    # Word-wrap helper. Greedy: pack words onto a line until adding
    # the next word would exceed inner_w; then start a new line.
    # A single word longer than inner_w is broken character-by-
    # character (rare for callsign-and-body messages, but defensive).
    def _wrap_to_lines(s: str, with_caret: bool) -> list[str]:
        if not s and not with_caret:
            return [""]
        if with_caret:
            s = s + "▎"
        # Treat existing newlines as hard line breaks (currently no
        # way to type one, but defensive for future multi-line
        # paste/import).
        out: list[str] = []
        for paragraph in s.split("\n"):
            words = paragraph.split(" ")
            cur = ""
            for w_i, word in enumerate(words):
                # The space we'd add before this word (none for first).
                sep = "" if cur == "" else " "
                candidate = cur + sep + word
                try:
                    cw = int(draw.textlength(candidate, font=fonts.body))
                except Exception:
                    cw = 8 * len(candidate)
                if cw <= inner_w:
                    cur = candidate
                    continue
                # Would overflow. If cur has content, flush it; else
                # the word itself is wider than inner_w, break char-
                # by-char.
                if cur:
                    out.append(cur)
                    cur = ""
                # Pack chars from `word` until we'd overflow, then
                # break the line; loop with remainder.
                remainder = word
                while remainder:
                    i = len(remainder)
                    while i > 0:
                        try:
                            piece_w = int(draw.textlength(
                                remainder[:i], font=fonts.body))
                        except Exception:
                            piece_w = 8 * i
                        if piece_w <= inner_w:
                            break
                        i -= 1
                    if i <= 0:  # safety: even 1 char doesn't fit
                        i = 1
                    if i == len(remainder):
                        cur = remainder
                        remainder = ""
                    else:
                        out.append(remainder[:i])
                        remainder = remainder[i:]
            out.append(cur)
        return out or [""]

    lines = _wrap_to_lines(text_value, with_caret=is_text_focus)
    # Scroll: keep last N lines visible (the operator sees what
    # they've most recently typed).
    if len(lines) > n_lines_visible:
        lines = lines[-n_lines_visible:]
    # Render
    iy = inner_y_top
    body_color = theme.FG if state.compose_text else theme.FG_DIM
    for line in lines:
        draw.text((inner_x, iy), line, font=fonts.body, fill=body_color)
        iy += line_h
    y = text_box_y1 + 4


    # ── SEND button ─────────────────────────────────────────────────
    is_send_focus = (focused == "compose_send")
    btn_w = 96
    btn_h = 18
    btn_x0 = (theme.SCREEN_W - btn_w) // 2
    btn = (btn_x0, y, btn_x0 + btn_w, y + btn_h)
    if is_send_focus:
        # Filled inverted button — looks pressable.
        draw.rectangle(btn, fill=theme.HEADER_BG, outline=theme.FG_GOOD)
        send_color = theme.FG_GOOD
    else:
        draw.rectangle(btn, outline=theme.FG_DIM)
        send_color = theme.FG_DIM
    try:
        send_w = int(draw.textlength("SEND", font=fonts.body))
    except Exception:
        send_w = 40
    send_x = btn_x0 + (btn_w - send_w) // 2
    send_y = y + (btn_h - 14) // 2 - 1
    draw.text((send_x, send_y), "SEND", font=fonts.body, fill=send_color)
    y = btn[3] + 2

    # ── TX-state hint ───────────────────────────────────────────────
    # If the operator presses SEND, the message will enqueue. Whether
    # it actually fires depends on (a) tx_allowed (station configured
    # / emergency override), (b) timing consensus (chrony OR ≥3
    # decoded frames), and (c) TO != our own callsign (gfsk8 strips
    # the leading callsign when it matches ours, producing a
    # malformed transmission — see build_compose_wire's SELF-CALL
    # note). Surface the most-pressing gate so the operator knows
    # what to expect before pressing SEND.
    tx_warning: Optional[str] = None
    to_upper = (state.compose_to or "").strip().upper()
    own_upper = (state.callsign or "").strip().upper()
    if to_upper and own_upper and to_upper == own_upper:
        # Self-target — would silently produce a malformed frame.
        # Highest-priority warning since it's a content/protocol error
        # the operator can fix immediately.
        tx_warning = "TO cannot be your own call"
    elif not state.tx_allowed:
        tx_warning = "TX OFF — configure station"
    elif (
        state.battery is not None
        and state.battery.is_critical
        and not state.emergency_override
    ):
        # Phase 6 §6.11: battery ≤ 5% blocks TX in the normal path.
        # The emergency override (help beacon) is exempt — life-
        # safety traffic transmits regardless of battery state — so
        # we explicitly check `not state.emergency_override` here to
        # mirror the same exemption that TxSafetyGate applies.
        tx_warning = "TX OFF — battery critical"
    elif not state.time_source:
        # No chrony, no consensus — scheduler won't fire until 3+
        # frames are decoded OR chrony comes back. The compose will
        # enqueue and wait.
        tx_warning = "queued — awaiting time sync"
    if tx_warning:
        try:
            w = int(draw.textlength(tx_warning, font=fonts.small))
        except Exception:
            w = 6 * len(tx_warning)
        draw.text(
            ((theme.SCREEN_W - w) // 2, y),
            tx_warning, font=fonts.small, fill=theme.FG_WARN,
        )

    # ── Footer hint, contextual to focus ────────────────────────────
    if is_to_focus:
        hint = "type call · Tab next · Esc cancel"
    elif is_cmd_focus:
        hint = "↑↓ cycle · Tab next · Esc cancel"
    elif is_text_focus:
        hint = "type · Bksp del · Tab next · Esc cancel"
    elif is_send_focus:
        hint = "Enter send · Tab next · Esc cancel"
    else:
        hint = "Tab pick field · Esc cancel"
    _draw_footer(draw, fonts, hint)
    return img


def _render_allcall(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """ALLCALL screen — three-row menu (HEARTBEAT / QUERY MSGS / CQ).

    The focused row is highlighted with the accent background so the
    operator knows which row Enter will act on. The HEARTBEAT row's
    value reads the live mode from ``state.hb_mode`` and tints amber-
    coloured (FG_WARN) when not OFF — the same convention the HOME
    HB row uses for "this is actively transmitting on a schedule",
    catching the eye without screaming.

    Phase 4 320×170 layout: 3 rows × 32 px row_h = 96 px content;
    body region is 130 px (170 − HEADER_H − FOOTER_H − seps), so
    fits with margin.
    """
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "ALLCALL", state)
    y = theme.BODY_Y0 + 12
    items = (
        ("HEARTBEAT",  state.hb_mode.value),
        ("QUERY MSGS", "send"),
        ("CQ",         "send"),
    )
    focus = state.allcall_focus
    row_h = 32
    for i, (label, value) in enumerate(items):
        is_focused = (i == focus)
        # Selection highlight — subtle dark-blue background band.
        if is_focused:
            draw.rectangle(
                [(0, y - 4), (theme.SCREEN_W, y + row_h - 12)],
                fill=theme.ACCENT_BG,
            )
        # Label: accent when focused for unambiguous "this is selected".
        label_color = theme.ACCENT if is_focused else theme.FG
        draw.text(
            (theme.PAD_X + 4, y),
            label, font=fonts.body, fill=label_color,
        )
        if value:
            try:
                vw = int(draw.textlength(value, font=fonts.body))
            except Exception:
                vw = 7 * len(value)
            # HEARTBEAT row's value color signals active mode at a
            # glance: FG_WARN (amber) when running, FG_DIM when OFF.
            # Other rows ("send") use FG_DIM consistently.
            if i == 0:
                value_color = (
                    theme.FG_WARN
                    if state.hb_mode is not HbMode.OFF
                    else theme.FG_DIM
                )
            else:
                value_color = theme.FG_DIM
            draw.text(
                (theme.SCREEN_W - vw - theme.PAD_X - 4, y),
                value, font=fonts.body, fill=value_color,
            )
        y += row_h
    _draw_footer(draw, fonts, "↑ ↓ pick · Enter invoke")
    return img


def _render_hb_mode_select(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """HB_MODE_SELECT modal sub-screen — pick the heartbeat cadence.

    Renders a 4-row dropdown over the HbMode values. The currently-
    active mode (state.hb_mode) is shown with a thin amber underline;
    the focused row (state.hb_select_focus) has the accent-bg highlight.
    This way the operator sees BOTH "where I am" and "what's running
    now" simultaneously — crucial for the operator who paused mid-
    selection and forgot which mode the beacon is actually on.

    Phase 4 320×170 layout: 4 rows × 26 px row_h = 104 px content
    (header 24 + body 130 = 154; 24 + 12 + 104 = 140, fits with
    14 px footer margin).
    """
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "HEARTBEAT MODE", state)
    y = theme.BODY_Y0 + 8
    row_h = 26
    focus = state.hb_select_focus
    for i, mode in enumerate(HB_MODES_ORDERED):
        is_focused = (i == focus)
        is_active = (mode is state.hb_mode)
        if is_focused:
            draw.rectangle(
                [(0, y - 4), (theme.SCREEN_W, y + row_h - 10)],
                fill=theme.ACCENT_BG,
            )
        label_color = theme.ACCENT if is_focused else theme.FG
        draw.text(
            (theme.PAD_X + 4, y),
            mode.value, font=fonts.body, fill=label_color,
        )
        # Active-mode indicator: dim amber dot at right edge so the
        # operator never has to guess which mode is currently running.
        if is_active:
            dot_x = theme.SCREEN_W - theme.PAD_X - 8
            draw.ellipse(
                [(dot_x - 4, y + 4), (dot_x + 4, y + 12)],
                fill=theme.FG_WARN,
            )
        y += row_h
    _draw_footer(draw, fonts, "↑ ↓ pick · Enter commit · Esc cancel")
    return img


def _render_directed_menu(state: UISnapshot, fonts: Fonts) -> Image.Image:
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "DIRECTED MENU", state)
    y = theme.BODY_Y0 + 4
    # Selected callsign at the top
    draw.text(
        (theme.PAD_X, y),
        "Target: (none selected)",
        font=fonts.body,
        fill=theme.FG_DIM,
    )
    y += 22
    draw.line(
        [(0, y), (theme.SCREEN_W - 1, y)], fill=theme.SEPARATOR
    )
    y += 4
    actions = ("MSG", "STORE", "AGN?", "SNR?", "GRID", "QUERY", "MYLOC")
    for act in actions:
        draw.text(
            (theme.PAD_X + 4, y), act, font=fonts.body, fill=theme.FG
        )
        y += 18
    _draw_footer(draw, fonts, "Pick from Heard List first (Step 3)")
    return img


def _render_emergency(state: UISnapshot, fonts: Fonts) -> Image.Image:
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "EMERGENCY", state)
    # Body — large bold "SEND HELP" centered, then position display.
    text = "SEND HELP"
    bbox = fonts.large_bold.getbbox(text)
    tw = bbox[2] - bbox[0]
    draw.text(
        ((theme.SCREEN_W - tw) // 2, theme.BODY_Y0 + 6),
        text,
        font=fonts.large_bold,
        fill=theme.FG_BAD,
    )
    y = theme.BODY_Y0 + 6 + (bbox[3] - bbox[1]) + 8

    # ── Identity ────────────────────────────────────────────────────
    # Phase 12: ALWAYS show the operator's callsign. Earlier versions
    # only showed N0CALL when emergency_override was True, but
    # emergency_override is a one-way flag — once tripped it stays
    # True even after the operator configures their callsign in
    # SETUP. The result was that configured stations displayed
    # "ID: N0CALL (unconfigured)" which is wrong and unhelpful.
    # Source of truth is now state.callsign:
    #   - Non-empty → show the actual callsign in red (alerting)
    #   - Empty → show "N0CALL (unconfigured)" — only true when
    #     the operator hasn't set a callsign at all.
    has_callsign = bool(state.callsign and state.callsign != "N0CALL")
    draw.text((theme.PAD_X, y), "ID:", font=fonts.body, fill=theme.FG_DIM)
    if has_callsign:
        draw.text(
            (theme.PAD_X + 32, y), state.callsign,
            font=fonts.body, fill=theme.FG_BAD,
        )
    else:
        draw.text(
            (theme.PAD_X + 32, y), "N0CALL (unconfigured)",
            font=fonts.body, fill=theme.FG_BAD,
        )
    y += 20

    # ── Position ────────────────────────────────────────────────────
    # Phase 12: GPS lat/lon is preferred when available — it's the
    # most actionable thing a rescuer can act on. Fall back to the
    # configured grid only when no fix is available, mark stale with
    # "(grid)" so the operator knows it's not live. Phase 9's
    # has_position flag handles the gpsd null-island case (mode-3
    # fix declared but coordinates still 0,0); we never render those
    # as valid coords.
    draw.text((theme.PAD_X, y), "Pos:", font=fonts.body, fill=theme.FG_DIM)
    if (
        state.gps.has_position
        and state.gps.lat is not None
        and state.gps.lon is not None
    ):
        pos = f"{state.gps.lat:+.4f}, {state.gps.lon:+.4f}"
        draw.text(
            (theme.PAD_X + 32, y), pos,
            font=fonts.body_mono, fill=theme.FG_GOOD,
        )
    elif state.grid:
        draw.text(
            (theme.PAD_X + 32, y), f"{state.grid} (grid)",
            font=fonts.body, fill=theme.FG,
        )
    else:
        draw.text(
            (theme.PAD_X + 32, y), "no position",
            font=fonts.body, fill=theme.FG_BAD,
        )
    y += 24

    # ── Three visual states ─────────────────────────────────────────
    # 1) HOLDING (in-progress arm or disarm): progress bar + label
    # 2) ARMED (idle, beacon TXing): "Beacon: ARMED" red text
    # 3) IDLE: "Beacon: not armed" dim text
    hold_in_progress = (
        state.emergency_hold_progress is not None
        and state.emergency_hold_direction is not None
    )

    if hold_in_progress:
        # Progress bar — 3-second countdown drains from full to empty.
        direction = state.emergency_hold_direction
        label = "Arming…" if direction == "arm" else "Disarming…"
        draw.text(
            (theme.PAD_X, y), label,
            font=fonts.body, fill=theme.FG_BAD,
        )
        y += 18
        # Bar background (dim) and fill (red while arming, dim-grey
        # while disarming — visual distinction matches the action).
        bar_x0 = theme.PAD_X
        bar_x1 = theme.SCREEN_W - theme.PAD_X
        bar_y0 = y
        bar_h = 12
        bar_y1 = bar_y0 + bar_h
        draw.rectangle(
            [(bar_x0, bar_y0), (bar_x1, bar_y1)],
            outline=theme.FG_DIM,
        )
        progress_w = int(
            (bar_x1 - bar_x0 - 2)
            * max(0.0, min(1.0, state.emergency_hold_progress or 0.0))
        )
        bar_fill = theme.FG_BAD if direction == "arm" else theme.FG_DIM
        draw.rectangle(
            [
                (bar_x0 + 1, bar_y0 + 1),
                (bar_x0 + 1 + progress_w, bar_y1 - 1),
            ],
            fill=bar_fill,
        )
        y += bar_h + 4
        # Footer — cancel hint.
        if direction == "arm":
            _draw_footer(draw, fonts, "Hold ENTER 3 s · ESC cancels")
        else:
            _draw_footer(draw, fonts, "Hold ESC 3 s · ENTER cancels")
    elif state.emergency_beacon_armed:
        # Armed state — clear, attention-grabbing red.
        draw.text(
            (theme.PAD_X, y), "Beacon:",
            font=fonts.body, fill=theme.FG_DIM,
        )
        draw.text(
            (theme.PAD_X + 70, y), "ARMED",
            font=fonts.body, fill=theme.FG_BAD,
        )
        y += 20
        # Sub-text: what's happening on the air.
        draw.text(
            (theme.PAD_X, y), "TX SOS every 3 min",
            font=fonts.small, fill=theme.FG_DIM,
        )
        _draw_footer(draw, fonts, "Hold ESC 3 s to disarm")
    else:
        # Idle state — beacon not armed.
        if has_callsign or state.emergency_override:
            # Configured station OR emergency-bypass mode — can arm.
            draw.text(
                (theme.PAD_X, y), "Beacon: not armed",
                font=fonts.small, fill=theme.FG_DIM,
            )
            _draw_footer(draw, fonts, "Hold ENTER 3 s to arm")
        else:
            # Unconfigured AND no emergency-bypass — operator must
            # bypass before the gesture will succeed. Tell them what
            # to do rather than letting them hammer ENTER fruitlessly.
            draw.text(
                (theme.PAD_X, y), "Configure callsign in SETUP",
                font=fonts.small, fill=theme.FG_WARN,
            )
            y += 14
            draw.text(
                (theme.PAD_X, y), "or use Emergency Bypass",
                font=fonts.small, fill=theme.FG_WARN,
            )
            _draw_footer(draw, fonts, "TX blocked: no callsign")
    return img


def _render_setup(state: UISnapshot, fonts: Fonts) -> Image.Image:
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "SETUP", state)

    # Each row: label, displayed value (or current edit buffer), color.
    # Focus highlights with a left chevron + accent color; edit mode
    # adds a caret at the end of the buffer.
    rows = _setup_rows(state)

    y = theme.BODY_Y0 + 6
    row_h = 22
    label_w = 64

    for field_name, label, value, value_color in rows:
        is_focused = (state.focused_field == field_name) and not state.editing_field
        is_editing = state.editing_field == field_name

        # Focus chevron / row highlight
        if is_focused:
            draw.rectangle(
                [(0, y - 2), (theme.SCREEN_W - 1, y + row_h - 4)],
                fill=theme.ACCENT_BG,
            )
        if is_editing:
            draw.rectangle(
                [(0, y - 2), (theme.SCREEN_W - 1, y + row_h - 4)],
                outline=theme.ACCENT,
            )

        # Label
        label_color = theme.FG if is_focused else theme.FG_DIM
        draw.text((theme.PAD_X, y), label, font=fonts.body, fill=label_color)

        # Value (or live edit buffer + caret)
        if is_editing:
            buf = state.edit_buffer
            color = theme.FG_BAD if state.edit_invalid else theme.ACCENT
            draw.text(
                (theme.PAD_X + label_w, y), buf + "_",
                font=fonts.body, fill=color,
            )
        else:
            draw.text(
                (theme.PAD_X + label_w, y), value,
                font=fonts.body, fill=value_color,
            )
        y += row_h

    # ── Emergency bypass row ────────────────────────────────────────
    # A labeled, focusable button at the bottom of the Setup screen.
    # Activated by Tab-into-it then Enter (no hold gesture).
    is_em_focused = (
        state.focused_field == "emergency_bypass" and not state.editing_field
    )
    em_y = y + 8
    if is_em_focused:
        draw.rectangle(
            [(theme.PAD_X, em_y), (theme.SCREEN_W - theme.PAD_X, em_y + 24)],
            fill=theme.EMERGENCY_BG,
        )
        em_color = theme.EMERGENCY_FG
    else:
        draw.rectangle(
            [(theme.PAD_X, em_y), (theme.SCREEN_W - theme.PAD_X, em_y + 24)],
            outline=theme.FG_BAD,
        )
        em_color = theme.FG_BAD
    em_text = "[EMERGENCY BEACON →]"
    bbox = fonts.body.getbbox(em_text)
    tw = bbox[2] - bbox[0]
    draw.text(
        ((theme.SCREEN_W - tw) // 2, em_y + 5),
        em_text,
        font=fonts.body,
        fill=em_color,
    )

    # Footer
    if state.editing_field:
        if state.edit_invalid:
            _draw_footer(draw, fonts, "invalid value · Enter retry · Esc cancel")
        else:
            _draw_footer(draw, fonts, "type · Enter save · Esc cancel")
    elif not state.tx_allowed:
        _draw_footer(draw, fonts, "Tab field · Enter edit · set Call+Grid first")
    else:
        _draw_footer(draw, fonts, "Tab field · Enter edit · ← → ring nav")
    return img


def _setup_rows(state: UISnapshot) -> list[tuple[str, str, str, tuple[int, int, int]]]:
    """Build the (field_name, label, displayed_value, color) tuples."""
    callsign_color = theme.FG if state.callsign != "N0CALL" else theme.FG_BAD
    grid_color = theme.FG if state.grid else theme.FG_BAD
    grid_display = state.grid if state.grid else "(unset)"
    # Groups row: displays the comma-separated configured groups, or
    # "(none)" in dim grey when empty. The display value is what the
    # operator types — comma-separated with the '@' prefix on each.
    # Dim grey for empty (not red): groups are optional, an empty
    # list isn't a misconfiguration. Layout fits the same row height
    # as the other Setup rows (Phase 4 reworked screens.py for the
    # 320×170 panel — see header notes in this file).
    if state.groups:
        groups_display = ", ".join(state.groups)
        groups_color = theme.FG
    else:
        groups_display = "(none)"
        groups_color = theme.FG_DIM
    freq_mhz = state.freq_hz / 1_000_000.0
    # Radio row: show the human-readable display name from the
    # registry. Pressing Enter on this row cycles to the next radio,
    # saves config.toml, and exits — systemd's Restart=always brings
    # the daemon back up with the new radio path active. Always-
    # consistent: what you see is what's running.
    try:
        from microjs8.cat.radios import get_radio
        radio_label = get_radio(state.radio_id).display_name
    except Exception:
        # Unknown id (shouldn't reach here — config validation
        # rejects unknowns). Show the raw id as a defensive fallback.
        radio_label = state.radio_id
    return [
        ("callsign", "Call",   state.callsign,                      callsign_color),
        ("grid",     "Grid",   grid_display,                        grid_color),
        ("groups",   "Groups", groups_display,                      groups_color),
        ("units",    "Units",  state.units,                         theme.FG),
        # Step 6: editable. Frequency goes through CAT to the radio.
        ("freq_hz",  "Freq",   f"{freq_mhz:.3f} MHz",               theme.FG),
        ("radio",    "Radio",  radio_label,                         theme.FG),
        ("mode",     "Mode",   state.mode,                          theme.FG_DIM),
        ("logs",     "Logs",   "30 days",                           theme.FG_DIM),
    ]


def _render_shutting_down(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """Confirmation screen during the Fn+Q press-and-hold countdown.

    Phase 4: layout reworked to fit 320×170 — every coordinate now
    derives from theme.BODY_Y0/BODY_Y1 instead of the prior hardcoded
    values that assumed a 240×240 panel. Also pulls SHUTDOWN_HOLD_S
    from the gesture module so the displayed seconds-left always
    matches the real timer (Phase 3 dropped this from 5 s to 3 s).
    """
    # Local import keeps the module-load order clean: ui/screens.py
    # doesn't depend on input/* otherwise, and a top-level import
    # would create an apparent (though not actual) cross-package
    # dependency that's harder to reason about.
    from microjs8.input.shutdown_gesture import SHUTDOWN_HOLD_S

    img, draw = _new_canvas()
    # No header / footer here — full-bleed for clarity under stress.

    title = "SHUTTING DOWN"
    bbox = fonts.title.getbbox(title)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    # Top of title: ~25% of body from the top, leaving room for the
    # subtitle, progress bar, and seconds-left text below.
    title_y = theme.BODY_Y0 + (theme.BODY_H // 5)
    draw.text(
        ((theme.SCREEN_W - tw) // 2, title_y),
        title,
        font=fonts.title,
        fill=theme.FG,
    )
    sub = "release Fn+Q to cancel"
    bbox = fonts.small.getbbox(sub)
    sw = bbox[2] - bbox[0]
    sub_y = title_y + th + 6
    draw.text(
        ((theme.SCREEN_W - sw) // 2, sub_y),
        sub,
        font=fonts.small,
        fill=theme.FG_DIM,
    )

    # Progress bar — empties from full to zero across SHUTDOWN_HOLD_S.
    # Anchored relative to BODY_Y1 so it stays just above the bottom
    # edge regardless of panel height.
    bar_h = 14
    bar_y = theme.BODY_Y1 - 28          # leaves room for seconds-left text below
    bar_x0 = 24
    bar_x1 = theme.SCREEN_W - 24
    bar_w = bar_x1 - bar_x0
    draw.rectangle(
        [(bar_x0, bar_y), (bar_x1, bar_y + bar_h)], outline=theme.FG_DIM
    )
    fill_px = int(bar_w * state.shutdown_remaining)
    if fill_px > 0:
        draw.rectangle(
            [(bar_x0, bar_y), (bar_x0 + fill_px, bar_y + bar_h)],
            fill=theme.FG_BAD,
        )

    seconds_left = state.shutdown_remaining * SHUTDOWN_HOLD_S
    txt = f"{seconds_left:.1f} s"
    bbox = fonts.body.getbbox(txt)
    tw = bbox[2] - bbox[0]
    draw.text(
        ((theme.SCREEN_W - tw) // 2, bar_y + bar_h + 2),
        txt,
        font=fonts.body,
        fill=theme.FG_DIM,
    )
    return img


# ── Dispatch ─────────────────────────────────────────────────────────


_RENDERERS: dict[Screen, Callable[[UISnapshot, Fonts], Image.Image]] = {
    Screen.HOME:           _render_home,
    Screen.HEARD:          _render_heard,
    Screen.DIRECTED:       _render_directed,    # activity-log chat view
    Screen.INBOX:          _render_inbox,       # mailbox / MSG content view
    Screen.COMPOSE:        _render_compose,
    Screen.ALLCALL:        _render_allcall,
    Screen.DIRECTED_MENU:  _render_directed_menu,
    Screen.EMERGENCY:      _render_emergency,
    Screen.SETUP:          _render_setup,
    Screen.SHUTTING_DOWN:  _render_shutting_down,
    Screen.INBOX_DETAIL:   _render_inbox_detail,
    Screen.HB_MODE_SELECT: _render_hb_mode_select,
}


def render(state: UISnapshot, fonts: Fonts) -> Image.Image:
    """Render the screen indicated by ``state.screen``.

    Catches and logs renderer exceptions, returning a fallback "render
    error" frame instead of crashing the daemon. A misrendered field
    is bad; a render thread that dies in a way that takes the radio
    offline is far worse.
    """
    renderer = _RENDERERS.get(state.screen)
    if renderer is None:
        return _render_error(state, fonts, f"unknown screen {state.screen}")
    try:
        return renderer(state, fonts)
    except Exception as exc:
        _log.exception("renderer for %s raised", state.screen.name)
        return _render_error(state, fonts, str(exc))


def _render_error(
    state: UISnapshot, fonts: Fonts, message: str
) -> Image.Image:
    img, draw = _new_canvas()
    _draw_header(draw, fonts, "RENDER ERROR", state)
    draw.text(
        (theme.PAD_X, theme.BODY_Y0 + 8),
        "screen render failed",
        font=fonts.body,
        fill=theme.FG_BAD,
    )
    # Wrap the message at ~30 chars.
    y = theme.BODY_Y0 + 30
    line = ""
    for word in message.split():
        if len(line) + len(word) + 1 > 30:
            draw.text(
                (theme.PAD_X, y), line, font=fonts.small, fill=theme.FG_DIM
            )
            y += 14
            line = word
        else:
            line = f"{line} {word}".strip()
        if y > theme.BODY_Y1 - 14:
            break
    if line and y <= theme.BODY_Y1 - 14:
        draw.text(
            (theme.PAD_X, y), line, font=fonts.small, fill=theme.FG_DIM
        )
    _draw_footer(draw, fonts, "see journalctl -u microjs8")
    return img
