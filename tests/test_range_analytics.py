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


def test_analyze_range_single_observation():
    history = _history(["2026-01-01"], [5.0])
    result = analyze_range(history)

    assert result.observation_count == 1
    assert result.range_width_full == 0.0
    assert math.isnan(result.range_position_full)
    assert math.isnan(result.realized_vol_price)
    assert math.isnan(result.efficiency_ratio)


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
