"""Slot-timing consensus tracking.

JS8 receivers report a per-decode time-offset (``dt_seconds``) — the
delta between the receiver's expected slot start and the actual signal
start. When our local clock is correctly aligned with the network, we
should see those values clustered near zero across many stations.
A consistent non-zero median across many independent stations means
**we** are off relative to the consensus — either a clock issue or a
TX-pipeline latency that affects how our own outgoing audio appears
to receivers (since the same factor affects how their incoming audio
looks to us).

This module provides a small ``TimingTracker`` that consumes decoded
frames as they come in and exposes a rolling-median dt estimate.
The intended use is:

  * Phase A (current): log the running median so the operator can
    observe the consensus drift; no behavior change.
  * Phase B (later): the scheduler reads ``median_dt()`` and adjusts
    its slot-firing time by that amount, compensating automatically.

We keep the implementation tiny and dependency-free (a deque + a list
copy + ``statistics.median``). The cost per call is O(N) where N is
the window size; with N ≤ 20 in practice that's a few microseconds.

Thread safety: one writer thread (the decode dispatcher) and one
reader thread (the scheduler) each touch a deque + an int. We use
a plain Lock around mutations and the snapshot read to keep the
window state consistent across threads.
"""

from __future__ import annotations

import logging
import statistics
import threading
from collections import deque
from typing import Optional


_log = logging.getLogger(__name__)


# Default rolling window size. Three is the operator-stated minimum
# before we'd publish a median estimate; a slightly larger window
# (small enough to react quickly to changing band conditions, large
# enough to outvote a single outlier) is the actual default.
DEFAULT_WINDOW_SIZE = 8

# Minimum number of samples before we'll publish an estimate. Below
# this, ``median_dt()`` returns None — the caller knows not to trust
# anything yet.
DEFAULT_MIN_SAMPLES = 3


class TimingTracker:
    """Rolling-median tracker for decoded-frame dt values.

    Single instance per daemon. Fed by the decode dispatcher in app.py;
    queried by the scheduler (or anything else wanting the current
    consensus offset) via ``median_dt()``.
    """

    def __init__(
        self,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        min_samples: int = DEFAULT_MIN_SAMPLES,
    ) -> None:
        if window_size < 1:
            raise ValueError(f"window_size must be >= 1 (got {window_size})")
        if min_samples < 1:
            raise ValueError(f"min_samples must be >= 1 (got {min_samples})")
        if min_samples > window_size:
            raise ValueError(
                f"min_samples ({min_samples}) cannot exceed window_size "
                f"({window_size}) — we'd never publish an estimate"
            )
        self._window: deque[float] = deque(maxlen=window_size)
        self._min_samples = min_samples
        self._lock = threading.Lock()

    def add(self, dt_seconds: float) -> None:
        """Record one decode's dt value.

        We accept any finite float. The decoder may report wild
        outliers in noisy conditions; the median naturally rejects
        them as long as the majority of samples in the window are
        reasonable.
        """
        # Guard against NaN / inf which would corrupt the median.
        # statistics.median tolerates these but they'd produce
        # nonsense results downstream.
        if dt_seconds != dt_seconds:  # NaN check (NaN != NaN)
            return
        if dt_seconds == float("inf") or dt_seconds == float("-inf"):
            return
        with self._lock:
            self._window.append(float(dt_seconds))

    def median_dt(self) -> Optional[float]:
        """Return the median dt of the current window, or None.

        Returns None if we don't have at least ``min_samples`` data
        points yet. Otherwise returns the median in seconds (signed).

        Positive values mean signals arrive AFTER the local slot
        boundary expectation — i.e. either remote stations are late
        OR our clock is early. Conversely negative values.
        """
        with self._lock:
            if len(self._window) < self._min_samples:
                return None
            # Copy to a list inside the lock; statistics.median is
            # cheap on small lists, but doing it under the lock keeps
            # the snapshot consistent.
            return statistics.median(list(self._window))

    def sample_count(self) -> int:
        """How many decodes are in the current rolling window."""
        with self._lock:
            return len(self._window)

    def clear(self) -> None:
        """Reset the window. Used at startup or when a setting that
        invalidates prior measurements (e.g. major clock change) is
        applied."""
        with self._lock:
            self._window.clear()
