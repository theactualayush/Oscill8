"""
tests/test_strategy_sets_composite_market_pairing.py

Focused coverage of the MARKET-PAIR-FIRST pairing rule in
strategy_sets/composite.py -- the top level of a composite ("Group A x
Group B") Strategy Set's product.

The rule, in one line: pair the two groups' unique MARKETS first, then
expand the full Cartesian product of the strategies belonging to each
surviving market pair.

    unique markets in Group A  x  unique markets in Group B
                          |
                          v
         for each surviving market pair (mA, mB):
           Group A strategies in mA  x  Group B strategies in mB

Same-market pairs are never formed, reverse market pairs are
deduplicated (unordered identity for uniqueness, directional A -> B for
construction and display), and within a market pair every A strategy
meets every B strategy -- never a positional/zip match.

Real objects throughout: real StrategyDefinition/IntermarketDefinition
shapes and a real StrategySetRepository backed by tmp_path (never
data/strategy_sets/, which is live user data). Nothing here fakes the
strategy model or re-implements the pairing it tests.

Composition, structural-zero filtering, instance generation, provider/
cache behaviour, execution and the UI are all deliberately out of scope
here -- they are unchanged by this rule and keep their own existing
coverage (tests/test_strategy_sets_composite.py, tests/test_strategy_
sets_composite_structural_zero.py, tests/test_composite_execution.py).
"""

from __future__ import annotations

import pytest

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec

from strategy_sets.composite import (
    SourceStrategy,
    cartesian_pairs,
    group_strategies_by_market,
    market_pair_first_pairs,
    market_pair_identity,
    resolve_composite_combinations,
    strategy_markets,
)
from strategy_sets.model import (
    IntermarketStrategySetEntry,
    StrategyGroup,
    StrategyGroupPair,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository


# ---------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------

def _fly(market_key: str, offsets=(0, 1, 2)) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=offsets, weights=(1.0, -2.0, 1.0),
        interval=BarInterval.DAILY,
    )


def _spread(market_key: str) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1), weights=(1.0, -1.0), interval=BarInterval.DAILY,
    )


def _intermarket(*legs) -> IntermarketDefinition:
    return IntermarketDefinition(
        legs=tuple(LegSpec(market_key=m, offset=o, weight=w) for m, o, w in legs),
        interval=BarInterval.DAILY,
    )


def _source(definition, entry_name: str, set_name: str = "Set") -> SourceStrategy:
    return SourceStrategy(set_name=set_name, entry_name=entry_name, definition=definition)


def _pairs(group_a, group_b) -> list[tuple[str, str]]:
    """market_pair_first_pairs() as readable (A name, B name) tuples."""
    return [(a.entry_name, b.entry_name) for a, b in market_pair_first_pairs(group_a, group_b)]


# ---------------------------------------------------------------------
# Market identity of ONE strategy
# ---------------------------------------------------------------------

def test_single_market_strategy_markets_is_its_own_market_key():
    assert strategy_markets(_fly("SOFR")) == ("SOFR",)
    assert strategy_markets(_spread("CORRA")) == ("CORRA",)


def test_intermarket_strategy_markets_are_every_market_its_legs_touch():
    definition = _intermarket(("SOFR", 0, 1.0), ("CORRA", 0, -1.0))
    assert strategy_markets(definition) == ("SOFR", "CORRA")


def test_strategy_markets_deduplicates_and_keeps_first_appearance_order():
    definition = _intermarket(
        ("CORRA", 0, 1.0), ("SOFR", 0, -1.0), ("CORRA", 1, 1.0), ("SOFR", 1, -1.0),
    )
    assert strategy_markets(definition) == ("CORRA", "SOFR")


def test_strategy_markets_never_reduces_a_multi_market_definition_to_one():
    """The established architectural rule for a cross-market definition
    is that no single leg's market ever stands for the whole thing (see
    _resolve_bp_per_point and Module 9's resolve_display_market_key).
    Market identity follows it: every market, never a 'primary' one."""
    definition = _intermarket(("SOFR", 0, 4.0), ("CORRA", 0, -1.0))
    markets = strategy_markets(definition)
    assert len(markets) == 2
    assert set(markets) == {"SOFR", "CORRA"}


