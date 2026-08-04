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
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory

from template_scanner.metrics import at_lookback, normalized_crossing_frequency


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
