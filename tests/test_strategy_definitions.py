"""
tests/test_strategy_definitions.py

Pure validation tests for StrategyDefinition -- no I/O, no mocking.
"""

from __future__ import annotations

import pytest

from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition


def test_outright_definition_is_valid():
    d = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )
    assert d.offsets == (0,)
    assert d.weights == (1,)


def test_spread_definition_is_valid():
    d = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )
    assert d.offsets == (0, 1)
    assert d.weights == (1, -1)
    assert d.price_field == "Close"


def test_fly_definition_is_valid():
    d = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    assert len(d.offsets) == 3


def test_condor_definition_is_valid():
    d = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2, 3), weights=(1, -1, -1, 1), interval=BarInterval.DAILY,
    )
    assert len(d.offsets) == 4


def test_custom_asymmetric_weights_are_valid():
    d = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(2, -5, 3), interval=BarInterval.DAILY,
    )
    assert d.weights == (2, -5, 3)


def test_interval_accepts_str_or_barinterval():
    d1 = StrategyDefinition(market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval="DAILY")
    d2 = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY
    )
    assert d1.interval == BarInterval.DAILY
    assert d2.interval == BarInterval.DAILY


def test_unknown_interval_string_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval="WEEKLY")


def test_unknown_market_raises():
    with pytest.raises(KeyError):
        StrategyDefinition(
            market_key="NOT_A_MARKET", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY
        )


def test_mismatched_offsets_and_weights_length_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(
            market_key="SOFR", offsets=(0, 1, 2), weights=(1, -1), interval=BarInterval.DAILY
        )


def test_zero_legs_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(market_key="SOFR", offsets=(), weights=(), interval=BarInterval.DAILY)


def test_offsets_not_starting_at_zero_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(
            market_key="SOFR", offsets=(1, 2), weights=(1, -1), interval=BarInterval.DAILY
        )


def test_offsets_not_strictly_increasing_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(
            market_key="SOFR", offsets=(0, 1, 1), weights=(1, -2, 1), interval=BarInterval.DAILY
        )


def test_all_zero_weights_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(
            market_key="SOFR", offsets=(0, 1), weights=(0, 0), interval=BarInterval.DAILY
        )


def test_single_zero_weight_outright_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(market_key="SOFR", offsets=(0,), weights=(0,), interval=BarInterval.DAILY)


@pytest.mark.parametrize("price_field", ["Open", "High", "Low", "Close"])
def test_supported_price_fields_are_valid(price_field):
    d = StrategyDefinition(
        market_key="SOFR",
        offsets=(0, 1),
        weights=(1, -1),
        interval=BarInterval.DAILY,
        price_field=price_field,
    )
    assert d.price_field == price_field


def test_unsupported_price_field_raises():
    with pytest.raises(ValueError):
        StrategyDefinition(
            market_key="SOFR",
            offsets=(0, 1),
            weights=(1, -1),
            interval=BarInterval.DAILY,
            price_field="VWAP",
        )
