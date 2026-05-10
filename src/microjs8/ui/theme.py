"""Theme constants for the MicroJS8 UI.

All colors, sizes, and layout numbers in one place so a future visual
refresh is a one-file edit. RGB tuples (Pillow uses RGB order natively;
the framebuffer driver does the RGB->RGB565 conversion when we hand it a
PIL image).

Sizes are tuned for the M5Stack CardputerZero's built-in **320×170**
ST7789v3 LCD (Phase 4 retarget — the prior MiniJS8 layout was 240×240).
Vertical column layout:

    +----------------------------------+   <- y=0
    |  HEADER  (screen title, status)  |   <- HEADER_H = 24
    +----------------------------------+
    |                                  |
    |          BODY  (per-screen)      |   <- 170 - HEADER_H - FOOTER_H
    |                                  |
    +----------------------------------+
    |  FOOTER  (hint / progress)       |   <- FOOTER_H = 16
    +----------------------------------+   <- y=170

A solid line at the header/body and body/footer boundaries gives the
eye an anchor when the ring rotates between screens.

Phase 4 sizing rationale (vs MiniJS8 240×240):
  - HEADER_H 28→24: FONT_TITLE=18 renders ~22px tall in PIL with
    DejaVuSans-Bold; HEADER_H=24 leaves a 2px margin. Saves 4px.
  - FOOTER_H 18→16: FONT_SMALL=11 renders ~13-14px; FOOTER_H=16
    leaves a 2-3px margin. Saves 2px.
  - HEARD_ROW_H 18→16: FONT_BODY=14 renders ~17px monospaced. The
    HEARD columns (CALL/SNR/GRID/MI/AZ) contain no descender-bearing
    glyphs (no g/p/q/y), so a 1px tight fit on the row height is
    acceptable and earns one more visible row.
  - HEARD_COL_X redistributed for 320px width with ~26px gaps —
    more comfortable than MiniJS8's 240px-cramped layout.
  - FONT_LARGE 28→24: a 28px banner consumes 16% of a 170px screen
    height — too dominant. 24 is still legible at glance distance.
"""

from __future__ import annotations

from typing import Final

# Panel dimensions (CardputerZero ST7789v3 320×170)
SCREEN_W: Final = 320
SCREEN_H: Final = 170

# Header / footer reservations
HEADER_H: Final = 24
FOOTER_H: Final = 16
BODY_Y0: Final = HEADER_H + 1   # +1 for separator line
BODY_Y1: Final = SCREEN_H - FOOTER_H - 1
BODY_H: Final = BODY_Y1 - BODY_Y0

# Padding from screen edges
PAD_X: Final = 4
PAD_Y: Final = 2

# Colors (RGB).
BG: Final          = (0, 0, 0)         # body background
HEADER_BG: Final   = (0, 32, 64)       # dark navy
HEADER_FG: Final   = (220, 220, 220)
FOOTER_BG: Final   = (16, 16, 16)
FOOTER_FG: Final   = (140, 140, 140)
SEPARATOR: Final   = (60, 60, 60)

FG: Final          = (220, 220, 220)   # body primary text
FG_DIM: Final      = (140, 140, 140)   # body secondary text / placeholders
FG_GOOD: Final     = (60, 200, 80)     # GPS lock, TX-allowed, etc.
FG_WARN: Final     = (240, 180, 40)    # caution
FG_BAD: Final      = (220, 60, 60)     # not configured, error states

ACCENT: Final      = (60, 160, 220)    # focused field, selected row
ACCENT_BG: Final   = (20, 60, 100)     # selected-row background

EMERGENCY_BG: Final = (140, 0, 0)      # full-screen emergency banner
EMERGENCY_FG: Final = (255, 255, 255)

# Font sizes (px, used by fonts.py)
FONT_TITLE: Final = 18    # header text — bold proportional
FONT_BODY: Final = 14     # main body text
FONT_SMALL: Final = 11    # footer hints, secondary detail
FONT_CLOCK: Final = 14    # header clock — same size as body, bold so it
                          # reads as a paired companion to the title font
FONT_LARGE: Final = 24    # emergency / shutdown banners (was 28; reduced
                          # so a single banner glyph doesn't dominate the
                          # short 170px panel height)

# Heard-list column layout — chosen to fit the 320px-wide CardputerZero
# panel with the FONT_BODY monospaced font (DejaVuSansMono at 14pt is
# approximately 8.4 px wide per glyph).
# Columns:  CALL  SNR  GRID  MI  AZ
#           8ch   3ch  4ch   4ch 4ch  -> 23 chars total
# Total content: ~184 px. Available body width: SCREEN_W - 2*PAD_X = 312.
# Slack of 128 px is distributed as ~26 px gaps + right margin.
HEARD_COL_X: Final = (
    4,    # CALL    (start, 8 chars wide ~ 64 px → ends at 68)
    90,   # SNR     (3 chars ~ 24 px → ends at 114)
    138,  # GRID    (4 chars ~ 32 px → ends at 170)
    196,  # MI      (4 chars ~ 32 px → ends at 228)
    254,  # AZ      (4 chars ~ 32 px → ends at 286, right margin 34 px)
)
HEARD_ROW_H: Final = 16      # per row including spacing
# Visible rows: (BODY_H - 14 column-header advance - 2 top pad) / HEARD_ROW_H
# = (128 - 14 - 2) / 16 = 7 rows. Step down from MiniJS8's 11 — direct
# consequence of the shorter 170px panel.
HEARD_ROWS_VISIBLE: Final = (BODY_H - 16) // HEARD_ROW_H
