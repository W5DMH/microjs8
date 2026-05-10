"""Tests for microjs8.timing.TimingTracker."""

from __future__ import annotations

import math
import threading

import pytest

from microjs8.timing import (
    DEFAULT_MIN_SAMPLES,
    DEFAULT_WINDOW_SIZE,
    TimingTracker,
)


# ── Defaults & validation ───────────────────────────────────────────


def test_defaults_are_sensible():
    """Default window/min-samples must produce a usable tracker."""
    t = TimingTracker()
    assert t.sample_count() == 0
    assert t.median_dt() is None  # nothing yet


def test_window_size_must_be_positive():
    with pytest.raises(ValueError, match="window_size"):
        TimingTracker(window_size=0)


def test_min_samples_must_be_positive():
    with pytest.raises(ValueError, match="min_samples"):
        TimingTracker(min_samples=0)


def test_min_samples_cannot_exceed_window():
    """If min_samples > window_size we'd never publish an estimate."""
    with pytest.raises(ValueError, match="cannot exceed"):
        TimingTracker(window_size=3, min_samples=5)


# ── Adding samples & sample count ───────────────────────────────────


def test_sample_count_grows_with_adds():
    t = TimingTracker(window_size=10, min_samples=3)
    t.add(0.5)
    t.add(0.6)
    assert t.sample_count() == 2


def test_window_is_capped_at_window_size():
    """Adding more than window_size samples drops the oldest."""
    t = TimingTracker(window_size=3, min_samples=1)
    t.add(1.0)
    t.add(2.0)
    t.add(3.0)
    t.add(4.0)
    t.add(5.0)
    assert t.sample_count() == 3
    # The oldest two (1.0 and 2.0) were dropped; window is [3.0, 4.0, 5.0]
    assert t.median_dt() == 4.0


# ── Median publishing ───────────────────────────────────────────────


def test_no_estimate_below_min_samples():
    """Below min_samples the tracker returns None."""
    t = TimingTracker(window_size=10, min_samples=3)
    t.add(1.5)
    t.add(1.5)
    assert t.median_dt() is None
    t.add(1.5)
    assert t.median_dt() == 1.5


def test_median_of_three_consistent_samples():
    """Three samples around 1.8 should produce a median near 1.8."""
    t = TimingTracker(window_size=10, min_samples=3)
    t.add(1.7)
    t.add(1.8)
    t.add(1.9)
    assert t.median_dt() == 1.8


def test_median_rejects_single_outlier():
    """Median is robust — one wild value among reasonable ones
    doesn't move the result much."""
    t = TimingTracker(window_size=10, min_samples=3)
    for v in [1.7, 1.8, 1.85, 1.9, 1.95]:
        t.add(v)
    # All clustered around 1.8 — median is the middle one
    assert t.median_dt() == pytest.approx(1.85)
    # Now add a wild outlier
    t.add(15.0)
    # Median moves slightly — but stays in the cluster, not at 15
    median = t.median_dt()
    assert 1.7 < median < 2.0


def test_median_with_negative_values():
    """Negative dt is valid (signal arrived BEFORE our slot start)."""
    t = TimingTracker(window_size=10, min_samples=3)
    t.add(-0.5)
    t.add(-0.3)
    t.add(-0.4)
    assert t.median_dt() == pytest.approx(-0.4)


# ── Robustness against pathological inputs ──────────────────────────


def test_nan_is_ignored():
    """A NaN dt value should not be added to the window."""
    t = TimingTracker(window_size=10, min_samples=1)
    t.add(1.0)
    t.add(float("nan"))
    t.add(2.0)
    assert t.sample_count() == 2
    assert t.median_dt() == pytest.approx(1.5)


def test_infinity_is_ignored():
    """+inf and -inf must not pollute the window."""
    t = TimingTracker(window_size=10, min_samples=1)
    t.add(1.0)
    t.add(float("inf"))
    t.add(float("-inf"))
    t.add(2.0)
    assert t.sample_count() == 2
    assert t.median_dt() == pytest.approx(1.5)


def test_clear_resets_state():
    t = TimingTracker(window_size=10, min_samples=1)
    for v in [1.0, 2.0, 3.0]:
        t.add(v)
    assert t.sample_count() == 3
    t.clear()
    assert t.sample_count() == 0
    assert t.median_dt() is None


# ── Thread safety smoke test ────────────────────────────────────────


def test_concurrent_add_and_read():
    """One writer + one reader running in parallel must not corrupt
    state. We don't assert specific values (race-free median is hard
    to define mid-update) — just that the program terminates without
    exception and final state looks sane."""
    t = TimingTracker(window_size=20, min_samples=3)

    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        try:
            i = 0
            while not stop.is_set():
                t.add(float(i % 5) * 0.5)
                i += 1
        except BaseException as exc:
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                _ = t.median_dt()
                _ = t.sample_count()
        except BaseException as exc:
            errors.append(exc)

    w = threading.Thread(target=writer, daemon=True)
    r = threading.Thread(target=reader, daemon=True)
    w.start()
    r.start()
    threading.Event().wait(0.05)  # 50 ms of churn
    stop.set()
    w.join(timeout=1.0)
    r.join(timeout=1.0)

    assert not errors, errors
    assert t.sample_count() > 0
    assert t.sample_count() <= 20  # window cap respected
    median = t.median_dt()
    assert median is not None
    assert 0.0 <= median <= 2.0  # all values were 0..2
