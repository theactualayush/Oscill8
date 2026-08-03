"""
tests/test_range_mean_reversion.py

fit_ar1 tested against deterministic, hand-constructed series with
exactly-known dynamics (no random noise), so the fitted gamma/beta/
half-life can be asserted precisely rather than just "roughly right".
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from range_analytics.mean_reversion import fit_ar1


def test_fit_ar1_recovers_known_smooth_mean_reversion():
    # S_t = 100 * 0.5^t exactly satisfies Delta S_t = gamma * S_(t-1)
    # with gamma = beta - 1 = -0.5, no noise -> perfect fit.
    beta_true = 0.5
    values = [100.0 * (beta_true ** t) for t in range(10)]
    fit = fit_ar1(pd.Series(values))

    assert fit.beta == pytest.approx(beta_true, abs=1e-9)
    assert fit.gamma == pytest.approx(beta_true - 1.0, abs=1e-9)
    assert fit.r_squared == pytest.approx(1.0, abs=1e-6)
    assert fit.half_life == pytest.approx(1.0, abs=1e-9)  # halves every bar


def test_fit_ar1_recovers_known_oscillatory_mean_reversion():
    # Same magnitude of decay as above, but beta < 0 -> sign flips every bar.
    beta_true = -0.5
    values = [100.0 * (beta_true ** t) for t in range(10)]
    fit = fit_ar1(pd.Series(values))

    assert fit.beta == pytest.approx(beta_true, abs=1e-9)
    assert fit.half_life == pytest.approx(1.0, abs=1e-9)


def test_fit_ar1_beta_zero_gives_half_life_of_zero_bars():
    # Hand-solved: S_(t-1)=[100,5,5,5,5], Delta S_t=[-95,0,0,0,0] -> gamma=-1
    # exactly -> beta = 0 exactly (no dependence on own lag at all).
    values = [100.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    fit = fit_ar1(pd.Series(values))

    assert fit.beta == pytest.approx(0.0, abs=1e-9)
    assert fit.half_life == pytest.approx(0.0, abs=1e-9)


def test_fit_ar1_random_walk_beta_equals_one_has_no_half_life():
    # A pure linear trend: Delta S_t is constant, uncorrelated with S_(t-1)
    # variation -> gamma = 0 exactly -> beta = 1 exactly.
    values = [100.0 + 2.0 * t for t in range(10)]
    fit = fit_ar1(pd.Series(values))

    assert fit.beta == pytest.approx(1.0, abs=1e-9)
    assert math.isnan(fit.half_life)


def test_fit_ar1_explosive_beta_greater_than_one_has_no_half_life():
    beta_true = 2.0
    values = [1.0 * (beta_true ** t) for t in range(8)]
    fit = fit_ar1(pd.Series(values))

    assert fit.beta == pytest.approx(beta_true, abs=1e-6)
    assert math.isnan(fit.half_life)


def test_fit_ar1_unstable_oscillation_beta_less_than_negative_one_has_no_half_life():
    beta_true = -2.0
    values = [1.0 * (beta_true ** t) for t in range(8)]
    fit = fit_ar1(pd.Series(values))

    assert fit.beta == pytest.approx(beta_true, abs=1e-6)
    assert math.isnan(fit.half_life)


def test_fit_ar1_nan_below_three_observations():
    fit = fit_ar1(pd.Series([1.0, 2.0]))
    assert math.isnan(fit.gamma)
    assert math.isnan(fit.beta)
    assert math.isnan(fit.half_life)


def test_fit_ar1_std_error_and_r_squared_nan_below_four_observations():
    # n=3 -> 2 pairs -> 0 residual degrees of freedom: gamma is computable
    # (fits exactly), but std_error/r_squared are not.
    fit = fit_ar1(pd.Series([10.0, 12.0, 9.0]))
    assert not math.isnan(fit.gamma)
    assert math.isnan(fit.std_error)
    assert math.isnan(fit.r_squared)


def test_fit_ar1_nan_when_regressor_has_zero_variance():
    # S_(t-1) is constant -> OLS slope is undefined (0/0), regardless of n.
    fit = fit_ar1(pd.Series([5.0, 5.0, 5.0, 5.0]))
    assert math.isnan(fit.gamma)
    assert math.isnan(fit.beta)


def test_fit_ar1_empty_series_is_all_nan():
    fit = fit_ar1(pd.Series([], dtype=float))
    assert math.isnan(fit.gamma)
    assert math.isnan(fit.half_life)
