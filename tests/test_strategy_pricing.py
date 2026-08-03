"""
tests/test_strategy_pricing.py

build_history/generate_histories tested with database.get_history
mocked at the strategy_engine.pricing boundary -- the same "mock
exactly one function at the boundary" pattern used in
tests/test_service.py, since build_history's only I/O call is
database.get_history.
"""

from __future__ import annotations

import ast
import inspect

import pandas as pd
import pytest

from core.config import BarInterval
from strategy_engine import pricing
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition

_EMPTY_HISTORY = pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])


def _leg_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1000.0] * n,
        }
    )


def _fly_instance() -> StrategyInstance:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    return StrategyInstance(definition=definition, rics=("SRAH26", "SRAM26", "SRAU26"))


def test_build_history_computes_weighted_sum_when_fully_aligned(mocker):
    instance = _fly_instance()
    dates = ["2026-01-02", "2026-01-05"]
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            _leg_df(dates, [96.800, 96.820]),
            _leg_df(dates, [96.720, 96.730]),
            _leg_df(dates, [96.650, 96.650]),
        ],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert list(result.history["Date"]) == list(pd.to_datetime(dates))
    assert result.history["Leg_1"].tolist() == [96.800, 96.820]
    assert result.history["Leg_2"].tolist() == [96.720, 96.730]
    assert result.history["Leg_3"].tolist() == [96.650, 96.650]
    expected_strategy = [
        96.800 - 2 * 96.720 + 96.650,
        96.820 - 2 * 96.730 + 96.650,
    ]
    assert result.history["Strategy"].tolist() == pytest.approx(expected_strategy)
    assert result.instance is instance
    assert result.price_field == "Close"


def test_build_history_drops_timestamp_missing_from_any_leg(mocker):
    instance = _fly_instance()
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            _leg_df(["2026-01-02", "2026-01-05"], [96.80, 96.82]),
            _leg_df(["2026-01-02"], [96.72]),  # missing 2026-01-05
            _leg_df(["2026-01-02", "2026-01-05"], [96.65, 96.65]),
        ],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert len(result.history) == 1
    assert result.history["Date"].iloc[0] == pd.Timestamp("2026-01-02")


def test_build_history_empty_leg_produces_empty_result_not_error(mocker):
    instance = _fly_instance()
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            _leg_df(["2026-01-02"], [96.80]),
            _EMPTY_HISTORY,
            _leg_df(["2026-01-02"], [96.65]),
        ],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert result.history.empty
    assert list(result.history.columns) == ["Date", "Leg_1", "Leg_2", "Leg_3", "Strategy"]


@pytest.mark.parametrize("interval", [BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR])
def test_alignment_is_interval_agnostic(mocker, interval):
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=interval,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26", "SRAM26"))
    timestamps = ["2026-01-02 08:00:00", "2026-01-02 12:00:00"]
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            _leg_df(timestamps, [96.80, 96.81]),
            _leg_df([timestamps[0]], [96.72]),  # missing the second (12:00) bar
        ],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert len(result.history) == 1
    assert result.history["Date"].iloc[0] == pd.Timestamp(timestamps[0])


def test_generate_histories_fetches_each_distinct_leg_once(mocker):
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )
    instance_a = StrategyInstance(definition=definition, rics=("SRAH26", "SRAM26"))
    instance_b = StrategyInstance(definition=definition, rics=("SRAM26", "SRAU26"))
    mock_get_history = mocker.patch(
        "strategy_engine.pricing.get_history",
        return_value=_leg_df(["2026-01-02"], [96.80]),
    )

    pricing.generate_histories([instance_a, instance_b], "2026-01-01", "2026-01-31")

    # 3 distinct RICs across the two instances (SRAH26, SRAM26, SRAU26),
    # not 4 (2 instances x 2 legs each) -- SRAM26 is shared and fetched once.
    assert mock_get_history.call_count == 3


def test_pricing_module_never_imports_lseg_or_downloader():
    # Parses actual import statements rather than substring-matching the
    # whole source, since the module's own docstring legitimately
    # mentions "LSEG"/"core.downloader" when explaining why it avoids them.
    tree = ast.parse(inspect.getsource(pricing))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any("lseg" in name for name in imported)
    assert not any(name.endswith("downloader") for name in imported)
