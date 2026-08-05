"""
tests/test_range_multi_lookback.py

analyze_multi_lookback tested against hand-built StrategyHistory
fixtures -- no I/O, following the same _history() pattern already used
in tests/test_range_analytics.py. Every numeric assertion below was
verified against the actual implementation before being written (see
the design review's Module 4B round for the reasoning behind each
scenario); none are guessed hand-calculations.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest

from core.config import BarInterval
from range_analytics import (
    MultiLookbackAnalytics,
    LookbackStability,
    analyze_multi_lookback,
    analyze_range,
    range_to_volatility_ratio,
    robust_to_full_width_ratio,
)
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory


def _history(dates: list[str], values: list[float], market_key: str = "SOFR") -> StrategyHistory:
    definition = StrategyDefinition(
        market_key=market_key, offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26",))
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Leg_1": values,
            "Strategy": values,
        }
    )
    return StrategyHistory(instance=instance, price_field="Close", history=df)


def _dates(n: int, start: str = "2020-01-01") -> list[str]:
    return pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()


def _fields_equal(a, b) -> bool:
    """NaN-tolerant equality for two like-typed field values (float, str,
    int, BarInterval, pd.Timestamp, or a tuple of RangeAnalytics)."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_fields_equal(x, y) for x, y in zip(a, b))
    if dataclasses.is_dataclass(a) and dataclasses.is_dataclass(b):
        return all(
            _fields_equal(getattr(a, f.name), getattr(b, f.name))
            for f in dataclasses.fields(a)
        )
    return a == b


# ---------------------------------------------------------------------
# 1. Perfectly stable range
# ---------------------------------------------------------------------

def test_stable_range_width_median_vol_crossing_frequency_are_flat():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    assert result.range_width_robust_stability.values == pytest.approx((0.04,) * 5, abs=1e-9)
    assert result.median_stability.values == pytest.approx((1.0,) * 5, abs=1e-9)
    vol_values = result.realized_vol_bp_stability.values
    assert max(vol_values) - min(vol_values) < 0.1  # tightly stable, not exactly flat
    freq_values = result.normalized_crossing_frequency_stability.values
    assert max(freq_values) - min(freq_values) < 0.1
    # ER is NOT expected to be flat for a genuinely stationary series -- its
    # numerator is bounded while its denominator grows with n, so it
    # necessarily trends toward 0 as lookback grows. It should stay small.
    assert all(v < 0.1 for v in result.efficiency_ratio_stability.values)


# ---------------------------------------------------------------------
# 2. Drifting centre (the A-vs-B discriminator)
# ---------------------------------------------------------------------

