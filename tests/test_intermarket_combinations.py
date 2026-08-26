"""
tests/test_intermarket_combinations.py

generate_intermarket_instances tested against real core.futures_calendar/
core.ric logic -- pure calendar/combinatorics, no mocking needed. Mirrors
tests/test_strategy_combinations.py's style.
"""

from __future__ import annotations

import core.ric as ric_module
from core.config import BarInterval
from strategy_engine.intermarket_combinations import (
    IntermarketStrategyInstance,
    generate_intermarket_instances,
)
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec


# ---------------------------------------------------------------------
# B. Independent market generation
# ---------------------------------------------------------------------

def test_sofr_sonia_basis_generates_independently_per_market():
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-12-31")

    # SOFR is 2-digit year (SRAZ26), SONIA is 1-digit year (SONZ6) --
    # each leg's own RIC convention is untouched by the other's.
    assert [inst.rics for inst in instances] == [
        ("SRAH26", "SONH6"),
        ("SRAM26", "SONM6"),
        ("SRAU26", "SONU6"),
        ("SRAZ26", "SONZ6"),
    ]
    for inst in instances:
        assert inst.definition is definition


def test_sofr_corra_outright_basis():
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("CORRA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-06-30")
    assert [inst.rics for inst in instances] == [
        ("SRAH26", "CRAH6"),
        ("SRAM26", "CRAM6"),
    ]


# ---------------------------------------------------------------------
# C. Calendar-month alignment (never "nth contract on each curve")
# ---------------------------------------------------------------------

def test_sofr_dec_2026_pairs_with_sonia_dec_2026_not_by_curve_position():
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-12-31")

    dec_instance = instances[-1]
    sofr_ric, sonia_ric = dec_instance.rics
    assert sofr_ric == "SRAZ26"
    assert sonia_ric == "SONZ6"
    # Both legs' RICs decode to the SAME calendar month/year -- the
    # actual definition of "aligned", independent of which index each
    # RIC happens to occupy in its own market's own contract list.
    parsed_sofr = ric_module.parse_ric(sofr_ric)
    parsed_sonia = ric_module.parse_ric(sonia_ric)
    assert (parsed_sofr.month, parsed_sofr.year) == (parsed_sonia.month, parsed_sonia.year)
    assert (parsed_sofr.month, parsed_sofr.year) == (12, 2026)


# ---------------------------------------------------------------------
# D. Different listing cycles
# ---------------------------------------------------------------------

