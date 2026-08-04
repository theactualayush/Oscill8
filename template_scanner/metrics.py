"""
metrics.py

Small derived-metric helpers for Module 5B that read Module 4A/4B
objects without adding new measurement logic to range_analytics itself.

at_lookback() is a pure lookup: MultiLookbackAnalytics stores its
per-lookback RangeAnalytics results as a positional tuple aligned with
lookbacks_requested; this resolves "the RangeAnalytics for lookback N"
by name instead of forcing every caller to redo the same zip/index
lookup.

normalized_crossing_frequency() duplicates the one-line formula
range_analytics.multi_lookback's own private _normalized_crossing_
frequency() helper already uses internally to build
normalized_crossing_frequency_stability -- but that per-lookback value
itself is never exposed as a public field. Module 4B is complete and
tested; this is pure arithmetic on already-public RangeAnalytics
fields, not a new measurement, so it is duplicated here rather than
reopening Module 4B (see the Module 5B design review for the reasoning).
"""

from __future__ import annotations

from range_analytics.multi_lookback import MultiLookbackAnalytics
from range_analytics.results import RangeAnalytics

_NAN = float("nan")


def at_lookback(multi_lookback: MultiLookbackAnalytics, lookback: int) -> RangeAnalytics:
    """Return the RangeAnalytics computed at exactly `lookback`.

    Raises:
        ValueError: `lookback` is not one of
            multi_lookback.lookbacks_requested.
    """
    try:
        index = multi_lookback.lookbacks_requested.index(lookback)
    except ValueError:
        raise ValueError(
            f"lookback {lookback} was not requested; "
            f"lookbacks_requested={multi_lookback.lookbacks_requested}"
        ) from None
    return multi_lookback.per_lookback[index]


def normalized_crossing_frequency(result: RangeAnalytics) -> float:
    """hysteresis_crossing_count / (observation_count - 1).

    NaN when observation_count < 2 (denominator undefined) -- mirrors
    range_analytics.multi_lookback's own private per-lookback formula
    exactly.
    """
    n = result.observation_count
    if n < 2:
        return _NAN
    return result.hysteresis_crossing_count / (n - 1)
