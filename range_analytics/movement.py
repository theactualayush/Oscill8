"""
movement.py

Close-to-close movement magnitude for a strategy LEVEL series: Mean
Absolute Change, mean(abs(Delta S_t)). Deliberately NOT a textbook OHLC
Average True Range -- the synthetic strategy history (strategy_engine.
pricing.StrategyHistory.history) carries only a single price field
(Close, by default) per leg, never true intrabar OHLC for the combined
multi-leg series, so a classical True Range calculation would be
fabricated, not economically real (see the Module 4A-tradability design
review). This is the direct close-to-close analogue: how much a bar
typically moves in absolute terms, independent of direction.
"""

from __future__ import annotations

import pandas as pd

_NAN = float("nan")


def mean_absolute_change(series: pd.Series) -> float:
    """Mean of abs(Delta S_t) = mean(abs(S_t - S_(t-1))).

    Requires at least 1 level change (2 observations) -- below that,
    NaN (insufficient data, distinct from a valid zero). A constant
    series (all diffs == 0, n >= 2) correctly returns 0.0 -- zero
    movement is a real, well-defined answer, not a 0/0, exactly the
    same convention range_analytics.volatility.realized_volatility uses
    for a flat series.

    Returns raw price-unit movement; bp conversion is the caller's
    responsibility (see range_analytics.units.price_to_bp), keeping
    this function market-agnostic.
    """
    diffs = series.diff().dropna()
    if len(diffs) < 1:
        return _NAN
    return float(diffs.abs().mean())
