from __future__ import annotations

import math

import pandas as pd
import pytest

from range_analytics import volatility


def test_realized_volatility_matches_manual_std_of_level_changes():
    s = pd.Series([1.0, 1.02, 0.99, 1.05])
    diffs = s.diff().dropna()
    assert volatility.realized_volatility(s) == pytest.approx(diffs.std(ddof=1))


def test_realized_volatility_nan_below_three_observations():
    assert math.isnan(volatility.realized_volatility(pd.Series([1.0])))
    assert math.isnan(volatility.realized_volatility(pd.Series([1.0, 1.01])))


def test_realized_volatility_zero_for_constant_series():
    s = pd.Series([1.0, 1.0, 1.0, 1.0])
    assert volatility.realized_volatility(s) == 0.0


def test_realized_volatility_uses_level_changes_not_percentage_returns():
    # A series that crosses zero -- pct_change would be undefined/explode here.
    s = pd.Series([-0.01, 0.0, 0.01])
    result = volatility.realized_volatility(s)
    assert not math.isnan(result)
    assert result == pytest.approx(s.diff().dropna().std(ddof=1))


def test_realized_volatility_empty_series_is_nan():
    assert math.isnan(volatility.realized_volatility(pd.Series([], dtype=float)))