# ---------------------------------------------------------------------
# Grouping strategies by market
# ---------------------------------------------------------------------

def test_group_strategies_by_market_preserves_first_appearance_market_order():
    sources = [
        _source(_fly("SOFR"), "SOFR 3M"),
        _source(_fly("SONIA"), "SONIA 3M"),
        _source(_spread("SOFR"), "SOFR 6M"),
        _source(_fly("CORRA"), "CORRA 3M"),
    ]
    buckets = group_strategies_by_market(sources)
    assert list(buckets) == [frozenset({"SOFR"}), frozenset({"SONIA"}), frozenset({"CORRA"})]
    assert [s.entry_name for s in buckets[frozenset({"SOFR"})]] == ["SOFR 3M", "SOFR 6M"]


def test_group_strategies_by_market_is_never_alphabetical():
    sources = [_source(_fly("SONIA"), "B"), _source(_fly("CORRA"), "A")]
    assert list(group_strategies_by_market(sources)) == [
        frozenset({"SONIA"}), frozenset({"CORRA"}),
    ]


def test_intermarket_leg_order_does_not_split_one_market_group():
    """Two intermarket strategies over the same two markets belong to
    ONE market group however their legs happen to be ordered."""
    sofr_first = _source(_intermarket(("SOFR", 0, 1.0), ("CORRA", 0, -1.0)), "A1")
    corra_first = _source(_intermarket(("CORRA", 0, 1.0), ("SOFR", 0, -1.0)), "A2")
    buckets = group_strategies_by_market([sofr_first, corra_first])
    assert list(buckets) == [frozenset({"SOFR", "CORRA"})]
    assert [s.entry_name for s in buckets[frozenset({"SOFR", "CORRA"})]] == ["A1", "A2"]


# ---------------------------------------------------------------------
# (A) Basic market grouping
# ---------------------------------------------------------------------

def test_basic_market_grouping_expands_every_strategy_of_each_market():
    group_a = [
        _source(_fly("SOFR"), "SOFR-A1"),
        _source(_spread("SOFR"), "SOFR-A2"),
        _source(_fly("SONIA"), "SONIA-A3"),
    ]
    group_b = [
        _source(_fly("CORRA"), "CORRA-B1"),
        _source(_spread("CORRA"), "CORRA-B2"),
    ]
    assert _pairs(group_a, group_b) == [
        ("SOFR-A1", "CORRA-B1"),
        ("SOFR-A1", "CORRA-B2"),
        ("SOFR-A2", "CORRA-B1"),
        ("SOFR-A2", "CORRA-B2"),
        ("SONIA-A3", "CORRA-B1"),
        ("SONIA-A3", "CORRA-B2"),
    ]


# ---------------------------------------------------------------------
# (B) Multiple markets on both sides -- the four-market worked example
# ---------------------------------------------------------------------

@pytest.fixture
def four_market_groups():
    """The design brief's four-market example.

        Group A:  SOFR [A1, A2], SONIA [A3], CORRA [A4]
        Group B:  CORRA [B1, B2], EURIBOR [B3]
    """
    group_a = [
        _source(_fly("SOFR"), "A1"),
        _source(_spread("SOFR"), "A2"),
        _source(_fly("SONIA"), "A3"),
        _source(_fly("CORRA"), "A4"),
    ]
    group_b = [
        _source(_fly("CORRA"), "B1"),
        _source(_spread("CORRA"), "B2"),
        _source(_fly("EURIBOR"), "B3"),
    ]
    return group_a, group_b


def test_four_market_example_produces_exactly_the_expected_pairs(four_market_groups):
    group_a, group_b = four_market_groups
    assert _pairs(group_a, group_b) == [
        # SOFR -> CORRA
        ("A1", "B1"), ("A1", "B2"), ("A2", "B1"), ("A2", "B2"),
        # SOFR -> EURIBOR
        ("A1", "B3"), ("A2", "B3"),
        # SONIA -> CORRA
        ("A3", "B1"), ("A3", "B2"),
        # SONIA -> EURIBOR
        ("A3", "B3"),
        # CORRA -> CORRA is same-market and never formed
        # CORRA -> EURIBOR
        ("A4", "B3"),
    ]


