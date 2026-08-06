"""
tests/test_range_movement.py

mean_absolute_change (Tradability Analytics "Movement") -- close-to-
close mean absolute bar-to-bar change. Deliberately not a textbook OHLC
ATR; see range_analytics/movement.py's module docstring for why.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from range_analytics.movement import mean_absolute_change


def test_mean_absolute_change_matches_hand_computed_value():
    s = pd.Series([1.0, 1.02, 0.98, 1.05, 0.95])
    diffs = [0.02, -0.04, 0.07, -0.10]
    expected = sum(abs(d) for d in diffs) / len(diffs)
    assert mean_absolute_change(s) == pytest.approx(expected)


def test_mean_absolute_change_simple_monotonic_series():
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    # Each step is exactly 1.0, so mean absolute change is exactly 1.0.
    assert mean_absolute_change(s) == pytest.approx(1.0)


def test_mean_absolute_change_flat_series_is_zero_not_nan():
    s = pd.Series([1.0, 1.0, 1.0, 1.0])
    assert mean_absolute_change(s) == 0.0


def test_mean_absolute_change_nan_with_fewer_than_two_observations():
    assert math.isnan(mean_absolute_change(pd.Series([5.0])))
    assert math.isnan(mean_absolute_change(pd.Series([], dtype=float)))


def test_mean_absolute_change_two_observations_is_the_single_abs_diff():
    s = pd.Series([1.0, 1.5])
    assert mean_absolute_change(s) == pytest.approx(0.5)


def test_mean_absolute_change_is_direction_independent():
    up = pd.Series([1.0, 2.0, 1.0, 2.0])
    down = pd.Series([2.0, 1.0, 2.0, 1.0])
    assert mean_absolute_change(up) == pytest.approx(mean_absolute_change(down))
