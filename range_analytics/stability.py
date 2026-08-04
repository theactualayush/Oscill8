"""
stability.py

Module 4B primitive: a purely descriptive dispersion/change summary of
one metric's value across multiple lookbacks. No verdict -- never
decides stable vs. unstable, just reports the numbers.

This is the same "spread of a set of numbers" idea Module 4A already
applies to prices-across-time (location.py's range_width/mean/median),
reused one level up: on measurements-across-lookbacks. Generic and
independent of StrategyHistory/RangeAnalytics -- it operates on a plain
sequence of floats, so it stays testable with hand-built tuples.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

_NAN = float("nan")


@dataclass(frozen=True)
class LookbackStability:
    """Dispersion/change of one metric's value across multiple lookbacks.

    `values` is ordered by increasing lookback length (shortest first).
    `signed` metrics (e.g. ar1_beta, or a bound that can be negative)
    never populate short_vs_long_ratio/pairwise_ratios -- a ratio across
    a sign change, or with a near-zero denominator, is not a meaningful
    quantity, so those fields are NaN unconditionally for such metrics
    rather than computed opportunistically per instance.
    """

    values: tuple[float, ...]
    defined_count: int
    stdev: float
    min: float
    max: float
    short_vs_long_diff: float
    short_vs_long_ratio: float
    pairwise_diffs: tuple[float, ...]
    pairwise_ratios: tuple[float, ...]


def _safe_ratio(numerator: float, denominator: float) -> float:
    if pd.isna(numerator) or pd.isna(denominator) or denominator == 0:
        return _NAN
    return numerator / denominator


def _safe_diff(a: float, b: float) -> float:
    if pd.isna(a) or pd.isna(b):
        return _NAN
    return b - a


def build_stability(values: tuple[float, ...], *, signed: bool) -> LookbackStability:
    """Build a LookbackStability from a metric's per-lookback values.

    `signed` must be declared by the caller for this metric (a fixed
    property of what the metric represents), not inferred from whether
    the particular values happened to be positive -- a metric that
    *could* cross zero should never expose a ratio, even in an instance
    where it didn't.
    """
    series = pd.Series(values, dtype="float64")
    defined = series.dropna()
    defined_count = len(defined)

    stdev = float(defined.std(ddof=1)) if defined_count >= 2 else _NAN
    lo = float(defined.min()) if defined_count else _NAN
    hi = float(defined.max()) if defined_count else _NAN

    first, last = values[0], values[-1]
    short_vs_long_diff = _safe_diff(first, last)
    short_vs_long_ratio = _NAN if signed else _safe_ratio(last, first)

    pairwise_diffs = tuple(_safe_diff(a, b) for a, b in zip(values, values[1:]))
    if signed:
        pairwise_ratios = tuple(_NAN for _ in pairwise_diffs)
    else:
        pairwise_ratios = tuple(_safe_ratio(b, a) for a, b in zip(values, values[1:]))

    return LookbackStability(
        values=tuple(values),
        defined_count=defined_count,
        stdev=stdev,
        min=lo,
        max=hi,
        short_vs_long_diff=short_vs_long_diff,
        short_vs_long_ratio=short_vs_long_ratio,
        pairwise_diffs=pairwise_diffs,
        pairwise_ratios=pairwise_ratios,
    )
