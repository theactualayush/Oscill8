"""
results.py

Module 4A public entry point: turns a strategy_engine.StrategyHistory
into a RangeAnalytics -- a flat set of independently-interpretable
range/location, movement, oscillation, and mean-reversion diagnostics
for a selected historical window.

This module produces measurements only. It does not classify a
strategy as range-bound, does not compute a composite score, and does
not detect regime boundaries or duration -- those are out of scope for
Module 4A (see Module 4B).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.config import BarInterval
from core.utils import DateLike, get_logger
from strategy_engine.pricing import StrategyHistory

from range_analytics import efficiency, location, oscillation, units, volatility
from range_analytics.lookback import resolve_window
from range_analytics.mean_reversion import fit_ar1

logger = get_logger(__name__)


@dataclass(frozen=True)
class RangeAnalytics:
    """Diagnostics for one StrategyHistory over one historical window.

    Deliberately flat (not nested by category) so a batch of these
    converts directly to one DataFrame row each. Every numeric field
    uses NaN, consistently, for anything mathematically undefined or
    unavailable (e.g. too few observations, a zero denominator) -- no
    field is ever fabricated. No field here classifies, scores, or
    ranks a strategy; those decisions belong to a later module.
    """

    market_key: str
    interval: BarInterval
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    observation_count: int

    current_price: float
    mean: float
    median: float

    range_low_full: float
    range_high_full: float
    range_width_full: float
    range_low_robust: float
    range_high_robust: float
    range_width_robust: float
    lower_percentile: float
    upper_percentile: float

    range_position_full: float
    range_position_robust: float
    distance_from_mean: float
    z_score: float

    realized_vol_price: float
    realized_vol_bp: float

    efficiency_ratio: float

    crossing_equilibrium: float
    crossing_threshold: float
    raw_crossing_count: int
    hysteresis_crossing_count: int

    ar1_gamma: float
    ar1_beta: float
    ar1_std_error: float
    ar1_r_squared: float
    half_life: float


def analyze_range(
    history: StrategyHistory,
    lookback: int | None = None,
    start: DateLike | None = None,
    end: DateLike | None = None,
    crossing_equilibrium: float | None = None,
    crossing_threshold: float = 0.0,
    lower_percentile: float = 5.0,
    upper_percentile: float = 95.0,
) -> RangeAnalytics:
    """Compute RangeAnalytics for one StrategyHistory over a window.

    Window selection: either `lookback` (last N valid observations) or
    `start`/`end` (calendar-date filter), never both -- see
    lookback.resolve_window. Neither given selects the entire history.

    `crossing_equilibrium` defaults to the window's median when not
    given. `crossing_threshold` defaults to 0.0 (no hysteresis) --
    Module 4A does not assume any particular hysteresis convention.
    Both the resolved equilibrium and the threshold actually used are
    returned on the result, alongside both `raw_crossing_count`
    (always threshold=0.0) and `hysteresis_crossing_count` (uses
    `crossing_threshold`), so a caller can compare conventions
    empirically before any one is adopted.

    `lower_percentile`/`upper_percentile` (default 5.0/95.0, preserving
    prior behaviour) configure the robust-range band -- validated via
    location.validate_percentiles() before any computation, so an
    invalid pair (lower >= upper, or outside [0, 100]) fails immediately
    rather than producing a degenerate range. Both are also recorded on
    the returned RangeAnalytics so a caller (e.g. the UI) always knows
    exactly which band produced range_low_robust/range_high_robust/
    range_width_robust/range_position_robust, without having to thread
    the ScanRequest/config alongside the result separately.

    Z-score window semantics (documented here since it is easy to get
    wrong): `current` is `series.iloc[-1]` -- the most recent
    observation of the SAME window (of exactly `lookback` valid
    observations, or fewer if history is short) that `mean`/`std` are
    also computed over. That is, `current` is INCLUDED in the sample
    used for `mean` and `std` -- this is an in-sample z-score
    (Z = (X_n - mean(X_1..X_n)) / std(X_1..X_n)), not an out-of-sample
    one comparing `current` against the *other* N-1 observations. This
    is the established, tested convention (see
    tests/test_range_analytics.py) and is preserved unchanged here.
    """
    location.validate_percentiles(lower_percentile, upper_percentile)

    window = resolve_window(history.history, lookback=lookback, start=start, end=end)
    series = window["Strategy"]
    n = len(series)

    definition = history.instance.definition
    market_key = definition.market_key
    interval = definition.interval

    window_start = window["Date"].iloc[0] if n else pd.NaT
    window_end = window["Date"].iloc[-1] if n else pd.NaT

    current = float(series.iloc[-1]) if n else float("nan")
    mean_value = location.mean(series)
    median_value = location.median(series)

    low_full = location.range_low_full(series)
    high_full = location.range_high_full(series)
    width_full = location.range_width_full(series)
    low_robust = location.range_low_robust(series, lower_percentile)
    high_robust = location.range_high_robust(series, upper_percentile)
    width_robust = location.range_width_robust(series, lower_percentile, upper_percentile)

    position_full = location.range_position(current, low_full, high_full)
    position_robust = location.range_position(current, low_robust, high_robust)
    distance = location.distance_from_mean(current, mean_value)
    std_value = float(series.std(ddof=1))
    z = location.z_score(current, mean_value, std_value)

    vol_price = volatility.realized_volatility(series)
    vol_bp = units.price_to_bp(vol_price, market_key)

    er = efficiency.efficiency_ratio(series)

    equilibrium = median_value if crossing_equilibrium is None else crossing_equilibrium
    raw_count = oscillation.count_crossings(series, equilibrium, threshold=0.0)
    hysteresis_count = oscillation.count_crossings(
        series, equilibrium, threshold=crossing_threshold
    )

    ar1 = fit_ar1(series)

    logger.debug(
        "analyze_range: %s [%s] n=%d window=%s -> %s",
        market_key, interval.value, n, window_start, window_end,
    )

    return RangeAnalytics(
        market_key=market_key,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
        observation_count=n,
        current_price=current,
        mean=mean_value,
        median=median_value,
        range_low_full=low_full,
        range_high_full=high_full,
        range_width_full=width_full,
        range_low_robust=low_robust,
        range_high_robust=high_robust,
        range_width_robust=width_robust,
        lower_percentile=lower_percentile,
        upper_percentile=upper_percentile,
        range_position_full=position_full,
        range_position_robust=position_robust,
        distance_from_mean=distance,
        z_score=z,
        realized_vol_price=vol_price,
        realized_vol_bp=vol_bp,
        efficiency_ratio=er,
        crossing_equilibrium=equilibrium,
        crossing_threshold=crossing_threshold,
        raw_crossing_count=raw_count,
        hysteresis_crossing_count=hysteresis_count,
        ar1_gamma=ar1.gamma,
        ar1_beta=ar1.beta,
        ar1_std_error=ar1.std_error,
        ar1_r_squared=ar1.r_squared,
        half_life=ar1.half_life,
    )
