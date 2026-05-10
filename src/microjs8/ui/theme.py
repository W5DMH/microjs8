"""Theme constants for the MicroJS8 UI.

All colors, sizes, and layout numbers in one place so a future visual
refresh is a one-file edit. RGB tuples (Pillow uses RGB order natively;
the ST7789 driver does the RGB->RGB565 conversion when we hand it a PIL
image).

Sizes are tuned for a 240x240 portrait IPS panel. Vertical column layout:

    +----------------------------------+   <- y=0
    |  HEADER  (screen title, status)  |   <- HEADER_H = 28
    +----------------------------------+
    |                                  |
    |          BODY  (per-screen)      |   <- 240 - HEADER_H - FOOTER_H
    |                                  |
    +----------------------------------+
    |  FOOTER  (hint / progress)       |   <- FOOTER_H = 18
    +----------------------------------+   <- y=240

A solid line at the header/body and body/footer boundaries gives the
eye an anchor when the ring rotates between screens.
"""

from __future__ import annotations

from typing import Final

# Panel dimensions
SCREEN_W: Final = 240
SCREEN_H: Final = 240

# Header / footer reservations
HEADER_H: Final = 28
FOOTER_H: Final = 18
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
FONT_TITLE: Final = 18    # header text
FONT_BODY: Final = 14     # main body text
FONT_SMALL: Final = 11    # footer hints, secondary detail
FONT_CLOCK: Final = 14    # header clock — 75% of title, bold to match
FONT_LARGE: Final = 28    # emergency / shutdown banners

# Heard-list column layout — chosen to fit a 240px-wide panel with
# the FONT_BODY monospaced font (each glyph ~7 px wide).
# Columns:  CALL  SNR  GRID  MI  AZ
#           7ch   3ch  4ch   4ch 4ch  -> 22 chars total + 4 spaces ~= 178 px
# Leaving a comfortable margin at each side.
HEARD_COL_X: Final = (
    4,    # CALL    (start)
    72,   # SNR
    104,  # GRID
    148,  # MI
    188,  # AZ
)
HEARD_ROW_H: Final = 18      # per row including spacing
HEARD_ROWS_VISIBLE: Final = 11   # body height / row height, rounded down
