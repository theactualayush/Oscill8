"""
volatility.py

Realized volatility for a strategy LEVEL series (e.g. a SOFR fly),
defined as the sample standard deviation of level changes -- NOT
percentage returns, which are meaningless for a series that routinely
crosses zero. Not annualized: this returns the raw per-bar dispersion:
annualizing requires an interval-dependent scale factor that belongs at
the call site, not hidden in this function.
"""

from __future__ import annotations

import pandas as pd

_NAN = float("nan")


def realized_volatility(series: pd.Series) -> float:
    """Sample standard deviation (ddof=1) of Delta S_t = S_t - S_(t-1).

    Requires at least 2 level changes (3 observations) to estimate a
    sample standard deviation at all -- with 0 or 1 diffs the ddof=1
    estimator is undefined (division by zero degrees of freedom), so
    this is a genuine mathematical floor, not a chosen reliability
    threshold. Returns NaN below it.

    A constant series (all diffs == 0, n >= 3) correctly returns 0.0,
    not NaN -- zero volatility is a real, well-defined answer, unlike
    the 0/0 cases in z_score/efficiency_ratio.

    Returns raw price-unit volatility; bp conversion is the caller's
    responsibility (see range_analytics.units.price_to_bp), keeping
    this function market-agnostic.
    """
    diffs = series.diff().dropna()
    if len(diffs) < 2:
        return _NAN
    return float(diffs.std(ddof=1))