def test_drifting_centre_moves_median_and_both_bounds_in_the_same_direction():
    n = 200
    ramp = np.linspace(0, 2, n)
    osc = np.array([0.05 if i % 2 == 0 else -0.05 for i in range(n)])
    values = (ramp + osc).tolist()
    history = _history(_dates(n), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    # median at the longest lookback is well below the median at the
    # shortest lookback -- the centre has clearly drifted.
    assert result.median_stability.short_vs_long_diff < -0.3
    # both bounds moved in the same (negative) direction as the median --
    # this is the whole band shifting together, not a one-sided break.
    assert result.range_low_robust_stability.short_vs_long_diff < 0
    assert result.range_high_robust_stability.short_vs_long_diff < 0


# ---------------------------------------------------------------------
# 3. Recent tighter regime nested inside an older wider regime
# ---------------------------------------------------------------------

def test_nested_tighter_regime_shows_pairwise_ratio_jump_at_the_boundary():
    wide = [6, -6] * 50    # 100 bars, oldest
    tight = [1, -1] * 15   # 30 bars, most recent
    values = [float(v) for v in wide + tight]
    history = _history(_dates(130), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    width = result.range_width_robust_stability
    assert width.values == pytest.approx((2.0, 12.0, 12.0, 12.0, 12.0))
    # the ratio jumps sharply at the 20->40 step (crossing into the wide
    # regime) and stays ~1.0 for every step fully inside one regime.
    assert width.pairwise_ratios[0] > 3.0
    assert width.pairwise_ratios[1:] == pytest.approx((1.0, 1.0, 1.0))


# ---------------------------------------------------------------------
# 4. Expanding width (recent wider than the established history)
# ---------------------------------------------------------------------

def test_recent_wider_than_history_shows_negative_short_vs_long_diff():
    tight = [1, -1] * 97 + [1]   # 195 bars of established narrow history
    wide = [6, -6, 6, -6, 6]     # 5 bars of a just-started wide excursion
    values = [float(v) for v in (tight[:195] + wide)]
    history = _history(_dates(200), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 60, 120, 155, 200))

    # short lookback (dominated by the new wide excursion) is far wider
    # than the long lookback (dominated by the old established range).
    assert result.range_width_robust_stability.short_vs_long_diff < -5.0
    # yet the current price is entirely unremarkable within its own
    # short, recent window -- position sits right at the window's own
    # edge, not flagged as an extreme outlier from that vantage point.
    assert result.per_lookback[0].range_position_robust == pytest.approx(1.0)


# ---------------------------------------------------------------------
# 5 & 6. Breakout above / below the robust range
# ---------------------------------------------------------------------

def test_breakout_above_is_mild_at_short_lookback_and_severe_at_long_lookback():
    calm = [0.99, 1.01] * 75  # 150 calm bars
    ramp = [1.5, 2.0, 2.5, 3.0, 3.0]  # 5-bar breakout to the upside
    values = [float(v) for v in (calm + ramp)]
    history = _history(_dates(155), values)

    result = analyze_multi_lookback(history, lookbacks=(5, 20, 60, 100, 155))

    positions = [r.range_position_robust for r in result.per_lookback]
    # at the shortest lookbacks the window IS mostly the breakout itself --
    # current sits at/near the top of its own band, not "far outside" it.
    assert positions[0] == pytest.approx(1.0)
    assert positions[1] == pytest.approx(1.0)
    # at longer lookbacks, dominated by the calm history, the same current
    # price reads as dramatically outside the historical band.
    assert positions[-1] > 50


def test_breakout_below_mirrors_breakout_above():
    calm = [0.99, 1.01] * 75
    ramp = [0.5, 0.0, -0.5, -1.0, -1.0]  # 5-bar breakout to the downside
    values = [float(v) for v in (calm + ramp)]
    history = _history(_dates(155), values)

    result = analyze_multi_lookback(history, lookbacks=(5, 20, 60, 100, 155))

    positions = [r.range_position_robust for r in result.per_lookback]
    assert positions[0] == pytest.approx(0.0)
    assert positions[1] == pytest.approx(0.0)
    assert positions[-1] < -50


# ---------------------------------------------------------------------
# 7. Random walk
# ---------------------------------------------------------------------

def test_random_walk_beta_stays_close_to_one_with_long_half_life():
    rng = np.random.default_rng(42)
    increments = rng.normal(0, 0.01, 400)
    values = (np.cumsum(increments) + 1.0).tolist()
    history = _history(_dates(400), values)

    result = analyze_multi_lookback(history, lookbacks=(60, 120, 200, 300, 400))

    assert all(0.8 <= b <= 1.05 for b in result.ar1_beta_stability.values)
    # deliberately no assertion on efficiency_ratio here -- it is
    # path-dependent and not reliably bounded for a single random-walk
    # realization; an honest non-assertion, not an oversight.


# ---------------------------------------------------------------------
# 8 & 9. Smooth vs. oscillatory mean reversion
# ---------------------------------------------------------------------

def test_smooth_mean_reversion_recovers_known_beta_and_half_life():
    beta_true = 0.7
    values = [100.0 * (beta_true ** t) for t in range(130)]
    history = _history(_dates(130), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    assert result.ar1_beta_stability.values == pytest.approx((beta_true,) * 5, abs=1e-6)
    expected_half_life = math.log(2) / (-math.log(beta_true))
    assert result.half_life_stability.values == pytest.approx(
        (expected_half_life,) * 5, abs=1e-4
    )


def test_oscillatory_mean_reversion_recovers_known_negative_beta():
    beta_true = -0.5
    values = [100.0 * (beta_true ** t) for t in range(130)]
    history = _history(_dates(130), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    assert result.ar1_beta_stability.values == pytest.approx((beta_true,) * 5, abs=1e-6)
    expected_half_life = math.log(2) / (-math.log(abs(beta_true)))
    assert result.half_life_stability.values == pytest.approx(
        (expected_half_life,) * 5, abs=1e-4
    )


# ---------------------------------------------------------------------
# 10. Flat / dead series
# ---------------------------------------------------------------------

def test_flat_dead_series_shows_real_zeros_and_genuine_nans():
    values = [1.0] * 130
    history = _history(_dates(130), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    # width and volatility are real, meaningful zeros -- not NaN.
    assert result.range_width_robust_stability.values == pytest.approx((0.0,) * 5)
    assert result.realized_vol_bp_stability.values == pytest.approx((0.0,) * 5)
    # ER, AR(1) beta, and the range-to-volatility ratio are genuine 0/0 or
    # zero-variance indeterminates -- NaN, not fabricated.
    assert all(math.isnan(v) for v in result.efficiency_ratio_stability.values)
    assert all(math.isnan(v) for v in result.ar1_beta_stability.values)
    assert all(math.isnan(v) for v in result.range_to_volatility_ratio_stability.values)


# ---------------------------------------------------------------------
# 11. Missing observations
# ---------------------------------------------------------------------

def test_missing_observations_reduce_lookbacks_effective():
    values = [float(v) for v in range(1, 126)]
    for idx in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
        values[idx] = float("nan")
    history = _history(_dates(125), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    # 125 rows - 10 NaN = 115 valid observations -- only the longest
    # requested lookback (120) is truncated by the missing data.
    assert result.lookbacks_effective == (20, 40, 60, 90, 115)
    assert [r.observation_count for r in result.per_lookback] == [20, 40, 60, 90, 115]


# ---------------------------------------------------------------------
# 12. Short history (fewer valid observations than the largest lookback)
# ---------------------------------------------------------------------

def test_short_history_truncates_and_over_requested_lookbacks_collapse():
    values = [float(v) for v in range(1, 51)]  # only 50 valid observations
    history = _history(_dates(50), values)

    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    assert result.lookbacks_effective == (20, 40, 50, 50, 50)
    # the three over-requested lookbacks all silently truncate to the same
    # 50-row window -- numerically identical results, not distinct
    # comparisons, exactly the risk lookbacks_effective exists to surface.
    # NaN-aware comparison: RangeAnalytics has no custom __eq__, so plain
    # `==` on two otherwise-identical results fails whenever a field is
    # genuinely NaN in both (e.g. ar1_r_squared, half_life for this
    # perfectly-linear fixture), since NaN != NaN under IEEE 754.
    assert _fields_equal(result.per_lookback[2], result.per_lookback[3])
    assert _fields_equal(result.per_lookback[3], result.per_lookback[4])


# ---------------------------------------------------------------------
# 13. Integration: batched path matches direct analyze_range calls
# ---------------------------------------------------------------------

def test_analyze_multi_lookback_matches_direct_analyze_range_per_lookback():
    values = [1.0, 1.02, 0.99, 1.05, 0.95, 1.03, 0.97, 1.01]
    history = _history(_dates(8), values)
    lookbacks = (2, 4, 6)

    result = analyze_multi_lookback(history, lookbacks=lookbacks)

    for lookback, batched in zip(lookbacks, result.per_lookback):
        direct = analyze_range(history, lookback=lookback)
        assert _fields_equal(batched, direct)


def test_analyze_multi_lookback_forbidden_classification_fields_are_absent():
    forbidden = {
        "regime_age", "regime_start", "range_break", "is_stable",
        "stable_since", "is_range_bound", "range_score", "stability_score",
        "breakout_flag",
    }
    multi_fields = {f.name for f in dataclasses.fields(MultiLookbackAnalytics)}
    stability_fields = {f.name for f in dataclasses.fields(LookbackStability)}
    assert multi_fields.isdisjoint(forbidden)
    assert stability_fields.isdisjoint(forbidden)


# ---------------------------------------------------------------------
# 14. lookbacks validation
# ---------------------------------------------------------------------

def test_analyze_multi_lookback_forwards_percentiles_to_every_per_lookback_result():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)

    result = analyze_multi_lookback(
        history, lookbacks=(20, 40, 60), lower_percentile=25.0, upper_percentile=75.0
    )

    for r in result.per_lookback:
        assert r.lower_percentile == 25.0
        assert r.upper_percentile == 75.0

    # cross-check against a direct analyze_range call at one lookback --
    # the batched path must not silently diverge from the direct path.
    direct = analyze_range(history, lookback=40, lower_percentile=25.0, upper_percentile=75.0)
    assert _fields_equal(result.per_lookback[1], direct)


def test_analyze_multi_lookback_rejects_empty_lookbacks():
    history = _history(_dates(10), [float(v) for v in range(10)])
    with pytest.raises(ValueError):
        analyze_multi_lookback(history, lookbacks=())


def test_analyze_multi_lookback_rejects_non_increasing_lookbacks():
    history = _history(_dates(10), [float(v) for v in range(10)])
    with pytest.raises(ValueError):
        analyze_multi_lookback(history, lookbacks=(40, 20))
    with pytest.raises(ValueError):
        analyze_multi_lookback(history, lookbacks=(20, 20, 40))


def test_analyze_multi_lookback_accepts_a_single_lookback():
    history = _history(_dates(10), [float(v) for v in range(10)])
    result = analyze_multi_lookback(history, lookbacks=(5,))
    assert result.lookbacks_requested == (5,)
    assert result.range_width_robust_stability.pairwise_diffs == ()
    assert math.isnan(result.range_width_robust_stability.short_vs_long_diff) is False


# ---------------------------------------------------------------------
# 15. robust_to_full_width_ratio -- standalone function
# ---------------------------------------------------------------------

def test_robust_to_full_width_ratio_nan_on_genuine_zero_over_zero():
    history = _history(_dates(10), [1.0] * 10)
    result = analyze_range(history, lookback=10)
    assert result.range_width_robust == 0.0
    assert result.range_width_full == 0.0
    assert math.isnan(robust_to_full_width_ratio(result))


def test_robust_to_full_width_ratio_nan_on_single_observation():
    history = _history(_dates(1), [5.0])
    result = analyze_range(history, lookback=1)
    assert math.isnan(robust_to_full_width_ratio(result))


def test_robust_to_full_width_ratio_normal_case_is_between_zero_and_one():
    values = [100.0 + i for i in range(50)] + [1000.0]  # one outlier
    history = _history(_dates(51), values)
    result = analyze_range(history, lookback=51)
    ratio = robust_to_full_width_ratio(result)
    assert 0.0 < ratio < 1.0  # robust range is narrower than the outlier-driven full range


# ---------------------------------------------------------------------
# 16. range_to_volatility_ratio -- corrected interpretation regression guard
# ---------------------------------------------------------------------

def test_range_to_volatility_ratio_is_large_for_a_smooth_trend_not_only_oscillation():
    # A smooth, almost-perfectly-directional trend (ER close to 1, only 1
    # crossing) still produces a large range_to_volatility_ratio -- proof
    # that a large ratio does NOT by itself imply oscillation/range-bound
    # behavior, exactly the corrected interpretation from the design review.
    values = []
    total = 0.0
    for i in range(100):
        step = 0.012 if i % 7 == 0 else 0.01  # tiny genuine (non-float-noise) variation
        total += step
        values.append(total)
    history = _history(_dates(100), values)

    result = analyze_range(history, lookback=100)

    assert result.efficiency_ratio == pytest.approx(1.0)
    assert result.hysteresis_crossing_count <= 1
    assert range_to_volatility_ratio(result) > 100  # large, despite being a pure trend
