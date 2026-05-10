"""Font loading for MicroJS8.

Loads PIL ``ImageFont`` instances once at startup so screen renderers
don't pay the open-and-parse cost on every redraw. We use the DejaVu
fonts that ship with Raspberry Pi OS Bookworm — both ``DejaVuSans`` and
``DejaVuSansMono`` are guaranteed present at the paths below by the
``fonts-dejavu-core`` package, which is pulled in by Bookworm Lite's
default selection.

If a font file is somehow missing (corrupt SD, stripped image), we fall
back to PIL's built-in bitmap font and log a warning. This keeps the
display working — ugly, but legible — rather than crashing the daemon.

The monospaced font is used for the Heard List columns and any
fixed-width data; the proportional font is used everywhere else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

from microjs8.ui import theme

_log = logging.getLogger(__name__)

# Paths shipped by the fonts-dejavu-core package on Debian/RPiOS.
_PROP_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
_PROP_BOLD_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
_MONO_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
_MONO_BOLD_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")


@dataclass(frozen=True)
class Fonts:
    """Bundle of pre-loaded fonts handed to every screen renderer."""

    title: ImageFont.ImageFont
    body: ImageFont.ImageFont
    body_mono: ImageFont.ImageFont
    small: ImageFont.ImageFont
    clock: ImageFont.ImageFont          # header UTC clock — bold, ~75% title
    large_bold: ImageFont.ImageFont


def _load_truetype(path: Path, size: int, label: str) -> ImageFont.ImageFont:
    """Load a TrueType font with a graceful fallback to PIL's default."""
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError as exc:
        _log.warning(
            "could not load %s (%s) at %s — using PIL default bitmap font",
            label,
            exc,
            path,
        )
        return ImageFont.load_default()


def load_fonts() -> Fonts:
    """Load and cache all fonts. Call once at app start."""
    return Fonts(
        title=_load_truetype(_PROP_BOLD_PATH, theme.FONT_TITLE, "title font"),
        body=_load_truetype(_PROP_PATH, theme.FONT_BODY, "body font"),
        body_mono=_load_truetype(_MONO_PATH, theme.FONT_BODY, "monospace body font"),
        small=_load_truetype(_PROP_PATH, theme.FONT_SMALL, "small font"),
        # Clock uses the bold proportional face (same as title) so the
        # two read as a matched pair — the operator's eye recognizes
        # them as part of the same banner. 14 pt is ~75% of the 18 pt
        # title, large enough to read at glance distance.
        clock=_load_truetype(_PROP_BOLD_PATH, theme.FONT_CLOCK, "clock font"),
        large_bold=_load_truetype(_PROP_BOLD_PATH, theme.FONT_LARGE, "large font"),
    )
