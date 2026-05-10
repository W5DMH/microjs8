"""microjs8.power — battery and backlight, both via Linux sysfs.

Phase 3 introduced this subpackage with the backlight controller.
Phase 6 added the BQ27220 fuel-gauge reader.
"""

from microjs8.power.backlight import Backlight
from microjs8.power.battery import (
    BatteryReader,
    BatteryState,
    CRITICAL_BATTERY_PCT,
    LOW_BATTERY_PCT,
)

__all__ = [
    "Backlight",
    "BatteryReader",
    "BatteryState",
    "CRITICAL_BATTERY_PCT",
    "LOW_BATTERY_PCT",
]
