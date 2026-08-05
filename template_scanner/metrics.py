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

metric_value() is the canonical resolver for "a scalar metric by name
on a RangeAnalytics" -- either a direct dataclass field (e.g.
"efficiency_ratio") or one of the derived metrics registered in
_DERIVED_METRICS below (metrics that are functions of a RangeAnalytics,
not attributes of it). results_to_dataframe() (scan_results.py) and
filters.at_lookback() both resolve metrics through this one function,
so a metric name behaves identically -- same value, same set of
resolvable names -- everywhere in Module 5B (see the Module 5B
metric-resolution design review for why filters.py previously could not
consume derived metrics like normalized_crossing_frequency).
"""

from __future__ import annotations

from typing import Callable

from range_analytics.multi_lookback import (
    MultiLookbackAnalytics,
    range_to_volatility_ratio,
    robust_to_full_width_ratio,
)
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


def abs_z_score(result: RangeAnalytics) -> float:
    """abs(z_score) -- the conventional z-score's magnitude, useful for
    ranking/filtering on dislocation size regardless of direction. NaN
    propagates automatically (abs(nan) == nan): every NaN rule for
    z_score itself (zero/undefined std, insufficient observations) is
    defined exactly once, in range_analytics.location.z_score -- this
    is a pure, no-branching wrapper, not a second implementation.
    """
    return abs(result.z_score)


# Derived metrics: functions of a RangeAnalytics, not attributes of it.
# Kept as one explicit dict (not auto-discovered) so growth of the
# resolvable metric set is a visible, deliberate choice -- the same
# house style as scan_results.py's _STABILITY_METRICS list.
_DERIVED_METRICS: dict[str, Callable[[RangeAnalytics], float]] = {
    "normalized_crossing_frequency": normalized_crossing_frequency,
    "range_to_volatility_ratio": range_to_volatility_ratio,
    "robust_to_full_width_ratio": robust_to_full_width_ratio,
    "abs_z_score": abs_z_score,
}


def metric_value(analytics: RangeAnalytics, field: str) -> float:
    """Resolve `field` to a scalar value on `analytics` -- either a
    direct RangeAnalytics attribute (e.g. "efficiency_ratio",
    "ar1_beta") or one of the derived Module 5 metrics in
    _DERIVED_METRICS (e.g. "normalized_crossing_frequency",
    "range_to_volatility_ratio", "robust_to_full_width_ratio").

    This is the single canonical resolver -- results_to_dataframe()
    and filters.at_lookback() both go through this function rather than
    each separately deciding which field names are "direct" vs
    "derived", so a metric name means the same thing everywhere in
    Module 5B.

    Raises:
        AttributeError: `field` is neither a registered derived metric
            nor a RangeAnalytics attribute (the same failure mode plain
            getattr() already had for an unknown field).
    """
    derived = _DERIVED_METRICS.get(field)
    if derived is not None:
        return derived(analytics)
    return getattr(analytics, field)