def test_four_market_example_produces_exactly_ten_structures(four_market_groups):
    group_a, group_b = four_market_groups
    assert len(market_pair_first_pairs(*four_market_groups)) == 10


def test_four_market_example_forms_only_the_five_intended_relationships(four_market_groups):
    group_a, group_b = four_market_groups
    relationships = [
        (strategy_markets(a.definition), strategy_markets(b.definition))
        for a, b in market_pair_first_pairs(group_a, group_b)
    ]
    assert list(dict.fromkeys(relationships)) == [
        (("SOFR",), ("CORRA",)),
        (("SOFR",), ("EURIBOR",)),
        (("SONIA",), ("CORRA",)),
        (("SONIA",), ("EURIBOR",)),
        (("CORRA",), ("EURIBOR",)),
    ]


@pytest.mark.parametrize(
    "forbidden",
    [
        (("CORRA",), ("SOFR",)),
        (("EURIBOR",), ("SOFR",)),
        (("CORRA",), ("SONIA",)),
        (("EURIBOR",), ("SONIA",)),
        (("EURIBOR",), ("CORRA",)),
        (("CORRA",), ("CORRA",)),
    ],
)
def test_four_market_example_never_produces_a_reverse_or_same_market_pair(
    four_market_groups, forbidden
):
    group_a, group_b = four_market_groups
    relationships = {
        (strategy_markets(a.definition), strategy_markets(b.definition))
        for a, b in market_pair_first_pairs(group_a, group_b)
    }
    assert forbidden not in relationships


# ---------------------------------------------------------------------
# (C) Same-market exclusion
# ---------------------------------------------------------------------

def test_same_market_strategies_are_never_paired():
    group_a = [_source(_fly("SOFR"), "SOFR-A1"), _source(_spread("SOFR"), "SOFR-A2")]
    group_b = [_source(_fly("SOFR"), "SOFR-B1"), _source(_fly("CORRA"), "CORRA-B1")]
    assert _pairs(group_a, group_b) == [
        ("SOFR-A1", "CORRA-B1"),
        ("SOFR-A2", "CORRA-B1"),
    ]


def test_a_wholly_same_market_configuration_produces_nothing():
    group_a = [_source(_fly("SOFR"), "A1"), _source(_spread("SOFR"), "A2")]
    group_b = [_source(_fly("SOFR", offsets=(0, 2, 4)), "B1")]
    assert market_pair_first_pairs(group_a, group_b) == []


def test_same_market_exclusion_is_by_market_not_by_shape():
    """Two genuinely different SOFR strategies are still same-market --
    the exclusion is about the market relationship, not about whether
    the composed definition would cancel."""
    group_a = [_source(_fly("SOFR"), "Fly")]
    group_b = [_source(_spread("SOFR"), "Spread")]
    assert market_pair_first_pairs(group_a, group_b) == []


# ---------------------------------------------------------------------
# (D) Reverse market-pair deduplication
# ---------------------------------------------------------------------

def test_reverse_market_pair_is_deduplicated():
    group_a = [_source(_fly("SOFR"), "SOFR-A"), _source(_fly("CORRA"), "CORRA-A")]
    group_b = [_source(_spread("CORRA"), "CORRA-B"), _source(_spread("SOFR"), "SOFR-B")]
    # SOFR -> CORRA is reached first; CORRA -> SOFR is its reverse and
    # is dropped, and both same-market pairs are never formed.
    assert _pairs(group_a, group_b) == [("SOFR-A", "CORRA-B")]


def test_surviving_market_orientation_follows_the_group_configuration():
    a_first = [_source(_fly("SOFR"), "SOFR-A")]
    b_first = [_source(_fly("CORRA"), "CORRA-B")]
    assert _pairs(a_first, b_first) == [("SOFR-A", "CORRA-B")]
    # Swap which side each market is configured on: the orientation
    # follows the trader's own Group A / Group B, never alphabetical
    # order.
    assert _pairs(b_first, a_first) == [("CORRA-B", "SOFR-A")]


