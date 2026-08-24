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


def test_build_history_drops_date_with_nan_price_in_one_leg(mocker):
    """A Date a leg technically HAS a row for, but with a NaN price (a
    vendor data-quality gap on an otherwise-normal trading date), must
    be excluded from the synthetic Strategy series exactly like a Date
    the leg never had a bar for at all -- see the module docstring's
    valid-observation invariant.
    """
    instance = _fly_instance()
    dates = ["2026-01-02", "2026-01-05"]
    leg2 = _leg_df(dates, [96.72, 96.73])
    leg2.loc[leg2["Date"] == pd.Timestamp("2026-01-05"), "Close"] = float("nan")
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            _leg_df(dates, [96.80, 96.82]),
            leg2,
            _leg_df(dates, [96.65, 96.65]),
        ],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert len(result.history) == 1
    assert result.history["Date"].iloc[0] == pd.Timestamp("2026-01-02")
    assert result.history["Strategy"].tolist() == pytest.approx([96.80 - 2 * 96.72 + 96.65])


def test_build_history_intersection_combines_missing_date_and_nan_price(mocker):
    """Three legs, each excluding a DIFFERENT date via a different
    mechanism (a plain missing row vs. a NaN-priced row) -- only the
    date every leg agrees on survives, confirming the intersection-of-
    valid-dates policy holds when both exclusion mechanisms are mixed
    together, not just individually.
    """
    instance = _fly_instance()
    dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
    leg1 = _leg_df(dates, [96.80, 96.82, 96.83])  # fully valid
    leg2 = _leg_df(["2026-01-02", "2026-01-06"], [96.72, 96.74])  # 01-05 missing entirely
    leg3 = _leg_df(dates, [96.65, 96.66, 96.67])
    leg3.loc[leg3["Date"] == pd.Timestamp("2026-01-06"), "Close"] = float("nan")  # 01-06 NaN price

    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[leg1, leg2, leg3],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert len(result.history) == 1
    assert result.history["Date"].iloc[0] == pd.Timestamp("2026-01-02")


def test_build_history_never_forward_fills_a_nan_price(mocker):
    """The excluded Date's Strategy value must not be resurrected via
    any prior valid price -- it should simply be absent, not filled
    with 2026-01-02's price or interpolated between neighbours.
    """
    instance = _fly_instance()
    dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
    leg2 = _leg_df(dates, [96.72, 96.73, 96.75])
    leg2.loc[leg2["Date"] == pd.Timestamp("2026-01-05"), "Close"] = float("nan")
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            _leg_df(dates, [96.80, 96.82, 96.84]),
            leg2,
            _leg_df(dates, [96.65, 96.66, 96.67]),
        ],
    )

    result = pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert list(result.history["Date"]) == [pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-06")]
    assert not result.history["Strategy"].isna().any()
    assert 96.73 not in result.history["Leg_2"].tolist()


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
    leg = _leg_df(["2026-01-02"], [96.80])
    mock_get_history_batch = mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        return_value={"SRAH26": leg, "SRAM26": leg, "SRAU26": leg},
    )

    pricing.generate_histories([instance_a, instance_b], "2026-01-01", "2026-01-31")

    # prewarm_leg_cache batches the whole scan's distinct RICs into ONE
    # get_history_batch call (not one get_history call per leg) -- 3
    # distinct RICs across the two instances (SRAH26, SRAM26, SRAU26),
    # not 4 (2 instances x 2 legs each): SRAM26 is shared and requested
    # once within that one batch call.
    assert mock_get_history_batch.call_count == 1
    requested_rics = mock_get_history_batch.call_args[0][0]
    assert set(requested_rics) == {"SRAH26", "SRAM26", "SRAU26"}


def test_prewarm_leg_cache_groups_rics_by_interval(mocker):
    """One StrategySet-style call can legitimately mix intervals across
    entries (e.g. a SOFR DAILY entry alongside a SONIA HOURLY entry) --
    prewarm_leg_cache must issue one get_history_batch call PER DISTINCT
    interval, never merging rics across intervals into a single call
    (which would corrupt QuantHub's per-interval batching) and never
    issuing more than one call for the same interval."""
    daily_def = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )
    hourly_def = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1,), interval=BarInterval.HOURLY,
    )
    daily_instance = StrategyInstance(definition=daily_def, rics=("SRAH26", "SRAM26"))
    hourly_instance = StrategyInstance(definition=hourly_def, rics=("SRAU26",))
    leg = _leg_df(["2026-01-02"], [96.80])

    mock_batch = mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=lambda rics, interval, start, end: {ric: leg for ric in rics},
    )

    leg_cache = pricing.prewarm_leg_cache(
        [daily_instance, hourly_instance], "2026-01-01", "2026-01-31"
    )

    assert mock_batch.call_count == 2
    calls_by_interval = {call.args[1]: set(call.args[0]) for call in mock_batch.call_args_list}
    assert calls_by_interval == {
        BarInterval.DAILY: {"SRAH26", "SRAM26"},
        BarInterval.HOURLY: {"SRAU26"},
    }
    assert leg_cache[("SRAH26", "DAILY", "2026-01-01", "2026-01-31")] is leg
    assert leg_cache[("SRAU26", "HOURLY", "2026-01-01", "2026-01-31")] is leg


def test_prewarm_leg_cache_populated_keys_are_reused_by_build_history(mocker):
    """A key prewarm_leg_cache populates must be consumed by
    build_history/_fetch_leg without triggering any further
    get_history()/get_history_batch() call -- proves the cache-key
    convention between the two functions actually matches, not just
    that each independently uses the same-looking tuple shape."""
    instance = StrategyInstance(
        definition=StrategyDefinition(
            market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
        ),
        rics=("SRAH26", "SRAM26"),
    )
    leg = _leg_df(["2026-01-02"], [96.80])
    mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        return_value={"SRAH26": leg, "SRAM26": leg},
    )
    mock_get_history = mocker.patch("strategy_engine.pricing.get_history")

    leg_cache = pricing.prewarm_leg_cache([instance], "2026-01-01", "2026-01-31")
    pricing.build_history(instance, "2026-01-01", "2026-01-31", leg_cache=leg_cache)

    mock_get_history.assert_not_called()


def test_build_history_propagates_market_data_unavailable_error_unchanged(mocker):
    # No Module 3 handling for this exception -- it's typed and raised
    # entirely inside core.downloader (Module 5B.1); build_history must
    # propagate it exactly like any other database.get_history exception.
    instance = _fly_instance()
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=downloader_module.MarketDataUnavailableError(
            "SRAH26", "The universe is not found"
        ),
    )

    with pytest.raises(downloader_module.MarketDataUnavailableError) as exc_info:
        pricing.build_history(instance, "2026-01-01", "2026-01-31")

    assert exc_info.value.ric == "SRAH26"


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
