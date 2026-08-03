from __future__ import annotations

import math

import pandas as pd
import pytest

from range_analytics import efficiency


def test_efficiency_ratio_pure_trend_is_one():
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert efficiency.efficiency_ratio(s) == pytest.approx(1.0)


def test_efficiency_ratio_pure_oscillation_returning_to_start_is_zero():
    s = pd.Series([0.0, 1.0, 0.0, 1.0, 0.0])
    assert efficiency.efficiency_ratio(s) == pytest.approx(0.0)


def test_efficiency_ratio_nan_for_constant_series():
    s = pd.Series([2.0, 2.0, 2.0])
    assert math.isnan(efficiency.efficiency_ratio(s))


def test_efficiency_ratio_nan_for_single_observation():
    assert math.isnan(efficiency.efficiency_ratio(pd.Series([1.0])))


def test_efficiency_ratio_nan_for_empty_series():
    assert math.isnan(efficiency.efficiency_ratio(pd.Series([], dtype=float)))


def test_efficiency_ratio_partial_directional_move():
    s = pd.Series([0.0, 2.0, 1.0])  # net displacement 1, total path 2 + 1 = 3
    assert efficiency.efficiency_ratio(s) == pytest.approx(1 / 3)
