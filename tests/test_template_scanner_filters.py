"""
tests/test_template_scanner_filters.py

FilterCriterion/apply_filters()/accessor factories tested against real
analyze_multi_lookback() output on hand-built StrategyHistory fixtures
-- no I/O, same pattern used in tests/test_range_multi_lookback.py.
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

from template_scanner.filters import FilterCriterion, apply_filters, at_lookback, stability
from template_scanner.scan_results import ScanCandidateResult


def _dates(n: int, start: str = "2020-01-01") -> list[str]:
    return pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()


def _candidate(values: list[float], rics: tuple[str, ...], lookbacks=(10, 30)) -> ScanCandidateResult:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1.0,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=rics)
    df = pd.DataFrame(
        {"Date": pd.to_datetime(_dates(len(values))), "Leg_1": values, "Strategy": values}
    )
    history = StrategyHistory(instance=instance, price_field="Close", history=df)
    multi_lookback = analyze_multi_lookback(history, lookbacks=lookbacks)
    return ScanCandidateResult(
        market_key=definition.market_key,
        rics=rics,
        weights=definition.weights,
        offsets=definition.offsets,
        interval=definition.interval,
        price_field="Close",
        instance=instance,
        multi_lookback=multi_lookback,
    )


# Trending: monotonic -> high efficiency ratio, few/no crossings.
_TRENDING = _candidate([100.0 + 0.1 * i for i in range(100)], ("SRAH26",))
# Oscillating: repeated 3-value cycle -> low efficiency ratio, many crossings.
_OSCILLATING = _candidate(([98.0, 100.0, 102.0] * 40)[:100], ("SRAM26",))
# Single observation -> efficiency_ratio (and most derived metrics) NaN,
# per the existing Module 4A contract (test_analyze_range_single_observation).
_SHORT = _candidate([100.0], ("SRAU26",))


def test_apply_filters_no_criteria_returns_all_candidates():
    result = apply_filters([_TRENDING, _OSCILLATING, _SHORT])
    assert result == [_TRENDING, _OSCILLATING, _SHORT]


def test_apply_filters_min_only():
    er_trending = at_lookback("efficiency_ratio", 30)(_TRENDING)
    criterion = FilterCriterion("ER >= trending's", at_lookback("efficiency_ratio", 30), min_value=er_trending)
    result = apply_filters([_TRENDING, _OSCILLATING], [criterion])
    assert _TRENDING in result
    assert _OSCILLATING not in result


def test_apply_filters_max_only():
    er_oscillating = at_lookback("efficiency_ratio", 30)(_OSCILLATING)
    criterion = FilterCriterion("ER <= oscillating's", at_lookback("efficiency_ratio", 30), max_value=er_oscillating)
    result = apply_filters([_TRENDING, _OSCILLATING], [criterion])
    assert _OSCILLATING in result
    assert _TRENDING not in result


def test_apply_filters_boundary_is_inclusive():
    value = at_lookback("efficiency_ratio", 30)(_TRENDING)
    criterion = FilterCriterion("exact bound", at_lookback("efficiency_ratio", 30), min_value=value, max_value=value)
    result = apply_filters([_TRENDING], [criterion])
    assert result == [_TRENDING]


def test_apply_filters_combined_criteria_are_and():
    accessor = at_lookback("efficiency_ratio", 30)
    er_trending = accessor(_TRENDING)
    # A criterion the trending candidate itself fails (max below its own value)
    # combined with one it passes -- overall must fail.
    criteria = [
        FilterCriterion("passes", accessor, min_value=0.0),
        FilterCriterion("fails", accessor, max_value=er_trending - 0.01),
    ]
    result = apply_filters([_TRENDING], criteria)
    assert result == []


def test_apply_filters_nan_metric_fails_only_that_filter():
    criterion = FilterCriterion("ER", at_lookback("efficiency_ratio", 30), min_value=0.0)
    filtered = apply_filters([_SHORT], [criterion])
    assert filtered == []

    # unfiltered, the same candidate is still returned
    assert apply_filters([_SHORT]) == [_SHORT]


def test_at_lookback_accessor_reads_field_at_the_requested_lookback():
    accessor = at_lookback("observation_count", 10)
    assert accessor(_TRENDING) == 10  # tail(10) of 100 valid observations


def test_stability_accessor_reads_stability_scalar_field():
    accessor = stability("efficiency_ratio", "stdev")
    expected = _TRENDING.multi_lookback.efficiency_ratio_stability.stdev
    value = accessor(_TRENDING)
    assert value == pytest.approx(expected, nan_ok=True)


def test_filter_on_stability_accessor():
    accessor = stability("efficiency_ratio", "stdev")
    value = accessor(_TRENDING)
    criterion = FilterCriterion("stable ER", accessor, max_value=value)
    result = apply_filters([_TRENDING], [criterion])
    assert result == [_TRENDING]