def test_quarterly_and_monthly_market_align_on_quarterly_months_only():
    """Both legs at offset=0 -- i.e. both are ANCHOR legs, so their
    calendars must genuinely intersect (this is the one case where
    "alignment across legs" is correct and required): SOFR (quarterly) +
    FED_FUNDS (monthly) can only anchor on months SOFR actually lists,
    even though FED_FUNDS lists a contract every month. A naive "nth
    contract from each curve" pairing would incorrectly pair SOFR's 1st
    contract (March) with FED_FUNDS's 1st contract (January) -- anchor
    alignment must not do that.

    Contrast with test_non_anchor_leg_steps_its_own_curve_not_a_shared_one
    below, where FED_FUNDS is a NON-anchor (offset > 0) leg instead --
    there, its own January/February/April/May contracts are NOT
    discarded, because only anchor legs' calendars are intersected.
    """
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("FED_FUNDS", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-06-30")

    assert [inst.rics for inst in instances] == [
        ("SRAH26", "FFH26"),  # both March 2026
        ("SRAM26", "FFM26"),  # both June 2026
    ]
    # FED_FUNDS's January/February/April/May contracts (FFF26, FFG26,
    # FFJ26, FFK26) exist on FED_FUNDS's own curve but are correctly
    # never used -- SOFR has no contract in those months.
    all_fed_funds_rics = {ric for inst in instances for ric in inst.rics if ric.startswith("FF")}
    assert all_fed_funds_rics == {"FFH26", "FFM26"}


def test_too_few_aligned_months_returns_empty_list_not_error():
    definition = IntermarketDefinition(
        legs=(
            LegSpec("SOFR", 0, 1.0),
            LegSpec("SOFR", 1, -2.0),
            LegSpec("SOFR", 2, 1.0),
        ),
        interval=BarInterval.DAILY,
    )
    # Only 2 quarterly months fall in this window -- fewer than the
    # 3 aligned positions this shape needs (span=2).
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-06-30")
    assert instances == []


# ---------------------------------------------------------------------
# E. RIC generation / market identity
# ---------------------------------------------------------------------

def test_every_leg_ric_parses_back_to_its_own_declared_market():
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0), LegSpec("CORRA", 0, 1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-12-31")
    assert instances

    for inst in instances:
        for ric, leg in zip(inst.rics, definition.legs):
            parsed = ric_module.parse_ric(ric)
            assert parsed.market_key == leg.market_key


# ---------------------------------------------------------------------
# F. Offsets applied independently per market
# ---------------------------------------------------------------------

def test_offsets_apply_independently_to_the_shared_aligned_sequence():
    """SOFR one quarter ahead of SONIA -- a calendar spread ACROSS
    markets. SOFR's offset=1 steps forward on SOFR's OWN curve from the
    SONIA-anchored period; it only LOOKS like "the shared sequence" here
    because SOFR and SONIA happen to share the same quarterly listing
    cycle. See test_non_anchor_leg_steps_its_own_curve_not_a_shared_one
    below for the case where that coincidence doesn't hold and the
    distinction becomes visible."""
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 1, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-12-31")

    assert [inst.rics for inst in instances] == [
        ("SRAM26", "SONH6"),
        ("SRAU26", "SONM6"),
        ("SRAZ26", "SONU6"),
    ]
    for inst in instances:
        sofr_parsed = ric_module.parse_ric(inst.rics[0])
        sonia_parsed = ric_module.parse_ric(inst.rics[1])
        # SOFR's month is always exactly one quarter (3 calendar months)
        # ahead of SONIA's within the same instance.
        assert sofr_parsed.year * 12 + sofr_parsed.month == sonia_parsed.year * 12 + sonia_parsed.month + 3


def test_duplicate_offsets_across_legs_are_valid_and_common_case():
    """Both legs at offset=0 is the ordinary intermarket-spread shape
    (e.g. SOFR vs SONIA, same period) -- not a degenerate/rejected case,
    unlike StrategyDefinition's own strictly-increasing-offsets rule."""
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-03-31")
    assert instances == [
        IntermarketStrategyInstance(definition=definition, rics=("SRAH26", "SONH6")),
    ]


# ---------------------------------------------------------------------
# Offset-semantics regression: a non-anchor leg's offset steps forward
# on THAT LEG'S OWN curve, never on a curve shared/intersected with its
# sibling legs. An earlier draft of generate_intermarket_instances()
# intersected ALL legs' calendars (including non-anchor legs) before
# applying any offset -- which silently discarded a finer-grained
# leg's own real contracts whenever it shared a definition with a
# coarser-cycle market. These tests lock in the corrected behavior.
# ---------------------------------------------------------------------

def test_non_anchor_leg_steps_its_own_curve_not_a_shared_one():
    """SOFR (quarterly, anchor) + FED_FUNDS (monthly, offset=1). At the
    March-2026 anchor, FED_FUNDS must resolve to its OWN next listed
    contract -- April 2026 -- NOT June 2026 (the next month common to
    both SOFR's and FED_FUNDS's calendars, which is what an
    all-legs-intersected implementation would incorrectly produce).
    """
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("FED_FUNDS", 1, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-06-30")

    assert [inst.rics for inst in instances] == [("SRAH26", "FFJ26")]  # FFJ26 = April 2026
    _, fed_funds_ric = instances[0].rics
    parsed_fed_funds = ric_module.parse_ric(fed_funds_ric)
    assert (parsed_fed_funds.month, parsed_fed_funds.year) == (4, 2026)  # April, NOT June (6)

    # June-2026 (the anchor axis's only other valid month) can't produce
    # a second instance either: FED_FUNDS has no contract past June in
    # this window to step forward to, so that anchor period is correctly
    # dropped rather than raising or fabricating a value.
    assert len(instances) == 1


def test_non_anchor_offset_meaning_is_independent_of_which_sibling_supplies_the_anchor():
    """The SAME FED_FUNDS(offset=1) leg must resolve to the SAME RIC
    (April 2026) regardless of whether its sibling anchor leg is SOFR or
    SONIA -- both anchor legs share the same quarterly cycle and the
    same March-2026 anchor month, so FED_FUNDS's own curve position is
    identical either way. This is the direct test of the invariant that
    a non-anchor leg's offset means the same thing no matter which other
    markets happen to share its IntermarketDefinition.
    """
    window = ("2026-01-01", "2026-06-30")

    sofr_anchor = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("FED_FUNDS", 1, -1.0)),
        interval=BarInterval.DAILY,
    )
    sonia_anchor = IntermarketDefinition(
        legs=(LegSpec("SONIA", 0, 1.0), LegSpec("FED_FUNDS", 1, -1.0)),
        interval=BarInterval.DAILY,
    )

    sofr_instances = generate_intermarket_instances(sofr_anchor, *window)
    sonia_instances = generate_intermarket_instances(sonia_anchor, *window)

    assert sofr_instances[0].rics[1] == "FFJ26"
    assert sonia_instances[0].rics[1] == "FFJ26"
    assert sofr_instances[0].rics[1] == sonia_instances[0].rics[1]


def test_anchor_axis_sorts_chronologically_across_a_year_boundary():
    """Regression for a latent ordering bug: sorting raw (month, year)
    tuples sorts by month first, which is WRONG across a year boundary
    (e.g. December would sort after a later January/March). Internally
    this module must compare (year, month), never (month, year), so a
    window spanning a year boundary still produces instances in genuine
    chronological order.
    """
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-10-01", "2027-06-30")

    assert [inst.rics for inst in instances] == [
        ("SRAZ26", "SONZ6"),  # Dec 2026
        ("SRAH27", "SONH7"),  # Mar 2027
        ("SRAM27", "SONM7"),  # Jun 2027
    ]
