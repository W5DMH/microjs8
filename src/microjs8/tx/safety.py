"""TX safety gate — the four hard checks before any transmission.

Per Step 6 spec, all four of these MUST pass before TxBackend.transmit
is called:

  1. Callsign must not be N0CALL — except in emergency override.
  2. Configured grid must be set — except in emergency override.
  3. GPS fix required when emergency override is active (the bypass
     allows N0CALL but we still need a real grid for SOS to be useful).
  4. A usable time source must be available — chrony OR consensus.
     JS8 protocol requires ±1 s time accuracy; without sync we'd
     splatter the band.

Phase Y addition: time-source check delegates to
``time_source.time_source_status()``, which considers both chrony
sync state AND the running consensus from decoded JS8 frames. This
allows TX in basement / no-GPS scenarios where chrony has no upstream
but the radio is hearing other stations.

Frame-rate is enforced by the scheduler's structure (one TX per slot
loop iteration), not by this module.

Construct with refs to UIState (for emergency-override state), the
``Config`` (for callsign/grid identity), a chrony-status function,
and optionally a TimingTracker for consensus fallback.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Callable, Optional

from microjs8.time_source import TimeSource, time_source_status
from microjs8.timing import TimingTracker

_log = logging.getLogger(__name__)


# Sentinel for unconfigured station.
N0CALL = "N0CALL"


# Type alias: returns True if chrony reports a healthy time sync.
ChronyOk = Callable[[], bool]


class TxSafetyGate:
    """Concrete safety gate. Implements ``_SafetyGateProto``."""

    def __init__(
        self,
        ui_state,             # microjs8.ui.state.UIState (avoid import cycle)
        chrony_ok_fn: Optional[ChronyOk] = None,
        timing_tracker: Optional[TimingTracker] = None,
    ) -> None:
        self._ui = ui_state
        # If no chrony function is provided, default to "is the chrony
        # binary on the path AND `chronyc tracking` exits 0 with a
        # non-stratum-0 reference?" That's a reasonable proxy for
        # "we have time discipline."
        self._chrony_ok_fn = chrony_ok_fn or default_chrony_ok
        # Optional consensus fallback. When chrony is unavailable but
        # we've heard ≥3 decodes, we can still TX using the consensus
        # offset from time_source_status().
        self._timing_tracker = timing_tracker

    def time_source(self) -> TimeSource:
        """Snapshot the current time-source decision.

        Public so the scheduler can read the same answer the gate
        uses (avoids divergence between "is TX allowed" and "what
        slot grid offset should we apply").
        """
        return time_source_status(
            chrony_ok_fn=self._chrony_ok_fn,
            timing_tracker=self._timing_tracker,
        )

    def check_can_transmit(self) -> tuple[bool, Optional[str]]:
        """Return (ok, reason). ``reason`` is short, UI-displayable."""
        snap = self._ui.snapshot()

        # Resolve the time-source decision once for this check. The
        # scheduler will read the same answer (via time_source()) when
        # it computes slot timing, so the two stay coherent.
        ts = self.time_source()

        # Emergency override path: bypasses callsign/grid checks BUT
        # still requires GPS lock (the SOS message needs a real
        # locator) and a usable time source.
        if snap.emergency_override:
            if not snap.gps.has_position:
                return False, "GPS fix required for emergency"
            if not ts.usable:
                return False, "time not synced (no consensus)"
            return True, None

        # Normal path.
        if snap.callsign == N0CALL or not snap.callsign:
            return False, "callsign not set"
        if not snap.grid:
            return False, "grid not set"
        if not ts.usable:
            return False, "time not synced (no consensus)"
        return True, None


# ── Default chrony probe ─────────────────────────────────────────────


# Cache successful chrony probe for this many seconds; chrony state
# changes slowly compared to our slot rate, and shelling out twice a
# minute is wasteful. The cache is invalidated on any failed probe so
# we re-check immediately when something might have changed.
_CHRONY_CACHE_S = 5.0
_chrony_cache: dict = {"checked_at": 0.0, "result": False}


def default_chrony_ok() -> bool:
    """Return True if chrony has a usable time source.

    Calls ``chronyc tracking`` and looks for either:
      - "Leap status     : Normal" — chrony is satisfied
      - "Reference ID" not equal to "00000000 ()" — we have a source
        (handles the "synced but technically pre-leap-known" case)

    Caches recent successful results for _CHRONY_CACHE_S seconds.
    Returns False if chronyc isn't available or the call fails for
    any reason (defensive — prefer "no TX" over "TX without time
    discipline").
    """
    import time
    now = time.monotonic()
    if (
        _chrony_cache["result"]
        and (now - _chrony_cache["checked_at"]) < _CHRONY_CACHE_S
    ):
        return True

    chronyc = shutil.which("chronyc")
    if chronyc is None:
        _log.debug("chronyc not on PATH — assuming time not synced")
        _chrony_cache["result"] = False
        _chrony_cache["checked_at"] = now
        return False

    try:
        result = subprocess.run(
            [chronyc, "tracking"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.warning("chronyc tracking failed: %s", exc)
        _chrony_cache["result"] = False
        _chrony_cache["checked_at"] = now
        return False

    if result.returncode != 0:
        _log.debug("chronyc tracking returned %d: %s",
                   result.returncode, result.stderr)
        _chrony_cache["result"] = False
        _chrony_cache["checked_at"] = now
        return False

    out = result.stdout
    # Two checks: leap status + non-zero reference ID. Both should
    # be truthy on a healthy chrony.
    leap_match = re.search(r"Leap status\s*:\s*(\w+)", out)
    ref_match = re.search(r"Reference ID\s*:\s*([0-9A-Fa-f]+)", out)

    leap_ok = leap_match is not None and leap_match.group(1) == "Normal"
    ref_set = ref_match is not None and ref_match.group(1) != "00000000"

    ok = leap_ok and ref_set
    _chrony_cache["result"] = ok
    _chrony_cache["checked_at"] = now
    return ok
