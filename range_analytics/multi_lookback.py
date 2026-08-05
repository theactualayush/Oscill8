"""
multi_lookback.py

Module 4B: repeatedly analyzes ONE StrategyHistory at MULTIPLE lookback
windows and describes how Module 4A's own measurements move across
those windows -- dispersion, short-vs-long change, step-by-step
structure, and how often each metric was even definable. Never
classifies, scores, or decides anything: every field here is either an
unmodified RangeAnalytics (one per lookback) or a plain descriptive
statistic of a sequence of already-approved 4A numbers.

analyze_multi_lookback() repeatedly calls range_analytics.analyze_range
-- it never reaches into 4A's lower-level primitives (resolve_window,
count_crossings, fit_ar1, ...) directly, and never touches database or
core.downloader. This guarantees 4B can never drift from 4A's own
edge-case handling; every NaN rule and degenerate-case convention is
defined exactly once, in 4A.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.config import BarInterval
from core.utils import DateLike, get_logger

from range_analytics.results import RangeAnalytics, analyze_range
from range_analytics.stability import LookbackStability, build_stability
from range_analytics.units import price_to_bp

from strategy_engine.pricing import StrategyHistory

logger = get_logger(__name__)

_NAN = float("nan")


def range_to_volatility_ratio(result: RangeAnalytics) -> float:
    """Size of the historical robust range relative to a typical one-bar
    movement (range_width_robust, converted to bp, divided by
    realized_vol_bp).

    This does NOT by itself indicate oscillation or range-bound
    behavior -- a slow, steady trend traversing a wide span over many
    bars produces just as large a ratio as a wide oscillating range,
    since the ratio only compares total span to typical single-step
    size and says nothing about how that span was traversed. Combine
    with efficiency_ratio, normalized crossing frequency, and ar1_beta
    for the complementary path-behavior information needed to tell a
    trend apart from an oscillating range.

    NaN when realized_vol_bp is 0 (genuine 0/0 -- a flat window's width
    is also necessarily 0) or itself NaN (too few observations to
    define volatility).
    """
    width_bp = price_to_bp(result.range_width_robust, result.market_key)
    vol_bp = result.realized_vol_bp
    if math.isnan(vol_bp) or vol_bp == 0:
        return _NAN
    return width_bp / vol_bp


def robust_to_full_width_ratio(result: RangeAnalytics) -> float:
    """How outlier-driven a single window's range is: range_width_robust
    / range_width_full. Near 1 means clean (the robust and full ranges
    agree); much less than 1 means the full range is dominated by one
    or a few extreme prints the robust range trims away.

    Standalone utility -- not wrapped in a dedicated cross-lookback
    stability field in MultiLookbackAnalytics (see module docstring in
    the Module 4B design review for why: it combines two measures of
    essentially the same underlying quantity -- level dispersion -- at
    different robustness levels, unlike range_to_volatility_ratio,
    which combines two independent axes). Apply this directly to any
    RangeAnalytics, including any MultiLookbackAnalytics.per_lookback[i]
    entry, if a cross-lookback view is wanted.

    0.0 when range_width_robust is 0 and range_width_full is nonzero (a
    real, meaningful zero). NaN only on genuine 0/0 (both zero, which
    is the only way range_width_full can be 0, since the robust range
    is always a subset of the full range).
    """
    full = result.range_width_full
    if math.isnan(full) or full == 0:
        return _NAN
    return result.range_width_robust / full


def _normalized_crossing_frequency(result: RangeAnalytics) -> float:
    """crossings / (observation_count - 1), using hysteresis_crossing_count
    (which equals raw_crossing_count whenever crossing_threshold=0.0,
    the default -- see range_analytics.oscillation.count_crossings).
    NaN when observation_count < 2 (denominator undefined)."""
    n = result.observation_count
    if n < 2:
        return _NAN
    return result.hysteresis_crossing_count / (n - 1)


@dataclass(frozen=True)
class MultiLookbackAnalytics:
    """Module 4A's diagnostics for ONE StrategyHistory across MULTIPLE
    lookbacks, plus threshold-free cross-lookback stability measurements
    for the metrics where tracking dispersion/change genuinely adds
    information beyond a single RangeAnalytics or a direct read of
    per_lookback. Never classifies, scores, or selects a window -- that
    judgment belongs to Module 5 (Template/Scanner).
    """

    market_key: str
    interval: BarInterval
    lookbacks_requested: tuple[int, ...]
    lookbacks_effective: tuple[int, ...]
    per_lookback: tuple[RangeAnalytics, ...]

    range_width_robust_stability: LookbackStability
    range_low_robust_stability: LookbackStability
    range_high_robust_stability: LookbackStability
    median_stability: LookbackStability
    realized_vol_bp_stability: LookbackStability
    efficiency_ratio_stability: LookbackStability
    normalized_crossing_frequency_stability: LookbackStability
    ar1_beta_stability: LookbackStability
    half_life_stability: LookbackStability
    range_to_volatility_ratio_stability: LookbackStability


def analyze_multi_lookback(
    history: StrategyHistory,
    lookbacks: tuple[int, ...] = (20, 40, 60, 90, 120),
    crossing_equilibrium: float | None = None,
    crossing_threshold: float = 0.0,
    lower_percentile: float = 5.0,
    upper_percentile: float = 95.0,
) -> MultiLookbackAnalytics:
    """Compute Module 4A diagnostics for `history` at each of `lookbacks`,
    plus threshold-free cross-lookback stability for the metrics where
    that genuinely adds information (see MultiLookbackAnalytics).

    `lookbacks` must be strictly increasing, arbitrary caller-supplied
    length >= 1 -- the default (20, 40, 60, 90, 120) is illustrative,
    never hard-coded logic.

    `crossing_equilibrium=None` (default) means each lookback gets its
    own window's median as equilibrium, not one global value forced
    across window lengths -- forcing one lookback's reference point
    onto a different-length window would conflate two different
    baselines. `lower_percentile`/`upper_percentile` (default 5.0/95.0)
    configure the robust-range band identically at every lookback, the
    same one band applied consistently across window lengths. All four
    are forwarded unchanged to every analyze_range() call, mirroring
    analyze_range's own pattern; analyze_range validates the percentile
    pair itself, so an invalid pair fails on the first per-lookback call.

    Calls analyze_range() once per lookback against the same in-memory
    `history` -- no I/O, no market-data access anywhere in this
    function.
    """
    if not lookbacks:
        raise ValueError("lookbacks must be non-empty")
    if any(a >= b for a, b in zip(lookbacks, lookbacks[1:])):
        raise ValueError(f"lookbacks must be strictly increasing, got {lookbacks}")

    per_lookback = tuple(
        analyze_range(
            history,
            lookback=lb,
            crossing_equilibrium=crossing_equilibrium,
            crossing_threshold=crossing_threshold,
            lower_percentile=lower_percentile,
            upper_percentile=upper_percentile,
        )
        for lb in lookbacks
    )

    definition = history.instance.definition
    market_key = definition.market_key
    interval = definition.interval

    logger.debug(
        "analyze_multi_lookback: %s [%s] lookbacks=%s -> effective=%s",
        market_key, interval.value, lookbacks,
        tuple(r.observation_count for r in per_lookback),
    )

    return MultiLookbackAnalytics(
        market_key=market_key,
        interval=interval,
        lookbacks_requested=tuple(lookbacks),
        lookbacks_effective=tuple(r.observation_count for r in per_lookback),
        per_lookback=per_lookback,
        range_width_robust_stability=build_stability(
            tuple(r.range_width_robust for r in per_lookback), signed=False
        ),
        range_low_robust_stability=build_stability(
            tuple(r.range_low_robust for r in per_lookback), signed=True
        ),
        range_high_robust_stability=build_stability(
            tuple(r.range_high_robust for r in per_lookback), signed=True
        ),
        median_stability=build_stability(
            tuple(r.median for r in per_lookback), signed=True
        ),
        realized_vol_bp_stability=build_stability(
            tuple(r.realized_vol_bp for r in per_lookback), signed=False
        ),
        efficiency_ratio_stability=build_stability(
            tuple(r.efficiency_ratio for r in per_lookback), signed=False
        ),
        normalized_crossing_frequency_stability=build_stability(
            tuple(_normalized_crossing_frequency(r) for r in per_lookback), signed=False
        ),
        ar1_beta_stability=build_stability(
            tuple(r.ar1_beta for r in per_lookback), signed=True
        ),
        half_life_stability=build_stability(
            tuple(r.half_life for r in per_lookback), signed=True
        ),
        range_to_volatility_ratio_stability=build_stability(
            tuple(range_to_volatility_ratio(r) for r in per_lookback), signed=False
        ),
    )
