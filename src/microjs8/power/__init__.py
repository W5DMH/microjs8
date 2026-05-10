"""microjs8.power — battery and backlight, both via Linux sysfs.

Phase 3 introduces this subpackage with the backlight controller.
Phase 6 adds ``battery``.
"""

from microjs8.power.backlight import Backlight

__all__ = ["Backlight"]