def test_market_pair_identity_is_order_insensitive():
    sofr, corra = frozenset({"SOFR"}), frozenset({"CORRA"})
    assert market_pair_identity(sofr, corra) == market_pair_identity(corra, sofr)


def test_market_pair_identity_distinguishes_genuinely_different_relationships():
    sofr, corra, sonia = frozenset({"SOFR"}), frozenset({"CORRA"}), frozenset({"SONIA"})
    assert market_pair_identity(sofr, corra) != market_pair_identity(sofr, sonia)


# ---------------------------------------------------------------------
# (E) Selection order
# ---------------------------------------------------------------------

def test_market_discovery_follows_selection_order_not_the_alphabet():
    group_a = [
        _source(_fly("SOFR"), "SOFR 3M"),
        _source(_fly("SONIA"), "SONIA 3M"),
        _source(_spread("SOFR"), "SOFR 6M"),
        _source(_fly("CORRA"), "CORRA 3M"),
    ]
    group_b = [_source(_fly("EURIBOR"), "EUR 3M")]
    assert _pairs(group_a, group_b) == [
        # Markets in first-appearance order: SOFR, SONIA, CORRA ...
        ("SOFR 3M", "EUR 3M"),
        ("SOFR 6M", "EUR 3M"),   # ... and SOFR's own strategies in order
        ("SONIA 3M", "EUR 3M"),
        ("CORRA 3M", "EUR 3M"),
    ]


def test_group_b_market_order_drives_the_inner_loop():
    group_a = [_source(_fly("SOFR"), "A1")]
    group_b = [_source(_fly("EURIBOR"), "EUR"), _source(_fly("CORRA"), "CRA")]
    assert _pairs(group_a, group_b) == [("A1", "EUR"), ("A1", "CRA")]
    # Reversing Group B's selection order reverses the emitted order --
    # nothing is sorted.
    assert _pairs(group_a, list(reversed(group_b))) == [("A1", "CRA"), ("A1", "EUR")]


def test_pairing_is_deterministic_across_repeated_calls(four_market_groups):
    group_a, group_b = four_market_groups
    assert _pairs(group_a, group_b) == _pairs(group_a, group_b)


# ---------------------------------------------------------------------
# (F) Cartesian product WITHIN a market pair -- never a zip match
# ---------------------------------------------------------------------

def test_three_by_two_within_one_market_pair_is_six_combinations():
    group_a = [
        _source(_fly("SOFR", offsets=(0, 1, 2)), "SOFR 3M"),
        _source(_fly("SOFR", offsets=(0, 2, 4)), "SOFR 6M"),
        _source(_fly("SOFR", offsets=(0, 3, 6)), "SOFR 9M"),
    ]
    group_b = [
        _source(_fly("CORRA", offsets=(0, 1, 2)), "CORRA 3M"),
        _source(_fly("CORRA", offsets=(0, 2, 4)), "CORRA 6M"),
    ]
    pairs = _pairs(group_a, group_b)
    assert len(pairs) == 6
    assert pairs == [
        ("SOFR 3M", "CORRA 3M"), ("SOFR 3M", "CORRA 6M"),
        ("SOFR 6M", "CORRA 3M"), ("SOFR 6M", "CORRA 6M"),
        ("SOFR 9M", "CORRA 3M"), ("SOFR 9M", "CORRA 6M"),
    ]


def test_within_market_pairing_is_not_a_positional_zip_match():
    group_a = [
        _source(_fly("SOFR", offsets=(0, 1, 2)), "SOFR 3M"),
        _source(_fly("SOFR", offsets=(0, 2, 4)), "SOFR 6M"),
    ]
    group_b = [
        _source(_fly("CORRA", offsets=(0, 1, 2)), "CORRA 3M"),
        _source(_fly("CORRA", offsets=(0, 2, 4)), "CORRA 6M"),
    ]
    pairs = _pairs(group_a, group_b)
    # A zip match would give exactly the two diagonal pairs.
    assert len(pairs) == 4
    assert ("SOFR 3M", "CORRA 6M") in pairs
    assert ("SOFR 6M", "CORRA 3M") in pairs


