"""
tests/test_strategy_combinations.py

generate_instances tested against real core.futures_calendar logic --
pure calendar/combinatorics, no mocking needed.
"""

from __future__ import annotations

from core.config import BarInterval
from strategy_engine.combinations import generate_instances
from strategy_engine.definitions import StrategyDefinition


def test_generate_instances_for_outright():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2026-12-31")

    # A span of 0 means every listed contract is its own outright instance.
    assert [inst.rics for inst in instances] == [
        ("SRAH26",),
        ("SRAM26",),
        ("SRAU26",),
        ("SRAZ26",),
    ]


def test_generate_instances_for_sofr_fly():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2027-12-31")

    assert [inst.rics for inst in instances] == [
        ("SRAH26", "SRAM26", "SRAU26"),
        ("SRAM26", "SRAU26", "SRAZ26"),
        ("SRAU26", "SRAZ26", "SRAH27"),
        ("SRAZ26", "SRAH27", "SRAM27"),
        ("SRAH27", "SRAM27", "SRAU27"),
        ("SRAM27", "SRAU27", "SRAZ27"),
    ]
    for inst in instances:
        assert inst.definition is definition


def test_generate_instances_for_sofr_spread():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2026-12-31")
    assert [inst.rics for inst in instances] == [
        ("SRAH26", "SRAM26"),
        ("SRAM26", "SRAU26"),
        ("SRAU26", "SRAZ26"),
    ]


def test_generate_instances_for_condor():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2, 3), weights=(1, -1, -1, 1), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2026-12-31")
    assert [inst.rics for inst in instances] == [
        ("SRAH26", "SRAM26", "SRAU26", "SRAZ26"),
    ]


def test_generate_instances_for_monthly_market_with_gapped_offsets():
    definition = StrategyDefinition(
        market_key="FED_FUNDS", offsets=(0, 3), weights=(1, -1), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2026-06-30")
    assert [inst.rics for inst in instances] == [
        ("FFF26", "FFJ26"),
        ("FFG26", "FFK26"),
        ("FFH26", "FFM26"),
    ]


def test_custom_weights_pass_through_as_metadata():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(2, -5, 3), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2026-12-31")
    assert instances
    assert instances[0].definition.weights == (2, -5, 3)


def test_too_few_contracts_returns_empty_list_not_error():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2, 3), weights=(1, -1, -1, 1), interval=BarInterval.DAILY,
    )
    # Only 2 quarterly contracts fall in this window -- fewer than the
    # 4 legs a condor needs.
    instances = generate_instances(definition, "2026-01-01", "2026-06-30")
    assert instances == []


# ---------------------------------------------------------------------
# Market-specific RIC conventions flow through unchanged, with no
# scanner/strategy_engine-specific special casing -- generate_instances
# only ever delegates to core.futures_calendar, so a market's own
# ric_root/ric_year_digits configuration is all that's needed.
# ---------------------------------------------------------------------

def test_generate_instances_for_corra_outright_one_digit_year_rics():
    definition = StrategyDefinition(
        market_key="CORRA", offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2026-12-31")
    assert [inst.rics for inst in instances] == [
        ("CRAH6",),
        ("CRAM6",),
        ("CRAU6",),
        ("CRAZ6",),
    ]


def test_generate_instances_for_sonia_spread_uses_corrected_root():
    definition = StrategyDefinition(
        market_key="SONIA", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )
    instances = generate_instances(definition, "2026-01-01", "2027-12-31")
    assert instances[0].rics == ("SONH6", "SONM6")
    assert all(r.startswith("SON") for inst in instances for r in inst.rics)
