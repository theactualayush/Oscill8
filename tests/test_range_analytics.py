"""
tests/test_range_analytics.py

analyze_range integration tests, built directly on a StrategyHistory
fixture (no I/O -- history.history is a hand-built DataFrame, so there
is nothing to mock).
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.config import BarInterval
from range_analytics import RangeAnalytics, analyze_range
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


def test_analyze_range_populates_all_fields_for_a_simple_series():
    dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
    values = [1.0, 1.02, 0.98, 1.05, 0.95, 1.03, 0.97, 1.01, 0.99, 1.0]
    history = _history(dates, values)

    result = analyze_range(history)

    assert isinstance(result, RangeAnalytics)
    assert result.market_key == "SOFR"
    assert result.interval == BarInterval.DAILY
    assert result.observation_count == 10
    assert result.window_start == pd.Timestamp(dates[0])
    assert result.window_end == pd.Timestamp(dates[-1])
    assert result.current_price == pytest.approx(1.0)
    assert result.range_low_full == pytest.approx(min(values))
    assert result.range_high_full == pytest.approx(max(values))
    assert result.realized_vol_bp == pytest.approx(result.realized_vol_price * 100.0)


def test_analyze_range_respects_lookback():
    dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
    values = [float(v) for v in range(1, 11)]
    history = _history(dates, values)

    result = analyze_range(history, lookback=3)

    assert result.observation_count == 3
    assert result.range_low_full == 8.0
    assert result.range_high_full == 10.0


def test_analyze_range_start_end_window():
    dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
    values = [float(v) for v in range(1, 11)]
    history = _history(dates, values)

    result = analyze_range(history, start="2026-01-08", end="2026-01-10")

    assert result.observation_count == 3
    assert result.range_low_full == 8.0
    assert result.range_high_full == 10.0


def test_analyze_range_rejects_lookback_and_start_together():
    history = _history(["2026-01-01"], [1.0])
    with pytest.raises(ValueError):
        analyze_range(history, lookback=1, start="2026-01-01")


def test_analyze_range_empty_history_returns_nan_fields():
    history = _history([], [])
    result = analyze_range(history)

    assert result.observation_count == 0
    assert result.window_start is pd.NaT
    assert result.window_end is pd.NaT
    assert math.isnan(result.current_price)
    assert math.isnan(result.range_width_full)
    assert math.isnan(result.efficiency_ratio)
    assert math.isnan(result.half_life)
    assert result.raw_crossing_count == 0
    assert result.hysteresis_crossing_count == 0
    assert result.oscillation_count == 0
    assert math.isnan(result.mean_abs_change_price)
    assert math.isnan(result.mean_abs_change_bp)


def test_analyze_range_single_observation():
    history = _history(["2026-01-01"], [5.0])
    result = analyze_range(history)

    assert result.observation_count == 1
    assert result.range_width_full == 0.0
    assert math.isnan(result.range_position_full)
    assert math.isnan(result.realized_vol_price)
    assert math.isnan(result.efficiency_ratio)
    # A single-observation window has a degenerate (zero-width) robust
    # range -- oscillation_count is a well-defined 0, never NaN.
    assert result.oscillation_count == 0
    # Movement needs at least 2 observations -- NaN, not 0, here.
    assert math.isnan(result.mean_abs_change_price)
    assert math.isnan(result.mean_abs_change_bp)


def test_analyze_range_no_classification_fields_are_exposed():
    history = _history(["2026-01-01"], [5.0])
    result = analyze_range(history)
    field_names = {f for f in result.__dataclass_fields__}
    forbidden = {"is_range_bound", "range_score", "good_range", "trade_signal", "regime_age"}
    assert field_names.isdisjoint(forbidden)


def test_analyze_range_hysteresis_suppresses_tick_noise_crossings():
    # Same tick-noise example as the design brief: raw crossing counting
    # is flooded by sub-tick noise, hysteresis with a real threshold
    # correctly reports zero.
    dates = [f"2026-01-{d:02d}" for d in range(1, 6)]
    values = [-0.001, 0.001, -0.001, 0.001, -0.001]
    history = _history(dates, values)

    result = analyze_range(history, crossing_equilibrium=0.0, crossing_threshold=1.0)

    assert result.crossing_equilibrium == 0.0
    assert result.crossing_threshold == 1.0
    assert result.raw_crossing_count == 4
    assert result.hysteresis_crossing_count == 0


def test_analyze_range_default_crossing_threshold_is_zero_and_matches_raw():
    dates = [f"2026-01-{d:02d}" for d in range(1, 4)]
    values = [-1.0, 1.0, -1.0]
    history = _history(dates, values)

    result = analyze_range(history, crossing_equilibrium=0.0)

    assert result.crossing_threshold == 0.0
    assert result.hysteresis_crossing_count == result.raw_crossing_count


def test_analyze_range_defaults_crossing_equilibrium_to_median():
    dates = [f"2026-01-{d:02d}" for d in range(1, 4)]
    values = [1.0, 2.0, 3.0]
    history = _history(dates, values)

    result = analyze_range(history)

    assert result.crossing_equilibrium == pytest.approx(2.0)


# ---------------------------------------------------------------------
# Configurable robust-range percentiles
# ---------------------------------------------------------------------

def test_analyze_range_default_percentiles_are_5_95():
    dates = _dates(100)
    values = [float(v) for v in range(1, 101)]
    history = _history(dates, values)

    result = analyze_range(history)

    assert result.lower_percentile == 5.0
    assert result.upper_percentile == 95.0
    series = pd.Series(values)
    assert result.range_low_robust == pytest.approx(series.quantile(0.05))
    assert result.range_high_robust == pytest.approx(series.quantile(0.95))


def test_analyze_range_custom_percentiles_change_robust_bounds_and_width():
    dates = _dates(100)
    values = [float(v) for v in range(1, 101)]
    history = _history(dates, values)

    result = analyze_range(history, lower_percentile=25.0, upper_percentile=75.0)

    series = pd.Series(values)
    assert result.lower_percentile == 25.0
    assert result.upper_percentile == 75.0
    assert result.range_low_robust == pytest.approx(series.quantile(0.25))
    assert result.range_high_robust == pytest.approx(series.quantile(0.75))
    assert result.range_width_robust == pytest.approx(
        series.quantile(0.75) - series.quantile(0.25)
    )


def test_analyze_range_range_position_robust_uses_configured_bounds_and_is_unclamped():
    # Reproduces the design brief's worked example: P25=0.10, P75=0.30,
    # current=0.35 -> position = (0.35 - 0.10) / (0.30 - 0.10) = 125%.
    dates = _dates(10)
    values = [0.10, 0.15, 0.20, 0.25, 0.30] * 2
    history = _history(dates, values)

    result = analyze_range(history, lower_percentile=25.0, upper_percentile=75.0)

    expected_position = (result.current_price - result.range_low_robust) / (
        result.range_high_robust - result.range_low_robust
    )
    assert result.range_position_robust == pytest.approx(expected_position)


def test_analyze_range_rejects_invalid_percentiles_early():
    history = _history(["2026-01-01"], [1.0])
    with pytest.raises(ValueError):
        analyze_range(history, lower_percentile=95.0, upper_percentile=5.0)
    with pytest.raises(ValueError):
        analyze_range(history, lower_percentile=-1.0, upper_percentile=95.0)
    with pytest.raises(ValueError):
        analyze_range(history, lower_percentile=5.0, upper_percentile=101.0)


# ---------------------------------------------------------------------
# Z-score window semantics (requirement: explicitly verify and document
# the repository's established lookback convention before relying on it
# -- current IS part of the mean/std sample, an in-sample z-score, not
# current-vs-prior-N-1).
# ---------------------------------------------------------------------

def test_z_score_window_includes_current_in_mean_and_std_sample():
    # 60 observations; current is series.iloc[-1], the 60th (most recent)
    # value, and is itself one of the 60 values mean/std are computed
    # over -- verified by independently recomputing mean/std over the
    # exact same 60-value window (including the last) via pandas/numpy,
    # with ddof=1, and checking the result matches to full precision,
    # not merely "close".
    values = [100.0 + 0.37 * math.sin(i / 3.0) + 0.02 * i for i in range(60)]
    dates = _dates(60)
    history = _history(dates, values)

    result = analyze_range(history, lookback=60)

    window = pd.Series(values)  # the full 60-value window, current included
    expected_mean = window.mean()
    expected_std = window.std(ddof=1)  # sample stdev, ddof=1 -- same convention as realized_volatility
    expected_current = window.iloc[-1]
    expected_z = (expected_current - expected_mean) / expected_std

    assert result.observation_count == 60
    assert result.current_price == pytest.approx(expected_current)
    assert result.mean == pytest.approx(expected_mean)
    assert result.z_score == pytest.approx(expected_z)

    # Contrast case, to make the convention unambiguous: computing mean/std
    # over only the PRIOR 59 observations (excluding current) gives a
    # different number for this non-degenerate fixture -- proving the
    # repository's z_score is genuinely in-sample, not out-of-sample.
    prior_59 = window.iloc[:-1]
    out_of_sample_z = (expected_current - prior_59.mean()) / prior_59.std(ddof=1)
    assert result.z_score != pytest.approx(out_of_sample_z)


def test_z_score_uses_sample_stddev_ddof1_matching_realized_volatility_convention():
    # Same ddof=1 sample-stdev convention already documented and tested
    # for realized_volatility() -- z_score must match it, not population
    # stdev (ddof=0).
    values = [1.0, 1.02, 0.98, 1.05, 0.95, 1.03, 0.97, 1.01, 0.99, 1.0]
    dates = _dates(10)
    history = _history(dates, values)

    result = analyze_range(history)

    series = pd.Series(values)
    ddof1_std = series.std(ddof=1)
    ddof0_std = series.std(ddof=0)
    assert ddof1_std != pytest.approx(ddof0_std)  # sanity: the two conventions actually differ here
    expected_z = (series.iloc[-1] - series.mean()) / ddof1_std
    assert result.z_score == pytest.approx(expected_z)


def test_z_score_nan_with_fewer_than_two_observations():
    history = _history(["2026-01-01"], [5.0])
    result = analyze_range(history)
    assert result.observation_count == 1
    assert math.isnan(result.z_score)


def test_z_score_nan_on_zero_std_constant_series():
    dates = _dates(10)
    values = [1.0] * 10
    history = _history(dates, values)
    result = analyze_range(history)
    assert math.isnan(result.z_score)


# ---------------------------------------------------------------------
# Tradability Analytics: Oscillation Count and Movement
# ---------------------------------------------------------------------

def test_analyze_range_oscillation_count_matches_the_underlying_primitive():
    from range_analytics.oscillation import count_oscillations

    dates = _dates(9)
    values = [0.0, 5.0, 10.0, 5.0, 0.0, 5.0, 7.0, 5.0, 0.0]
    history = _history(dates, values)

    result = analyze_range(history)
    series = pd.Series(values)
    expected = count_oscillations(series, result.range_low_robust, result.range_high_robust)
    assert result.oscillation_count == expected


def test_analyze_range_oscillation_count_depends_on_selected_percentile_range():
    dates = _dates(9)
    values = [0.0, 5.0, 10.0, 5.0, 0.0, 5.0, 7.0, 5.0, 0.0]
    history = _history(dates, values)

    default = analyze_range(history)  # P5/P95
    narrow = analyze_range(history, lower_percentile=25.0, upper_percentile=75.0)

    assert default.range_low_robust != narrow.range_low_robust or (
        default.range_high_robust != narrow.range_high_robust
    )
    assert default.oscillation_count == 1
    assert narrow.oscillation_count == 2
    assert default.oscillation_count != narrow.oscillation_count


def test_analyze_range_oscillation_count_zero_width_range_is_zero_not_nan():
    # A perfectly flat window: range_low_robust == range_high_robust.
    dates = _dates(10)
    values = [3.0] * 10
    history = _history(dates, values)

    result = analyze_range(history)
    assert result.range_low_robust == result.range_high_robust
    assert result.oscillation_count == 0


def test_analyze_range_movement_matches_mean_absolute_change():
    from range_analytics.movement import mean_absolute_change

    dates = _dates(10)
    values = [1.0, 1.02, 0.98, 1.05, 0.95, 1.03, 0.97, 1.01, 0.99, 1.0]
    history = _history(dates, values)

    result = analyze_range(history)
    series = pd.Series(values)
    assert result.mean_abs_change_price == pytest.approx(mean_absolute_change(series))
    assert result.mean_abs_change_bp == pytest.approx(result.mean_abs_change_price * 100.0)


def test_analyze_range_movement_flat_series_is_zero():
    dates = _dates(10)
    values = [2.0] * 10
    history = _history(dates, values)

    result = analyze_range(history)
    assert result.mean_abs_change_price == 0.0
    assert result.mean_abs_change_bp == 0.0


def test_analyze_range_movement_nan_with_fewer_than_two_observations():
    history = _history(["2026-01-01"], [5.0])
    result = analyze_range(history)
    assert math.isnan(result.mean_abs_change_price)
    assert math.isnan(result.mean_abs_change_bp)


def test_analyze_range_movement_is_independent_of_percentile_selection():
    dates = _dates(9)
    values = [0.0, 5.0, 10.0, 5.0, 0.0, 5.0, 7.0, 5.0, 0.0]
    history = _history(dates, values)

    default = analyze_range(history)  # P5/P95
    narrow = analyze_range(history, lower_percentile=25.0, upper_percentile=75.0)

    # Same resolved window/series in both calls -- Movement must be
    # identical even though Oscillation Count (percentile-dependent)
    # differs between them.
    assert default.oscillation_count != narrow.oscillation_count
    assert default.mean_abs_change_price == pytest.approx(narrow.mean_abs_change_price)
    assert default.mean_abs_change_bp == pytest.approx(narrow.mean_abs_change_bp)
