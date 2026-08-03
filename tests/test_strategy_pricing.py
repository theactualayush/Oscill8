"""
tests/test_strategy_pricing.py

build_history/generate_histories tested with database.get_history
mocked at the strategy_engine.pricing boundary -- the same "mock
exactly one function at the boundary" pattern used in
tests/test_service.py, since build_history's only I/O call is
database.get_history.
"""

from __future__ import annotations

import pandas as pd
import pytest

import core.downloader as downloader_module
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


def test_build_history_applies_weight_to_single_leg_outright(mocker):
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(-3,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26",))
    dates = ["2026-01-02", "2026-01-05"]
    mocker.patch(
        "strategy_engine.pricing.get_history",
        return_value=_leg_df(dates, [96.80, 96.82]),
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert result.history["Leg_1"].tolist() == [96.80, 96.82]
    assert result.history["Strategy"].tolist() == pytest.approx([-3 * 96.80, -3 * 96.82])


def test_build_history_uses_non_close_price_field(mocker):
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
        price_field="High",
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26", "SRAM26"))
    dates = ["2026-01-02"]

    def _leg_with_distinct_fields(high: float) -> pd.DataFrame:
        df = _leg_df(dates, [999.0])  # Close deliberately wrong, must be ignored
        df["High"] = [high]
        return df

    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[_leg_with_distinct_fields(96.90), _leg_with_distinct_fields(96.70)],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert result.price_field == "High"
    assert result.history["Leg_1"].tolist() == [96.90]
    assert result.history["Leg_2"].tolist() == [96.70]
    assert result.history["Strategy"].tolist() == pytest.approx([96.90 - 96.70])


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


def test_pricing_namespace_never_binds_downloader_functions():
    """Structural boundary check: inspects strategy_engine.pricing's live
    module namespace for the actual core.downloader function objects
    (identity, not name/string matching) -- would catch even a renamed
    import (e.g. `from core.downloader import download_history as x`),
    unlike a source-text search."""
    pricing_values = list(vars(pricing).values())
    assert downloader_module.download_history not in pricing_values
    assert downloader_module.open_lseg_session not in pricing_values
    assert downloader_module.close_lseg_session not in pricing_values


def test_build_history_never_calls_core_downloader_directly(mocker):
    """Behavioral boundary check: patches the real core.downloader.
    download_history to raise if called at all, then exercises
    build_history with database.get_history separately mocked. Would
    fail loudly if pricing ever bypassed database.get_history and
    reached into core.downloader directly."""
    mock_download = mocker.patch(
        "core.downloader.download_history",
        side_effect=AssertionError(
            "strategy_engine.pricing must never call core.downloader directly"
        ),
    )
    mocker.patch(
        "strategy_engine.pricing.get_history",
        return_value=_leg_df(["2026-01-02"], [96.80]),
    )

    instance = _fly_instance()
    pricing.build_history(instance, "2026-01-01", "2026-01-31")

    mock_download.assert_not_called()
