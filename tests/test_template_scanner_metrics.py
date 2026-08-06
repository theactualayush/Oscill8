"""
tests/test_template_scanner_metrics.py

at_lookback()/normalized_crossing_frequency() tested against real
analyze_multi_lookback() output on hand-built StrategyHistory fixtures
-- no I/O, same _history()/_dates() pattern used in
tests/test_range_multi_lookback.py.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.config import BarInterval
from range_analytics import analyze_multi_lookback
from range_analytics.multi_lookback import range_to_volatility_ratio, robust_to_full_width_ratio
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory

from template_scanner.metrics import (
    abs_z_score,
    at_lookback,
    metric_value,
    normalized_crossing_frequency,
)


def _history(dates: list[str], values: list[float]) -> StrategyHistory:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26",))
    df = pd.DataFrame(
        {"Date": pd.to_datetime(dates), "Leg_1": values, "Strategy": values}
    )
    return StrategyHistory(instance=instance, price_field="Close", history=df)


def _dates(n: int, start: str = "2020-01-01") -> list[str]:
    return pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()


def test_at_lookback_returns_matching_range_analytics():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60))

    assert at_lookback(result, 20) is result.per_lookback[0]
    assert at_lookback(result, 40) is result.per_lookback[1]
    assert at_lookback(result, 60) is result.per_lookback[2]


def test_at_lookback_raises_for_unrequested_lookback():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60))

    with pytest.raises(ValueError, match="90"):
        at_lookback(result, 90)


def test_normalized_crossing_frequency_matches_formula():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    result = analyze_multi_lookback(history, lookbacks=(20, 40))

    for range_analytics in result.per_lookback:
        expected = range_analytics.hysteresis_crossing_count / (
            range_analytics.observation_count - 1
        )
        assert normalized_crossing_frequency(range_analytics) == pytest.approx(expected)


def test_normalized_crossing_frequency_nan_below_two_observations():
    history = _history(_dates(1), [1.0])
    result = analyze_multi_lookback(history, lookbacks=(1,))

    assert result.per_lookback[0].observation_count == 1
    assert math.isnan(normalized_crossing_frequency(result.per_lookback[0]))


def test_normalized_crossing_frequency_matches_stability_source_values():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    result = analyze_multi_lookback(history, lookbacks=(20, 40, 60))

    recomputed = tuple(normalized_crossing_frequency(r) for r in result.per_lookback)
    assert recomputed == result.normalized_crossing_frequency_stability.values


# ---------------------------------------------------------------------
# metric_value: canonical direct-field / derived-metric resolver
# ---------------------------------------------------------------------

def test_metric_value_resolves_direct_field():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20, 40)).per_lookback[0]

    assert metric_value(analytics, "efficiency_ratio") == analytics.efficiency_ratio
    assert metric_value(analytics, "ar1_beta") == pytest.approx(analytics.ar1_beta, nan_ok=True)


def test_metric_value_resolves_normalized_crossing_frequency():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20, 40)).per_lookback[0]

    assert metric_value(analytics, "normalized_crossing_frequency") == pytest.approx(
        normalized_crossing_frequency(analytics), nan_ok=True
    )


def test_metric_value_resolves_range_to_volatility_ratio():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20, 40)).per_lookback[0]

    assert metric_value(analytics, "range_to_volatility_ratio") == pytest.approx(
        range_to_volatility_ratio(analytics), nan_ok=True
    )


def test_metric_value_resolves_robust_to_full_width_ratio():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20, 40)).per_lookback[0]

    assert metric_value(analytics, "robust_to_full_width_ratio") == pytest.approx(
        robust_to_full_width_ratio(analytics), nan_ok=True
    )


# ---------------------------------------------------------------------
# z_score / abs_z_score: deterministic synthetic data, hand-verifiable
# ---------------------------------------------------------------------

def test_metric_value_resolves_z_score_as_direct_field():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20, 40)).per_lookback[0]

    assert metric_value(analytics, "z_score") == analytics.z_score


def test_abs_z_score_positive_current_above_mean():
    # A monotonic ramp: current (last value) is well above the window's
    # mean -> positive z, and abs_z_score equals it exactly.
    values = [float(v) for v in range(1, 21)]  # 1..20, current=20, mean=10.5
    history = _history(_dates(20), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20,)).per_lookback[0]

    series = pd.Series(values)
    expected_z = (series.iloc[-1] - series.mean()) / series.std(ddof=1)
    assert expected_z > 0
    assert analytics.z_score == pytest.approx(expected_z)
    assert abs_z_score(analytics) == pytest.approx(expected_z)
    assert metric_value(analytics, "abs_z_score") == pytest.approx(expected_z)


def test_abs_z_score_negative_current_below_mean():
    # A monotonic decline: current (last value) is well below the mean
    # -> negative z; abs_z_score flips the sign to positive.
    values = [float(v) for v in range(20, 0, -1)]  # 20..1, current=1, mean=10.5
    history = _history(_dates(20), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20,)).per_lookback[0]

    series = pd.Series(values)
    expected_z = (series.iloc[-1] - series.mean()) / series.std(ddof=1)
    assert expected_z < 0
    assert analytics.z_score == pytest.approx(expected_z)
    assert abs_z_score(analytics) == pytest.approx(abs(expected_z))
    assert metric_value(analytics, "abs_z_score") == pytest.approx(abs(expected_z))


def test_z_score_zero_when_current_equals_mean():
    # Symmetric series around its own mean, engineered so the LAST value
    # (current) lands exactly on the mean -- deterministic, hand-verifiable.
    values = [10.0, 8.0, 12.0, 9.0, 11.0, 10.0]  # mean == 10.0, last == 10.0
    history = _history(_dates(6), values)
    analytics = analyze_multi_lookback(history, lookbacks=(6,)).per_lookback[0]

    assert analytics.mean == pytest.approx(10.0)
    assert analytics.current_price == pytest.approx(10.0)
    assert analytics.z_score == pytest.approx(0.0)
    assert abs_z_score(analytics) == pytest.approx(0.0)


def test_abs_z_score_nan_propagates_from_z_score():
    # Single observation -> std undefined -> z_score NaN -> abs_z_score
    # must also be NaN (abs(nan) == nan), not raise or default to 0.
    history = _history(_dates(1), [1.0])
    analytics = analyze_multi_lookback(history, lookbacks=(1,)).per_lookback[0]

    assert math.isnan(analytics.z_score)
    assert math.isnan(abs_z_score(analytics))
    assert math.isnan(metric_value(analytics, "abs_z_score"))


def test_z_score_and_abs_z_score_computed_independently_per_lookback():
    # A ramp with a late kink: the 20-bar window is dominated by the
    # kink (current close to its own short-window mean -> small |Z|),
    # while the 60-bar window's mean is pulled down by the earlier flat
    # section (current further from that longer-window mean -> larger
    # |Z|) -- z_score/abs_z_score must differ meaningfully per lookback,
    # not be computed once and reused.
    values = [100.0] * 40 + [100.0 + 0.5 * i for i in range(20)]
    history = _history(_dates(60), values)
    result = analyze_multi_lookback(history, lookbacks=(20, 60))

    z20 = result.per_lookback[0].z_score
    z60 = result.per_lookback[1].z_score
    assert z20 != pytest.approx(z60)
    assert abs_z_score(result.per_lookback[0]) != pytest.approx(abs_z_score(result.per_lookback[1]))


# ---------------------------------------------------------------------
# Tradability Analytics: Oscillation Count / Movement resolve as direct
# RangeAnalytics fields (no new derived-metric registration needed).
# ---------------------------------------------------------------------

def test_metric_value_resolves_oscillation_count_as_direct_field():
    values = [0.0, 5.0, 10.0, 5.0, 0.0, 5.0, 7.0, 5.0, 0.0]
    history = _history(_dates(9), values)
    analytics = analyze_multi_lookback(history, lookbacks=(9,)).per_lookback[0]

    assert metric_value(analytics, "oscillation_count") == analytics.oscillation_count


def test_metric_value_resolves_mean_abs_change_bp_as_direct_field():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20, 40)).per_lookback[0]

    assert metric_value(analytics, "mean_abs_change_bp") == pytest.approx(
        analytics.mean_abs_change_bp, nan_ok=True
    )


def test_metric_value_unknown_field_raises_attribute_error():
    values = ([0.98, 1.00, 1.02] * 50)[:150]
    history = _history(_dates(150), values)
    analytics = analyze_multi_lookback(history, lookbacks=(20,)).per_lookback[0]

    with pytest.raises(AttributeError):
        metric_value(analytics, "not_a_real_metric")
