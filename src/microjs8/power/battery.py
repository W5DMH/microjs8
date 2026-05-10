"""BQ27220 battery fuel-gauge reader for the M5Stack CardputerZero.

The CardputerZero ships a TI BQ27220 fuel gauge wired to the I2C bus.
The kernel's power_supply driver exposes it under
``/sys/class/power_supply/<name>/`` where ``<name>`` is platform-
dependent — common patterns:

  - ``bq27220-0`` (numeric suffix from the I2C addr)
  - ``bq27220``
  - ``bq27220-battery``

This module auto-discovers the directory by scanning for any name
containing ``bq27`` (case-insensitive). If multiple match (rare;
would need two batteries), the lowest-sorted name wins for
determinism — see ``find_battery_dir``.

Layers, mirroring the Phase 5 display module:

  1. ``find_battery_dir`` + ``read_battery_state`` — pure functions
     that take an injectable root, so tests use ``tmp_path``
     instead of touching real ``/sys``.

  2. ``BatteryReader`` — a long-running asyncio task that polls
     once per second, mutates ``UIState.battery``, and triggers
     dirty so the render thread redraws the HOME battery row.

The async approach (vs a thread) is deliberate: sysfs reads are
synchronous and fast (~microseconds), and mutating ``UIState``
happens on the asyncio loop anyway per the project's threading
contract. Spinning up a thread for 1 Hz file reads is wasted
plumbing.

Thresholds for the §6.11 HOME row coloring + TX gate are defined
here as constants so a config-toml override (deferred to a later
phase) has a single place to look.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

_log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────


DEFAULT_POWER_SUPPLY_DIR: Final = Path("/sys/class/power_supply")
DEFAULT_POLL_INTERVAL_S: Final = 1.0
DEFAULT_NAME_FRAGMENT: Final = "bq27"

# §6.11 thresholds. Intentionally on this module rather than scattered
# at every check-site so a future config override has one place to
# look. The 5%/15% pair are the proposed defaults from the build spec;
# can be overridden in [battery] of /var/microjs8/config.toml in a
# later phase (open question §1 of the build spec).
LOW_BATTERY_PCT: Final = 15
CRITICAL_BATTERY_PCT: Final = 5


# Kernel power_supply status enum — these are the canonical strings
# the driver writes to /sys/.../status. We don't try to handle every
# possible value, just the ones our UI cares about.
STATUS_CHARGING: Final = "Charging"
STATUS_DISCHARGING: Final = "Discharging"
STATUS_FULL: Final = "Full"
STATUS_NOT_CHARGING: Final = "Not charging"
STATUS_UNKNOWN: Final = "Unknown"


# ── BatteryState dataclass ───────────────────────────────────────────


@dataclass(frozen=True)
class BatteryState:
    """One snapshot of the fuel gauge.

    Field units match the kernel power_supply class semantics:

      - ``capacity``      — integer percent 0..100
      - ``voltage_v``     — float volts (kernel reports microvolts;
                            we divide by 1e6 here)
      - ``current_ma``    — float milliamps; signed, NEGATIVE while
                            discharging per Linux convention. Absent
                            from some BQ27 drivers; defaults to 0.0
                            if missing.
      - ``temperature_c`` — float degrees Celsius (kernel reports
                            tenths of a degree; we divide by 10).
                            Defaults to nan when sensor isn't
                            exposed.
      - ``status``        — raw kernel string ("Charging" /
                            "Discharging" / "Full" / etc.).
    """

    capacity: int
    voltage_v: float
    current_ma: float
    temperature_c: float
    status: str

    # ── Derived flags (cheap properties, no caching needed) ────────

    @property
    def is_charging(self) -> bool:
        return self.status == STATUS_CHARGING

    @property
    def is_discharging(self) -> bool:
        return self.status == STATUS_DISCHARGING

    @property
    def is_full(self) -> bool:
        return self.status == STATUS_FULL

    @property
    def is_low(self) -> bool:
        """Discharging AND ≤ ``LOW_BATTERY_PCT`` — HOME row goes amber.

        Charging doesn't trigger 'low' even at 14% — the operator is
        actively addressing the situation. Same logic for ``is_critical``.
        """
        return self.is_discharging and self.capacity <= LOW_BATTERY_PCT

    @property
    def is_critical(self) -> bool:
        """Discharging AND ≤ ``CRITICAL_BATTERY_PCT`` — TX is gated.

        Per §6.11, the help-beacon path is exempt from this gate;
        ``TxSafetyGate.check_can_transmit`` checks
        ``snap.emergency_override`` before applying the cutoff.
        """
        return self.is_discharging and self.capacity <= CRITICAL_BATTERY_PCT

    @property
    def status_glyph(self) -> str:
        """One-character glyph for the HOME row.

        Maps to:
          - "↑"  while charging
          - "↓"  while discharging
          - "="  when full / 'Not charging' / charge-complete
          - "?"  when status is unknown or unreadable
        """
        if self.is_charging:
            return "↑"
        if self.is_discharging:
            return "↓"
        if self.is_full or self.status == STATUS_NOT_CHARGING:
            return "="
        return "?"


# ── Discovery ────────────────────────────────────────────────────────


def find_battery_dir(
    *,
    base_path: Path = DEFAULT_POWER_SUPPLY_DIR,
    name_fragment: str = DEFAULT_NAME_FRAGMENT,
) -> Optional[Path]:
    """Locate the BQ27 battery directory under ``base_path``.

    Scans direct children of ``base_path`` for any whose name contains
    ``name_fragment`` (case-insensitive). Returns the lowest-sorted
    matching path, or ``None`` if nothing matches or ``base_path``
    doesn't exist.

    The fragment match (rather than an exact-name match) handles the
    several conventional names the kernel might pick — see module
    docstring. The case-insensitive comparison is defensive: some
    older kernels uppercase the chip name.
    """
    if not base_path.is_dir():
        # No /sys/class/power_supply at all means the kernel was
        # built without power_supply support, or we're in a stripped
        # container. Either way: silently no battery — the caller
        # treats this as "no fuel gauge" and the UI shows '--'.
        return None

    needle = name_fragment.lower()
    candidates: list[Path] = []
    try:
        for child in base_path.iterdir():
            if not child.is_dir():
                continue
            if needle in child.name.lower():
                candidates.append(child)
    except OSError as exc:
        _log.warning("cannot scan %s: %s", base_path, exc)
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name)
    if len(candidates) > 1:
        # Multiple BQ27 chips on the same bus is highly unusual but
        # we tolerate it and pick deterministically. Worth a warning
        # so a misconfigured DT shows up in the journal.
        _log.warning(
            "multiple BQ27 batteries detected: %s; using %s",
            [p.name for p in candidates], candidates[0].name,
        )
    return candidates[0]


# ── State read ───────────────────────────────────────────────────────


def _read_int_file(path: Path) -> int:
    """Read a stripped integer from ``path``.

    Raises ``RuntimeError`` with the path embedded if anything goes
    wrong. The single error type makes ``read_battery_state``'s
    error handling uniform.
    """
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"battery: cannot read int from {path}: {exc}") from exc


def _read_str_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"battery: cannot read str from {path}: {exc}") from exc


def read_battery_state(battery_dir: Path) -> BatteryState:
    """Read one fresh snapshot from a battery sysfs directory.

    ``capacity`` and ``status`` are mandatory — every Linux power_supply
    driver exposes them. The other three (``voltage_now``, ``current_now``,
    ``temp``) are best-effort: if a particular file is missing, we
    return a sensible default (0.0 V/mA, NaN °C) rather than raising,
    because the BQ27 family has slightly different feature sets across
    kernel versions and we'd rather show a partial reading than fail
    the whole poll.
    """
    import math

    capacity = _read_int_file(battery_dir / "capacity")
    if not 0 <= capacity <= 100:
        # Kernel sometimes returns 101 or -1 transiently during init.
        # Clamp to 0..100 so the UI math (color thresholds) stays sane.
        _log.debug("battery capacity %d out of range; clamping", capacity)
        capacity = max(0, min(100, capacity))

    status = _read_str_file(battery_dir / "status")

    # Optional fields. Each wrapped so a single missing file doesn't
    # poison the whole snapshot.
    try:
        voltage_uv = _read_int_file(battery_dir / "voltage_now")
        voltage_v = voltage_uv / 1_000_000.0
    except RuntimeError:
        voltage_v = 0.0

    try:
        current_ua = _read_int_file(battery_dir / "current_now")
        current_ma = current_ua / 1_000.0
    except RuntimeError:
        current_ma = 0.0

    try:
        temp_decic = _read_int_file(battery_dir / "temp")
        temperature_c = temp_decic / 10.0
    except RuntimeError:
        temperature_c = math.nan

    return BatteryState(
        capacity=capacity,
        voltage_v=voltage_v,
        current_ma=current_ma,
        temperature_c=temperature_c,
        status=status,
    )


# ── Async polling task ───────────────────────────────────────────────


class BatteryReader:
    """Long-running asyncio task that polls the fuel gauge at 1 Hz.

    Constructed with a UIState reference; on each successful poll it
    calls ``ui_state.set_battery(state)``, which triggers the dirty
    flag and the render thread redraws.

    When the battery directory cannot be found at startup, the reader
    enters a slow re-discovery loop (every 30 s) so a hot-plugged
    fuel gauge or a delayed driver bind eventually picks up. Until
    then, ``UIState.battery`` stays None and HOME shows '--'.

    Errors during a poll are logged once at WARNING and do not stop
    the task; transient I/O glitches must not silently kill battery
    reporting. After three consecutive failures we set
    ``ui_state.set_battery(None)`` to surface the problem in the UI.
    """

    # If discovery fails at startup, retry this often. 30 s is a
    # compromise between "operator notices the battery row eventually
    # populates" and "we don't pound the filesystem every second
    # forever on a system that just doesn't have a fuel gauge."
    REDISCOVER_INTERVAL_S: Final = 30.0

    # Three failures in a row before we surface "battery state
    # unknown" to the UI. Single transient errors (a stat() race
    # against a momentary i2c stall) are common; sustained errors
    # mean the chip is gone.
    FAILS_BEFORE_UNKNOWN: Final = 3

    def __init__(
        self,
        ui_state,                                     # microjs8.ui.state.UIState
        *,
        base_path: Path = DEFAULT_POWER_SUPPLY_DIR,
        name_fragment: str = DEFAULT_NAME_FRAGMENT,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._ui = ui_state
        self._base_path = base_path
        self._name_fragment = name_fragment
        self._poll_interval_s = poll_interval_s
        self._task: Optional[asyncio.Task] = None
        # Track failures so we surface "unknown" only on sustained
        # problems, not transient single-read glitches.
        self._consecutive_fails: int = 0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Schedule the polling coroutine on the given loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = loop.create_task(self._run(), name="battery-reader")

    def stop(self) -> None:
        """Cancel the polling coroutine. Idempotent and safe before start()."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        """The main polling loop.

        Two-phase:
          (1) Discovery — repeats every REDISCOVER_INTERVAL_S until a
              battery directory is found. While searching,
              ``ui_state.battery`` stays at whatever value it had last
              (None initially).
          (2) Polling — once found, read every poll_interval_s. If
              consecutive reads fail FAILS_BEFORE_UNKNOWN times in a
              row, set battery state to None and drop back into
              phase (1) to redetect.
        """
        _log.info("battery reader starting")
        try:
            while True:
                battery_dir = find_battery_dir(
                    base_path=self._base_path,
                    name_fragment=self._name_fragment,
                )
                if battery_dir is None:
                    _log.info(
                        "no battery matching %r in %s; retry in %.0fs",
                        self._name_fragment, self._base_path,
                        self.REDISCOVER_INTERVAL_S,
                    )
                    await asyncio.sleep(self.REDISCOVER_INTERVAL_S)
                    continue

                _log.info("battery found at %s", battery_dir)
                await self._poll_loop(battery_dir)
                # _poll_loop returns when sustained failures push us
                # back to rediscovery. Fall through to the outer
                # while-loop.
        except asyncio.CancelledError:
            _log.info("battery reader cancelled")
            raise

    async def _poll_loop(self, battery_dir: Path) -> None:
        """Inner per-directory polling loop. Returns on sustained failure."""
        self._consecutive_fails = 0
        while True:
            try:
                state = read_battery_state(battery_dir)
                self._ui.set_battery(state)
                self._consecutive_fails = 0
            except Exception as exc:                  # noqa: BLE001
                self._consecutive_fails += 1
                _log.warning(
                    "battery read failed (#%d): %s",
                    self._consecutive_fails, exc,
                )
                if self._consecutive_fails >= self.FAILS_BEFORE_UNKNOWN:
                    _log.warning(
                        "battery: %d consecutive failures; "
                        "surfacing unknown state and re-discovering",
                        self._consecutive_fails,
                    )
                    self._ui.set_battery(None)
                    return
            await asyncio.sleep(self._poll_interval_s)
