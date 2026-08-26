"""
tests/test_intermarket_definitions.py

Pure validation tests for LegSpec/IntermarketDefinition -- no I/O, no
mocking. Mirrors tests/test_strategy_definitions.py's style.
"""

from __future__ import annotations

import pytest

from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import (
    IntermarketDefinition,
    LegSpec,
    resolve_display_market_key,
    resolve_display_offsets,
)


def test_single_leg_intermarket_definition_is_valid():
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0),), interval=BarInterval.DAILY,
    )
    assert d.legs == (LegSpec("SOFR", 0, 1.0),)
    assert d.weights == (1.0,)
    assert d.market_keys == ("SOFR",)


def test_two_leg_intermarket_definition_is_valid():
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    assert d.market_keys == ("SOFR", "SONIA")
    assert d.weights == (1.0, -1.0)
    assert d.price_field == "Close"
    assert d.bp_per_point is None


def test_legs_with_different_offsets_are_valid():
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 1, 1.0), LegSpec("CORRA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    assert [leg.offset for leg in d.legs] == [1, 0]


def test_legs_field_is_coerced_to_tuple():
    d = IntermarketDefinition(
        legs=[LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)],
        interval=BarInterval.DAILY,
    )
    assert isinstance(d.legs, tuple)


# ---------------------------------------------------------------------
# Structural note on "mismatched lengths": StrategyDefinition has three
# separate parallel tuples (offsets, weights, over one shared market_key)
# that could disagree in length. LegSpec deliberately bundles
# (market_key, offset, weight) together per leg, so there is no separate
# array whose length could mismatch another -- this failure mode is
# impossible by construction. The closest structural equivalent is
# rejecting a non-LegSpec element in `legs`, tested below.
# ---------------------------------------------------------------------

def test_non_legspec_element_rejected():
    with pytest.raises(TypeError):
        IntermarketDefinition(
            legs=(LegSpec("SOFR", 0, 1.0), ("SONIA", 0, -1.0)),
            interval=BarInterval.DAILY,
        )


def test_zero_legs_raises():
    with pytest.raises(ValueError):
        IntermarketDefinition(legs=(), interval=BarInterval.DAILY)


def test_unknown_market_raises():
    with pytest.raises(KeyError):
        LegSpec("NOT_A_MARKET", 0, 1.0)


def test_negative_offset_rejected():
    with pytest.raises(ValueError):
        LegSpec("SOFR", -1, 1.0)


def test_non_int_offset_rejected():
    with pytest.raises(TypeError):
        LegSpec("SOFR", 0.5, 1.0)


def test_no_leg_anchored_at_zero_raises():
    with pytest.raises(ValueError):
        IntermarketDefinition(
            legs=(LegSpec("SOFR", 1, 1.0), LegSpec("SONIA", 2, -1.0)),
            interval=BarInterval.DAILY,
        )


def test_all_zero_weights_raises():
    with pytest.raises(ValueError):
        IntermarketDefinition(
            legs=(LegSpec("SOFR", 0, 0.0), LegSpec("SONIA", 0, 0.0)),
            interval=BarInterval.DAILY,
        )


def test_single_zero_weight_leg_raises():
    with pytest.raises(ValueError):
        IntermarketDefinition(legs=(LegSpec("SOFR", 0, 0.0),), interval=BarInterval.DAILY)


def test_interval_accepts_str_or_barinterval():
    d1 = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)), interval="DAILY",
    )
    d2 = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)), interval=BarInterval.DAILY,
    )
    assert d1.interval == BarInterval.DAILY
    assert d2.interval == BarInterval.DAILY


def test_unknown_interval_string_raises():
    with pytest.raises(ValueError):
        IntermarketDefinition(
            legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)), interval="WEEKLY",
        )


@pytest.mark.parametrize("price_field", ["Open", "High", "Low", "Close"])
def test_supported_price_fields_are_valid(price_field):
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
        price_field=price_field,
    )
    assert d.price_field == price_field


def test_unsupported_price_field_raises():
    with pytest.raises(ValueError):
        IntermarketDefinition(
            legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
            interval=BarInterval.DAILY,
            price_field="VWAP",
        )


def test_bp_per_point_defaults_to_none():
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)), interval=BarInterval.DAILY,
    )
    assert d.bp_per_point is None


def test_bp_per_point_explicit_override_accepted():
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
        bp_per_point=100.0,
    )
    assert d.bp_per_point == 100.0


def test_non_positive_bp_per_point_raises():
    with pytest.raises(ValueError):
        IntermarketDefinition(
            legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
            interval=BarInterval.DAILY,
            bp_per_point=0.0,
        )


# ---------------------------------------------------------------------
# resolve_display_market_key / resolve_display_offsets -- dispatched
# purely by TYPE (isinstance), never by inspecting a market_key VALUE.
# Any two/three distinct registry markets exercise this identically;
# the specific markets used here are arbitrary test data, not special
# cases the functions know about.
# ---------------------------------------------------------------------

def test_resolve_display_market_key_returns_scalar_for_single_market():
    d = StrategyDefinition(
        market_key="CORRA", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )
    assert resolve_display_market_key(d) == "CORRA"


def test_resolve_display_offsets_returns_scalar_for_single_market():
    d = StrategyDefinition(
        market_key="CORRA", offsets=(0, 2, 4), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    assert resolve_display_offsets(d) == (0, 2, 4)


def test_resolve_display_market_key_joins_leg_markets_in_order_for_intermarket():
    d = IntermarketDefinition(
        legs=(LegSpec("FED_FUNDS", 0, 1.0), LegSpec("SOFR", 1, -1.0), LegSpec("CORRA", 0, 2.0)),
        interval=BarInterval.DAILY,
    )
    assert resolve_display_market_key(d) == "FED_FUNDS/SOFR/CORRA"


def test_resolve_display_offsets_returns_per_leg_offsets_in_order_for_intermarket():
    d = IntermarketDefinition(
        legs=(LegSpec("FED_FUNDS", 0, 1.0), LegSpec("SOFR", 1, -1.0), LegSpec("CORRA", 0, 2.0)),
        interval=BarInterval.DAILY,
    )
    assert resolve_display_offsets(d) == (0, 1, 0)


def test_resolve_display_functions_never_depend_on_which_markets_are_present():
    """The resolver's OUTPUT shape/behavior is identical regardless of
    WHICH two markets are combined -- swapping in a different market
    pair changes only the label content, never the code path taken."""
    pair_one = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)), interval=BarInterval.DAILY,
    )
    pair_two = IntermarketDefinition(
        legs=(LegSpec("EURIBOR", 0, 1.0), LegSpec("SARON", 0, -1.0)), interval=BarInterval.DAILY,
    )
    assert resolve_display_market_key(pair_one) == "SOFR/SONIA"
    assert resolve_display_market_key(pair_two) == "EURIBOR/SARON"
    assert resolve_display_offsets(pair_one) == resolve_display_offsets(pair_two) == (0, 0)
