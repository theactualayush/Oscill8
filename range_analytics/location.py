"""
location.py

Range/regime and current-location diagnostics for a resolved Strategy
Series: full and robust (P05-P95) range bounds/width, mean/median,
range position, distance from mean, and z-score.

Every function is a pure function of a plain pd.Series (already
NaN-free and window-resolved by lookback.resolve_window) plus, where
relevant, a handful of scalar floats -- no dependency on StrategyHistory
or any other project module, so each is independently testable with
hand-built data.
"""

from __future__ import annotations

import pandas as pd

_NAN = float("nan")


def range_low_full(series: pd.Series) -> float:
    """Minimum observed value over the window."""
    if series.empty:
        return _NAN
    return float(series.min())


def range_high_full(series: pd.Series) -> float:
    """Maximum observed value over the window."""
    if series.empty:
        return _NAN
    return float(series.max())


def range_width_full(series: pd.Series) -> float:
    """max(series) - min(series)."""
    if series.empty:
        return _NAN
    return float(series.max() - series.min())


def validate_percentiles(lower_percentile: float, upper_percentile: float) -> None:
    """Shared validation for the configurable robust-range percentile
    band: 0 <= lower_percentile < upper_percentile <= 100. Raises
    ValueError early and clearly rather than letting an invalid pair
    silently produce a degenerate/reversed band. Called once, from
    every entry point that accepts a percentile pair (analyze_range,
    analyze_multi_lookback, ScanRequest), so the rule is defined here
    exactly once.
    """
    if not (0 <= lower_percentile < upper_percentile <= 100):
        raise ValueError(
            "percentile bounds must satisfy 0 <= lower_percentile < "
            f"upper_percentile <= 100, got lower_percentile={lower_percentile}, "
            f"upper_percentile={upper_percentile}"
        )


def range_low_robust(series: pd.Series, lower_percentile: float = 5.0) -> float:
    """`lower_percentile`-th percentile -- a robust range floor,
    resistant to a single outlier print (e.g. a one-session FOMC/CPI-
    driven spike). Defaults to the 5th percentile, preserving prior
    behaviour for any caller that doesn't configure it."""
    if series.empty:
        return _NAN
    return float(series.quantile(lower_percentile / 100))


def range_high_robust(series: pd.Series, upper_percentile: float = 95.0) -> float:
    """`upper_percentile`-th percentile -- robust range ceiling.
    Defaults to the 95th percentile, preserving prior behaviour for any
    caller that doesn't configure it."""
    if series.empty:
        return _NAN
    return float(series.quantile(upper_percentile / 100))


def range_width_robust(
    series: pd.Series, lower_percentile: float = 5.0, upper_percentile: float = 95.0
) -> float:
    """upper_percentile - lower_percentile band width. Defaults to
    P95 - P05, trimming 10% of the distribution (5% each tail) so a
    single historical outlier doesn't dominate the reported range the
    way it can with max - min. A caller-selected band (e.g. P25/P75)
    trims proportionally more/less of each tail instead."""
    if series.empty:
        return _NAN
    return float(series.quantile(upper_percentile / 100) - series.quantile(lower_percentile / 100))


def range_position(current: float, low: float, high: float) -> float:
    """(current - low) / (high - low).

    Deliberately NOT clipped to [0, 1]: a value below 0 or above 1
    means `current` sits outside the [low, high] band supplied (e.g.
    below the historical low, or above the P95 in the robust case) --
    that is itself useful information, not an error to be hidden.

    Returns NaN when high == low (zero-width band -- position is
    undefined, not defaulted to 0.5), including when both are NaN
    (empty/degenerate input).
    """
    width = high - low
    if width == 0:
        return _NAN
    return float((current - low) / width)


def mean(series: pd.Series) -> float:
    if series.empty:
        return _NAN
    return float(series.mean())


def median(series: pd.Series) -> float:
    if series.empty:
        return _NAN
    return float(series.median())


def distance_from_mean(current: float, mean_value: float) -> float:
    """current - mean. NaN propagates automatically when either input is NaN."""
    return float(current - mean_value)


def z_score(current: float, mean_value: float, std_value: float) -> float:
    """(current - mean) / std.

    Returns NaN when std == 0 (a constant window -- the ratio is a
    genuine 0/0, not a real z-score) rather than raising or returning
    +/-inf. NaN std (e.g. fewer than 2 observations) propagates to NaN
    automatically through the division.
    """
    if std_value == 0:
        return _NAN
    return float((current - mean_value) / std_value)