# ---------------------------------------------------------------------
# Multi-market (Module 9) source strategies
# ---------------------------------------------------------------------

def test_an_intermarket_source_forms_its_own_market_group():
    basis = _source(_intermarket(("SOFR", 0, 1.0), ("CORRA", 0, -1.0)), "SOFR vs CORRA")
    group_b = [_source(_fly("SONIA"), "SON Fly")]
    assert _pairs([basis], group_b) == [("SOFR vs CORRA", "SON Fly")]


def test_an_intermarket_source_is_not_paired_with_an_identical_market_identity():
    a = _source(_intermarket(("SOFR", 0, 1.0), ("CORRA", 0, -1.0)), "A basis")
    b = _source(_intermarket(("CORRA", 0, 1.0), ("SOFR", 0, -2.0)), "B basis")
    # Same two markets on both sides, whatever the leg order or weights.
    assert market_pair_first_pairs([a], [b]) == []


def test_an_intermarket_source_pairs_with_a_partially_overlapping_identity():
    """{SOFR, CORRA} and {CORRA} are DIFFERENT market identities, so
    they are still a distinct relationship and are still paired. Only an
    identical market identity is excluded."""
    a = _source(_intermarket(("SOFR", 0, 1.0), ("CORRA", 0, -1.0)), "SOFR vs CORRA")
    b = _source(_fly("CORRA"), "CRA Fly")
    assert _pairs([a], [b]) == [("SOFR vs CORRA", "CRA Fly")]


def test_an_intermarket_source_over_one_market_behaves_as_that_market():
    """A hand-authored intermarket definition whose legs all belong to
    one market has that market's identity -- so it is excluded against
    an ordinary strategy on the same market, exactly as two ordinary
    same-market strategies are."""
    a = _source(_intermarket(("SOFR", 0, 1.0), ("SOFR", 1, -1.0)), "SOFR pair")
    assert market_pair_first_pairs([a], [_source(_fly("SOFR"), "SR3 Fly")]) == []
    assert _pairs([a], [_source(_fly("CORRA"), "CRA Fly")]) == [("SOFR pair", "CRA Fly")]


# ---------------------------------------------------------------------
# The lower-level helper is unchanged
# ---------------------------------------------------------------------

def test_cartesian_pairs_still_computes_the_plain_strategy_product():
    a = [_source(_fly("SOFR"), "A1"), _source(_fly("SONIA"), "A2")]
    b = [_source(_fly("CORRA"), "B1")]
    assert [(x.entry_name, y.entry_name) for x, y in cartesian_pairs(a, b)] == [
        ("A1", "B1"), ("A2", "B1"),
    ]


def test_market_pair_first_pairs_is_a_subset_of_the_plain_product(four_market_groups):
    group_a, group_b = four_market_groups
    plain = {(a.entry_name, b.entry_name) for a, b in cartesian_pairs(group_a, group_b)}
    narrowed = set(_pairs(group_a, group_b))
    assert narrowed < plain


def test_empty_groups_produce_no_pairs():
    assert market_pair_first_pairs([], [_source(_fly("SOFR"), "B1")]) == []
    assert market_pair_first_pairs([_source(_fly("SOFR"), "A1")], []) == []


