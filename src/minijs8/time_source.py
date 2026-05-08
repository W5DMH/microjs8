"""Combined time-source decision: chrony OR consensus.

Two independent ways to know we have a valid time reference:

  1. **chrony** — the system NTP daemon, which reports a healthy sync
     when it has a usable upstream time source (GPS, internet NTP, or
     a peer). When chrony is happy, the system clock is correct
     within a few ms of UTC.

  2. **consensus** — the median ``dt_seconds`` value from recent
     decoded JS8 frames. When ≥ 3 decodes have come in, the median
     tells us how far our slot grid is offset from the network
     consensus. Stations broadcasting on the same band are ipso facto
     time-agreed-enough-to-decode-each-other, so they form a usable
     reference for slot alignment even when no GPS or NTP is
     available.

This module's job is to wrap both into a single decision the safety
gate and scheduler both consult. It returns:

  * ``usable`` — True if either source is good enough to TX with.
  * ``source`` — string label ("chrony", "consensus", or "" when no
    source is usable). Surfaced to the UI so the operator knows
    whether they're running on UTC-authoritative time or on slot-
    alignment-only consensus time.
  * ``correction_seconds`` — signed offset to apply to the scheduler's
    slot grid, in seconds. Always in the form "add this to
    epoch_seconds before computing slot_seconds % 15". Zero when
    chrony is happy and consensus is sub-threshold.

When BOTH chrony and consensus are available and they DISAGREE by
more than 3 s, we trust consensus per operator decision (Phase Y
Q1): consensus is what stations are actually decoding to. Chrony
might be tracking an internet NTP source that is itself off, or
chrony might be syncing to a stratum-N server with delay we can't
measure. Consensus is the radio-reality reference.

Sign convention reminder:

  median_dt < 0  → signals arrive BEFORE our slot expectation
                 → our local clock is LATE
                 → fire TX EARLIER (compensate by -median_dt seconds)
  median_dt > 0  → signals arrive AFTER our slot expectation
                 → our local clock is EARLY
                 → fire TX LATER  (compensate by -median_dt seconds)

So ``correction_seconds = -median_dt`` always (when we apply it).
We add this to ``epoch_seconds`` before the slot-grid mod.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from minijs8.timing import TimingTracker


_log = logging.getLogger(__name__)


# Below this magnitude (in seconds), consensus correction is NOT
# applied even though we know the value. JS8 protocol tolerates a few
# hundred ms of slot misalignment; chasing sub-threshold noise risks
# oscillation. Above this magnitude we apply the correction.
#
# Operator decision (Phase Y, revised after on-air testing): 0.3 s.
# The original 0.5 s value was set on a "two stations each at 0.5 s
# could end up 1 s apart" cumulative-offset argument, but on-air
# evidence showed our local clock running consistently ~340 ms off
# from network consensus (chrony tracking internet NTP, vs other
# stations averaging across multiple sources). 0.3 s is well above
# the decoder's dt-estimation noise floor (~50 ms) and catches the
# offset we actually observed without chasing genuine jitter.
TIMING_CORRECTION_THRESHOLD_S = 0.3


# When chrony AND consensus are both available and they disagree by
# more than this magnitude, we trust consensus (per operator decision
# Q1). Below this, we trust chrony — it's the more authoritative source
# when both agree.
CHRONY_CONSENSUS_DISAGREE_S = 3.0


# Type alias for chrony health probes.
ChronyOkFn = Callable[[], bool]


@dataclass(frozen=True)
class TimeSource:
    """Result of a time-source decision query."""

    usable: bool
    """True if some source (chrony or consensus) is good enough to TX."""

    source: str
    """Human-readable label: "chrony", "consensus", or "" (none)."""

    correction_seconds: float
    """Offset to add to epoch_seconds for slot alignment.

    Always 0.0 when source == "chrony" and consensus is sub-threshold.
    Equal to ``-median_dt`` when we're applying a consensus correction.
    """


def time_source_status(
    *,
    chrony_ok_fn: ChronyOkFn,
    timing_tracker: Optional[TimingTracker],
) -> TimeSource:
    """Decide which time source to trust right now.

    Args:
        chrony_ok_fn: zero-arg callable returning True if chrony is
            currently synced. May shell out to ``chronyc tracking``;
            should be cached to avoid hammering chrony.
        timing_tracker: optional consensus tracker. If None, we behave
            as if no consensus information is available (used in tests
            that don't care about the consensus path).

    Returns:
        A ``TimeSource`` describing the decision.
    """
    chrony_ok = bool(chrony_ok_fn())

    median_dt: Optional[float] = None
    consensus_n = 0
    if timing_tracker is not None:
        median_dt = timing_tracker.median_dt()
        consensus_n = timing_tracker.sample_count()

    consensus_available = median_dt is not None

    # Case 1: chrony happy, no consensus → trust chrony, no correction.
    if chrony_ok and not consensus_available:
        return TimeSource(usable=True, source="chrony", correction_seconds=0.0)

    # Case 2: chrony happy, consensus also available.
    # Sub-decision: do they agree enough to trust chrony, or has
    # consensus diverged enough that we should follow it instead?
    if chrony_ok and consensus_available:
        assert median_dt is not None  # for mypy/typing clarity
        if abs(median_dt) > CHRONY_CONSENSUS_DISAGREE_S:
            # They disagree significantly. Per operator decision,
            # consensus is the radio-reality reference, so we follow
            # it. The chrony-happy state may itself be wrong (NTP
            # source drift, etc.).
            _log.warning(
                "chrony and consensus disagree (chrony OK but "
                "consensus median dt = %+.2fs, n=%d); following "
                "consensus",
                median_dt, consensus_n,
            )
            return TimeSource(
                usable=True,
                source="consensus",
                correction_seconds=-median_dt,
            )
        # Both sources agree enough. Trust chrony, but apply consensus
        # correction if it exceeds our threshold (continuous self-tune
        # within the agree-enough band).
        if abs(median_dt) > TIMING_CORRECTION_THRESHOLD_S:
            return TimeSource(
                usable=True,
                source="chrony",
                correction_seconds=-median_dt,
            )
        return TimeSource(usable=True, source="chrony", correction_seconds=0.0)

    # Case 3: chrony unhappy, but consensus available.
    # Per operator decision: consensus is the truth when GPS/NTP
    # aren't available. No cap on correction magnitude (boot-up time
    # could be hours/days off).
    if not chrony_ok and consensus_available:
        assert median_dt is not None
        return TimeSource(
            usable=True,
            source="consensus",
            correction_seconds=-median_dt,
        )

    # Case 4: nothing available. TX must be blocked.
    return TimeSource(usable=False, source="", correction_seconds=0.0)
