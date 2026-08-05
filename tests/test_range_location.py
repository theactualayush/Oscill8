from __future__ import annotations

import math

import pandas as pd
import pytest

from range_analytics import location


def test_range_width_full_matches_max_minus_min():
    s = pd.Series([1.0, 3.0, 2.0, 5.0])
    assert location.range_low_full(s) == 1.0
    assert location.range_high_full(s) == 5.0
    assert location.range_width_full(s) == 4.0


def test_range_robust_matches_5th_and_95th_percentile():
    s = pd.Series(list(range(1, 101)), dtype=float)
    assert location.range_low_robust(s) == pytest.approx(s.quantile(0.05))
    assert location.range_high_robust(s) == pytest.approx(s.quantile(0.95))
    assert location.range_width_robust(s) == pytest.approx(
        s.quantile(0.95) - s.quantile(0.05)
    )


# ---------------------------------------------------------------------
# Configurable robust-range percentiles
# ---------------------------------------------------------------------

def test_range_robust_default_matches_5_95_explicitly():
    # Same fixture/assertions as the pre-existing default test, but
    # calling with explicit lower_percentile=5.0/upper_percentile=95.0
    # -- proves the new parameters' defaults reproduce prior behaviour
    # exactly, not just "close enough".
    s = pd.Series(list(range(1, 101)), dtype=float)
    assert location.range_low_robust(s, lower_percentile=5.0) == pytest.approx(s.quantile(0.05))
    assert location.range_high_robust(s, upper_percentile=95.0) == pytest.approx(s.quantile(0.95))
    assert location.range_width_robust(
        s, lower_percentile=5.0, upper_percentile=95.0
    ) == pytest.approx(s.quantile(0.95) - s.quantile(0.05))


def test_range_robust_25_75_matches_expected_quantiles():
    s = pd.Series(list(range(1, 101)), dtype=float)
    assert location.range_low_robust(s, lower_percentile=25.0) == pytest.approx(s.quantile(0.25))
    assert location.range_high_robust(s, upper_percentile=75.0) == pytest.approx(s.quantile(0.75))
    assert location.range_width_robust(
        s, lower_percentile=25.0, upper_percentile=75.0
    ) == pytest.approx(s.quantile(0.75) - s.quantile(0.25))


def test_range_robust_10_90_matches_expected_quantiles():
    s = pd.Series(list(range(1, 101)), dtype=float)
    assert location.range_low_robust(s, lower_percentile=10.0) == pytest.approx(s.quantile(0.10))
    assert location.range_high_robust(s, upper_percentile=90.0) == pytest.approx(s.quantile(0.90))
    assert location.range_width_robust(
        s, lower_percentile=10.0, upper_percentile=90.0
    ) == pytest.approx(s.quantile(0.90) - s.quantile(0.10))


def test_validate_percentiles_accepts_default_and_common_bands():
    location.validate_percentiles(5.0, 95.0)
    location.validate_percentiles(10.0, 90.0)
    location.validate_percentiles(25.0, 75.0)
    location.validate_percentiles(0.0, 100.0)


def test_validate_percentiles_rejects_lower_greater_or_equal_to_upper():
    with pytest.raises(ValueError):
        location.validate_percentiles(95.0, 5.0)
    with pytest.raises(ValueError):
        location.validate_percentiles(50.0, 50.0)


def test_validate_percentiles_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        location.validate_percentiles(-1.0, 95.0)
    with pytest.raises(ValueError):
        location.validate_percentiles(5.0, 101.0)


def test_robust_range_is_narrower_than_full_range_with_an_outlier():
    s = pd.Series([10.0] * 20 + [1000.0])
    assert location.range_width_robust(s) < location.range_width_full(s)


def test_range_position_not_clipped_outside_zero_one():
    assert location.range_position(current=-1.0, low=0.0, high=10.0) == pytest.approx(-0.1)
    assert location.range_position(current=11.0, low=0.0, high=10.0) == pytest.approx(1.1)


def test_range_position_midpoint_is_half():
    assert location.range_position(current=5.0, low=0.0, high=10.0) == pytest.approx(0.5)


def test_range_position_nan_when_width_is_zero():
    assert math.isnan(location.range_position(current=5.0, low=5.0, high=5.0))


def test_z_score_nan_when_std_is_zero():
    assert math.isnan(location.z_score(current=1.0, mean_value=1.0, std_value=0.0))


def test_z_score_nan_when_std_is_nan():
    assert math.isnan(location.z_score(current=1.0, mean_value=1.0, std_value=float("nan")))


def test_z_score_computed_normally():
    assert location.z_score(current=12.0, mean_value=10.0, std_value=2.0) == pytest.approx(1.0)


def test_distance_from_mean():
    assert location.distance_from_mean(current=7.0, mean_value=5.0) == pytest.approx(2.0)


def test_empty_series_returns_nan_for_all_location_metrics():
    s = pd.Series([], dtype=float)
    assert math.isnan(location.range_low_full(s))
    assert math.isnan(location.range_high_full(s))
    assert math.isnan(location.range_width_full(s))
    assert math.isnan(location.range_low_robust(s))
    assert math.isnan(location.range_high_robust(s))
    assert math.isnan(location.range_width_robust(s))
    assert math.isnan(location.mean(s))
    assert math.isnan(location.median(s))


def test_single_observation_width_is_zero_and_position_is_nan():
    s = pd.Series([7.0])
    low, high = location.range_low_full(s), location.range_high_full(s)
    assert location.range_width_full(s) == 0.0
    assert math.isnan(location.range_position(current=7.0, low=low, high=high))
