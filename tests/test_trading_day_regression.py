"""
tests/test_trading_day_regression.py

End-to-end regression for the trading-day / valid-observation invariant
(data-integrity phase): a mocked leg history spanning a REAL weekend is
run through the actual production chain --

    database.get_history (mocked at the strategy_engine.pricing
    boundary, the same pattern test_strategy_pricing.py uses)
        -> strategy_engine.pricing.build_history
        -> range_analytics.results.analyze_range

-- and the resulting RangeAnalytics is checked against values computed
directly from the same underlying array. This is a single continuous
chain, not the piecewise per-module coverage the rest of the suite
already provides -- it exists to catch an integration-level regression
(e.g. an accidental reindex/fill introduced at a module boundary) that
no individual module's own unit tests would surface.

Dates are generated with pd.bdate_range (real business days only) so
the fixture inherently spans "Friday -> Monday" at every week boundary
-- no manual weekday arithmetic, and no synthetic weekend row is ever
constructed, matching how LSEG's own historical responses behave.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.config import BarInterval
from range_analytics import movement, oscillation
from range_analytics.results import analyze_range

from strategy_engine import pricing
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition

_VALUES = [
    100.00, 100.15, 99.90, 100.30, 100.10,  # week 1: Mon-Fri
    100.05, 100.40, 100.20, 99.95, 100.25,  # week 2: Mon-Fri
]


def _leg_df(dates: pd.DatetimeIndex, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1000.0] * len(closes),
        }
    )


def _outright_instance() -> StrategyInstance:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )
    return StrategyInstance(definition=definition, rics=("SRAH26",))


@pytest.fixture
def weekend_spanning_history(mocker):
    dates = pd.bdate_range("2026-01-05", periods=len(_VALUES))  # two full trading weeks
    mocker.patch(
        "strategy_engine.pricing.get_history",
        return_value=_leg_df(dates, _VALUES),
    )
    instance = _outright_instance()
    history = pricing.build_history(instance, "2026-01-01", "2026-01-31")
    return dates, history


def test_regression_no_weekend_row_enters_the_strategy_history(weekend_spanning_history):
    dates, history = weekend_spanning_history
    assert list(history.history["Date"]) == list(dates)
    assert all(d.weekday() < 5 for d in history.history["Date"])


def test_regression_observation_count_matches_actual_bars_not_calendar_days(weekend_spanning_history):
    dates, history = weekend_spanning_history
    analytics = analyze_range(history, lookback=len(_VALUES))

    assert analytics.observation_count == len(_VALUES)
    # The calendar span covers 2 weekends (4 non-trading days) that were
    # never fetched/stored/joined in the first place -- observation_count
    # must reflect only the 10 real bars, not the 14-calendar-day span.
    calendar_days = (dates[-1] - dates[0]).days + 1
    assert calendar_days > len(_VALUES)


def test_regression_movement_treats_friday_monday_as_consecutive(weekend_spanning_history):
    _, history = weekend_spanning_history
    analytics = analyze_range(history, lookback=len(_VALUES))

    expected = movement.mean_absolute_change(pd.Series(_VALUES))
    assert analytics.mean_abs_change_price == pytest.approx(expected)
    # Directly confirm the Friday(idx 4) -> Monday(idx 5) step is one of
    # the consecutive diffs actually averaged -- not skipped, and not
    # padded with an intervening zero for the weekend.
    diffs = np.diff(_VALUES)
    friday_to_monday = abs(_VALUES[5] - _VALUES[4])
    assert friday_to_monday in np.abs(diffs)
    assert analytics.mean_abs_change_price == pytest.approx(float(np.abs(diffs).mean()))


def test_regression_oscillation_count_ignores_absent_weekend(weekend_spanning_history):
    _, history = weekend_spanning_history
    analytics = analyze_range(history, lookback=len(_VALUES))

    series = pd.Series(_VALUES)
    expected = oscillation.count_oscillations(
        series, analytics.range_low_robust, analytics.range_high_robust
    )
    assert analytics.oscillation_count == expected


def test_regression_analytics_match_hand_computed_valid_observation_values(weekend_spanning_history):
    _, history = weekend_spanning_history
    analytics = analyze_range(history, lookback=len(_VALUES))

    series = pd.Series(_VALUES)
    assert analytics.mean == pytest.approx(float(series.mean()))
    assert analytics.median == pytest.approx(float(series.median()))
    assert analytics.range_low_full == pytest.approx(float(series.min()))
    assert analytics.range_high_full == pytest.approx(float(series.max()))
    assert analytics.current_price == pytest.approx(_VALUES[-1])