# ---------------------------------------------------------------------
# End to end through resolve_composite_combinations()
# ---------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path) -> StrategySetRepository:
    repository = StrategySetRepository(base_dir=str(tmp_path / "strategy_sets"))
    repository.save(
        StrategySet(
            name="Group A Source",
            entries=(
                StrategySetEntry(name="SOFR 3M Fly", definition=_fly("SOFR", (0, 1, 2))),
                StrategySetEntry(name="SOFR 6M Fly", definition=_fly("SOFR", (0, 2, 4))),
                StrategySetEntry(name="SOFR 9M Fly", definition=_fly("SOFR", (0, 3, 6))),
                StrategySetEntry(name="SONIA 3M Fly", definition=_fly("SONIA", (0, 1, 2))),
                StrategySetEntry(name="SONIA 6M Fly", definition=_fly("SONIA", (0, 2, 4))),
            ),
        )
    )
    repository.save(
        StrategySet(
            name="Group B Source",
            entries=(
                StrategySetEntry(name="CORRA 3M Fly", definition=_fly("CORRA", (0, 1, 2))),
                StrategySetEntry(name="CORRA 6M Fly", definition=_fly("CORRA", (0, 2, 4))),
            ),
        )
    )
    repository.save(
        StrategySet(
            name="Basis Source",
            entries=(),
            intermarket_entries=(
                IntermarketStrategySetEntry(
                    name="SOFR vs CORRA",
                    definition=_intermarket(("SOFR", 0, 1.0), ("CORRA", 0, -1.0)),
                ),
            ),
        )
    )
    return repository


def _composite(a_selected, b_selected, a_source="Group A Source", b_source="Group B Source"):
    return StrategySet(
        name="Combos",
        entries=(),
        groups=StrategyGroupPair(
            group_a=StrategyGroup(source_set_name=a_source, selected_entry_names=a_selected),
            group_b=StrategyGroup(source_set_name=b_source, selected_entry_names=b_selected),
        ),
    )


def _names(combinations) -> list[str]:
    return [c.name for c in combinations]


def test_the_design_brief_example_resolves_to_its_ten_structures(repo):
    """Group A: SOFR [3M, 6M, 9M] + SONIA [3M, 6M]; Group B: CORRA
    [3M, 6M]. SOFR -> CORRA is 3 x 2 = 6 and SONIA -> CORRA is 2 x 2 =
    4, for 10 in total."""
    composite = _composite(
        ("SOFR 3M Fly", "SOFR 6M Fly", "SOFR 9M Fly", "SONIA 3M Fly", "SONIA 6M Fly"),
        ("CORRA 3M Fly", "CORRA 6M Fly"),
    )
    names = _names(resolve_composite_combinations(composite, repo))
    assert names == [
        "SOFR 3M Fly - CORRA 3M Fly",
        "SOFR 3M Fly - CORRA 6M Fly",
        "SOFR 6M Fly - CORRA 3M Fly",
        "SOFR 6M Fly - CORRA 6M Fly",
        "SOFR 9M Fly - CORRA 3M Fly",
        "SOFR 9M Fly - CORRA 6M Fly",
        "SONIA 3M Fly - CORRA 3M Fly",
        "SONIA 3M Fly - CORRA 6M Fly",
        "SONIA 6M Fly - CORRA 3M Fly",
        "SONIA 6M Fly - CORRA 6M Fly",
    ]
    assert len(names) == 10


def test_resolution_never_produces_a_same_market_combination(repo):
    composite = _composite(
        ("SOFR 3M Fly", "SOFR 6M Fly"),
        ("CORRA 3M Fly",),
    )
    for combination in resolve_composite_combinations(composite, repo):
        a_markets = set(strategy_markets(combination.group_a.definition))
        b_markets = set(strategy_markets(combination.group_b.definition))
        assert a_markets != b_markets


def test_resolution_keeps_an_intermarket_group_member_pairing(repo):
    composite = _composite(
        ("SOFR vs CORRA",), ("CORRA 3M Fly",), a_source="Basis Source",
    )
    combinations = resolve_composite_combinations(composite, repo)
    assert _names(combinations) == ["SOFR vs CORRA - CORRA 3M Fly"]
    assert combinations[0].definition.market_keys == (
        "SOFR", "CORRA", "CORRA", "CORRA", "CORRA",
    )


def test_resolution_market_order_follows_the_saved_selection_order(repo):
    composite = _composite(
        ("SONIA 3M Fly", "SOFR 3M Fly"), ("CORRA 3M Fly",),
    )
    assert _names(resolve_composite_combinations(composite, repo)) == [
        "SONIA 3M Fly - CORRA 3M Fly",
        "SOFR 3M Fly - CORRA 3M Fly",
    ]
