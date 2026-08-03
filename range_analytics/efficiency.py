"""
efficiency.py

Kaufman-style Efficiency Ratio: net displacement over a window divided
by total path length travelled to get there.

ER near 0: a large amount of back-and-forth movement relative to net
displacement -- potentially oscillatory/range-bound.
ER near 1: movement is predominantly directional -- potentially
trending.

IMPORTANT: ER cannot by itself distinguish genuine oscillation from
inactivity. A strategy that barely moves at all (tiny range width,
near-zero realized volatility) can also show a low or NaN ER purely
because there's nothing to be directional about -- that is a dead/
illiquid series, not an interesting trading range. ER must always be
read alongside range width and realized volatility, never in
isolation; this module deliberately does not attempt that
classification itself.
"""

from __future__ import annotations

import pandas as pd

_NAN = float("nan")


def efficiency_ratio(series: pd.Series) -> float:
    """abs(S_T - S_0) / sum(abs(Delta S_t)).

    Requires at least 2 observations (1 level change) to have a
    non-empty denominator; below that, NaN.

    A constant series (denominator == 0, whether from 2 observations
    or 2000) is a genuine 0/0 -- returns NaN rather than raising or
    fabricating a value.
    """
    if len(series) < 2:
        return _NAN
    diffs = series.diff().dropna()
    denominator = float(diffs.abs().sum())
    if denominator == 0:
        return _NAN
    numerator = abs(float(series.iloc[-1]) - float(series.iloc[0]))
    return numerator / denominator
