"""
tests/test_template_scanner_templates.py

template_from_dense_weights tested against hand-built dense weight
vectors -- no I/O, no contract generation, purely the translation into
StrategyDefinition's (offsets, weights) representation.
"""

from __future__ import annotations

import pytest

from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from template_scanner.templates import template_from_dense_weights


def test_two_leg_spread():
    result = template_from_dense_weights("SOFR", (1, -1), BarInterval.DAILY)
    assert result.offsets == (0, 1)
    assert result.weights == (1.0, -1.0)


def test_consecutive_fly():
    result = template_from_dense_weights("SOFR", (1, -2, 1), BarInterval.DAILY)
    assert result.offsets == (0, 1, 2)
    assert result.weights == (1.0, -2.0, 1.0)


def test_four_leg_ratio():
    result = template_from_dense_weights("SOFR", (1, -3, 3, -1), BarInterval.DAILY)
    assert result.offsets == (0, 1, 2, 3)
    assert result.weights == (1.0, -3.0, 3.0, -1.0)


def test_gapped_ratio_matches_direct_offsets():
    result = template_from_dense_weights("SOFR", (1, 0, -2, 0, 1), BarInterval.DAILY)
    assert result.offsets == (0, 2, 4)
    assert result.weights == (1.0, -2.0, 1.0)
    # numerically equivalent to entering offsets=(0,2,4) directly
    direct = StrategyDefinition(
        market_key="SOFR", offsets=(0, 2, 4), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    assert result == direct


def test_asymmetric_non_normalized_weights():
    result = template_from_dense_weights("SOFR", (2, 0, -1, 0, -1), BarInterval.DAILY)
    assert result.offsets == (0, 2, 4)
    assert result.weights == (2.0, -1.0, -1.0)


def test_leading_zeros_are_rebased_away():
    result = template_from_dense_weights("SOFR", (0, 1, -2, 1), BarInterval.DAILY)
    assert result.offsets == (0, 1, 2)
    assert result.weights == (1.0, -2.0, 1.0)


def test_trailing_zeros_are_harmless():
    result = template_from_dense_weights("SOFR", (1, -2, 1, 0, 0), BarInterval.DAILY)
    assert result.offsets == (0, 1, 2)
    assert result.weights == (1.0, -2.0, 1.0)


def test_leading_and_trailing_zeros_give_identical_result_to_bare_shape():
    padded = template_from_dense_weights("SOFR", (0, 0, 1, -2, 1, 0), BarInterval.DAILY)
    bare = template_from_dense_weights("SOFR", (1, -2, 1), BarInterval.DAILY)
    assert padded == bare


def test_single_leg_outright():
    result = template_from_dense_weights("SOFR", (1,), BarInterval.DAILY)
    assert result.offsets == (0,)
    assert result.weights == (1.0,)


def test_all_zero_raises():
    with pytest.raises(ValueError, match="no nonzero weights"):
        template_from_dense_weights("SOFR", (0, 0, 0, 0), BarInterval.DAILY)


def test_all_zero_single_entry_raises():
    with pytest.raises(ValueError, match="no nonzero weights"):
        template_from_dense_weights("SOFR", (0,), BarInterval.DAILY)


def test_reuses_strategy_definition_validation_for_unknown_market():
    with pytest.raises(KeyError):
        template_from_dense_weights("NOT_A_MARKET", (1, -1), BarInterval.DAILY)


def test_reuses_strategy_definition_validation_for_price_field():
    result = template_from_dense_weights(
        "SOFR", (1, -1), BarInterval.DAILY, price_field="Open"
    )
    assert result.price_field == "Open"
    with pytest.raises(ValueError, match="Unsupported price_field"):
        template_from_dense_weights("SOFR", (1, -1), BarInterval.DAILY, price_field="VWAP")


def test_interval_accepts_str_or_barinterval():
    result = template_from_dense_weights("SOFR", (1, -1), "DAILY")
    assert result.interval == BarInterval.DAILY
