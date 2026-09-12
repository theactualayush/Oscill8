"""
tests/test_strategy_sets_composite_structural_zero.py

Phase 3: structural-zero filtering of composite ("Group A x Group B")
combinations -- strategy_sets.composite.aggregate_leg_weights() /
is_structurally_zero(), and their application inside
resolve_composite_combinations().

A composed definition is structurally zero when aggregating leg weights
by (market_key, offset) leaves every group at zero: the series would be
identically 0.0 for every rolled contract and every date, provably from
the definition alone. Such a combination is dropped BEFORE instance
generation, cache prewarming, history construction, analytics, or
scanning.

This is emphatically NOT `sum(weights) == 0` -- an ordinary fly's
weights sum to zero while being a perfectly meaningful strategy. The
fly sanity check below is the load-bearing regression against that
mistake.

It is also NOT the same thing as a HISTORICALLY FLAT series (a real
strategy whose realized prices happen not to move over some window).
That is a property of data, is handled by the existing range_analytics
+ optional trader filters, and is deliberately untouched here -- see
test_historically_flat_series_is_not_a_structural_zero_concern.

Real objects throughout: real StrategyDefinition/IntermarketDefinition
shapes, a real tmp_path-backed StrategySetRepository, and real contract
RIC generation via core.futures_calendar. Nothing fakes the strategy
model.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec
from strategy_engine.pricing import StrategyHistory

from range_analytics.results import analyze_range

import strategy_sets.composite as composite_module
from strategy_sets.composite import (
    SourceStrategy,
    aggregate_leg_weights,
    compose_definition,
    is_structurally_zero,
    resolve_composite_combinations,
)
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import (
    IntermarketStrategySetEntry,
    StrategyGroup,
    StrategyGroupPair,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository

_START, _END = "2026-01-01", "2027-12-31"


# ---------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------

def _fly(market_key: str, weights=(1.0, -2.0, 1.0), offsets=(0, 1, 2)) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=offsets, weights=weights, interval=BarInterval.DAILY,
    )


def _spread(market_key: str) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1), weights=(1.0, -1.0), interval=BarInterval.DAILY,
    )


def _intermarket(legs, bp_per_point=None) -> IntermarketDefinition:
    return IntermarketDefinition(
        legs=tuple(LegSpec(market_key=m, offset=o, weight=w) for m, o, w in legs),
        interval=BarInterval.DAILY,
        bp_per_point=bp_per_point,
    )


def _source(definition, set_name="Set", entry_name="Strategy") -> SourceStrategy:
    return SourceStrategy(set_name=set_name, entry_name=entry_name, definition=definition)


def _composed(a_definition, b_definition) -> IntermarketDefinition:
    return compose_definition(_source(a_definition, entry_name="A"), _source(b_definition, entry_name="B"))


# ---------------------------------------------------------------------
# aggregate_leg_weights -- the grouping key
# ---------------------------------------------------------------------

def test_aggregate_groups_by_market_key_and_offset():
    assert aggregate_leg_weights(_fly("SOFR")) == {
        ("SOFR", 0): 1.0, ("SOFR", 1): -2.0, ("SOFR", 2): 1.0,
    }


def test_aggregate_sums_repeated_market_offset_legs():
    definition = _intermarket(
        (("SOFR", 0, 1.0), ("SOFR", 0, 2.5), ("CORRA", 0, -1.0)),
    )
    assert aggregate_leg_weights(definition) == {("SOFR", 0): 3.5, ("CORRA", 0): -1.0}


def test_aggregate_keeps_same_offset_on_different_markets_separate():
    definition = _intermarket((("SOFR", 0, 1.0), ("CORRA", 0, -1.0)))
    assert aggregate_leg_weights(definition) == {("SOFR", 0): 1.0, ("CORRA", 0): -1.0}


def test_aggregate_keeps_same_market_at_different_offsets_separate():
    definition = _intermarket((("SOFR", 0, 1.0), ("SOFR", 1, -1.0)))
    assert aggregate_leg_weights(definition) == {("SOFR", 0): 1.0, ("SOFR", 1): -1.0}


# ---------------------------------------------------------------------
# (C) The mandatory sanity check -- sum(weights) == 0 is NOT the test
# ---------------------------------------------------------------------

def test_ordinary_fly_sums_to_zero_but_is_not_structurally_zero():
    fly = _fly("SOFR")
    assert sum(fly.weights) == 0          # the naive, WRONG test would fire here
    assert not is_structurally_zero(fly)  # the correct, per-(market, offset) test does not


@pytest.mark.parametrize(
    "weights",
    [
        (1.0, -2.0, 1.0),        # fly
        (1.0, -1.0),             # spread
        (1.0, -3.0, 3.0, -1.0),  # butterfly-of-butterflies / condor-ish
        (1.0, -1.0, -1.0, 1.0),  # condor
    ],
)
def test_ordinary_zero_summing_shapes_all_survive(weights):
    offsets = tuple(range(len(weights)))
    definition = StrategyDefinition(
        market_key="SOFR", offsets=offsets, weights=weights, interval=BarInterval.DAILY,
    )
    assert sum(definition.weights) == 0
    assert not is_structurally_zero(definition)


# ---------------------------------------------------------------------
# (A) (D) (E) (F) (G) is_structurally_zero on composed definitions
# ---------------------------------------------------------------------

def test_identical_ordinary_strategy_minus_itself_is_structurally_zero():
    assert is_structurally_zero(_composed(_fly("SOFR"), _fly("SOFR")))


def test_cross_market_composite_is_not_structurally_zero():
    composed = _composed(_fly("SOFR"), _fly("CORRA"))
    assert not is_structurally_zero(composed)
    assert aggregate_leg_weights(composed) == {
        ("SOFR", 0): 1.0, ("SOFR", 1): -2.0, ("SOFR", 2): 1.0,
        ("CORRA", 0): -1.0, ("CORRA", 1): 2.0, ("CORRA", 2): -1.0,
    }


def test_partial_cancellation_survives():
    # SOFR fly minus SOFR spread: (SOFR, 0) cancels, (SOFR, 1)/(SOFR, 2) remain.
    composed = _composed(_fly("SOFR"), _spread("SOFR"))
    assert aggregate_leg_weights(composed) == {
        ("SOFR", 0): 0.0, ("SOFR", 1): -1.0, ("SOFR", 2): 1.0,
    }
    assert not is_structurally_zero(composed)


def test_same_market_different_offsets_survives():
    composed = _composed(_fly("SOFR"), _fly("SOFR", offsets=(0, 2, 4)))
    assert not is_structurally_zero(composed)


def test_scaled_structures_survive():
    # (1, -2, 1) minus (2, -4, 2) leaves (-1, 2, -1) -- real exposure.
    composed = _composed(_fly("SOFR"), _fly("SOFR", weights=(2.0, -4.0, 2.0)))
    assert aggregate_leg_weights(composed) == {
        ("SOFR", 0): -1.0, ("SOFR", 1): 2.0, ("SOFR", 2): -1.0,
    }
    assert not is_structurally_zero(composed)


def test_a_scaled_structure_minus_itself_is_structurally_zero():
    scaled = _fly("SOFR", weights=(2.0, -4.0, 2.0))
    assert is_structurally_zero(_composed(scaled, scaled))


# ---------------------------------------------------------------------
# (B) (H) (I) Leg ordering and intermarket sources
# ---------------------------------------------------------------------

def test_intermarket_definition_minus_itself_is_structurally_zero():
    basis = _intermarket((("SOFR", 0, 1.0), ("CORRA", 0, -1.0)))
    assert is_structurally_zero(_composed(basis, basis))


def test_reordered_legs_describing_the_same_shape_still_cancel():
    # Same legs, different LegSpec order -- an order-SENSITIVE comparison
    # (e.g. strategy_identity equality) would miss this; aggregation
    # cannot, because it is commutative.
    forward = _intermarket((("SOFR", 0, 1.0), ("CORRA", 1, -2.0), ("SONIA", 0, 3.0)))
    shuffled = _intermarket((("SONIA", 0, 3.0), ("SOFR", 0, 1.0), ("CORRA", 1, -2.0)))
    assert forward.legs != shuffled.legs          # genuinely a different leg order
    assert is_structurally_zero(_composed(forward, shuffled))


def test_leg_order_never_changes_the_structural_zero_verdict():
    a = _intermarket((("SOFR", 0, 1.0), ("CORRA", 0, -1.0), ("SONIA", 2, 0.5)))
    b = _intermarket((("SONIA", 2, 0.5), ("CORRA", 0, -1.0), ("SOFR", 0, 1.0)))
    assert is_structurally_zero(_composed(a, b)) is True
    assert is_structurally_zero(_composed(b, a)) is True
    # ... and a genuinely different shape stays non-zero in both orders.
    c = _intermarket((("SOFR", 0, 1.0), ("CORRA", 0, -1.0)))
    assert is_structurally_zero(_composed(a, c)) is False
    assert is_structurally_zero(_composed(c, a)) is False


def test_intermarket_minus_single_market_survives_when_exposure_remains():
    basis = _intermarket((("SOFR", 0, 1.0), ("CORRA", 0, -1.0)))
    composed = _composed(basis, _spread("SOFR"))
    assert aggregate_leg_weights(composed) == {
        ("SOFR", 0): 0.0, ("CORRA", 0): -1.0, ("SOFR", 1): 1.0,
    }
    assert not is_structurally_zero(composed)


# ---------------------------------------------------------------------
# (Q) Numerical policy: exact zero, via math.fsum
# ---------------------------------------------------------------------

def test_decimal_weights_cancel_exactly():
    # IEEE-754 negation is exact, so a weight and its negation always
    # cancel regardless of the decimal value.
    decimal_fly = _fly("SOFR", weights=(0.1, -0.2, 0.1))
    assert is_structurally_zero(_composed(decimal_fly, decimal_fly))


def test_repeated_market_offset_decimal_legs_cancel_exactly():
    # The case a naive left-to-right accumulation gets wrong:
    # 0.1 + 0.2 - 0.1 - 0.2 accumulates to 2.78e-17, but the exact sum
    # is 0. aggregate_leg_weights() uses math.fsum, so it reports 0.0.
    definition = _intermarket(
        (("SOFR", 0, 0.1), ("SOFR", 0, 0.2), ("SOFR", 0, -0.1), ("SOFR", 0, -0.2),
         ("CORRA", 0, 1.0)),
    )
    naive = 0.0
    for weight in (0.1, 0.2, -0.1, -0.2):
        naive += weight
    assert naive != 0.0                                   # the residual is real
    assert aggregate_leg_weights(definition)[("SOFR", 0)] == 0.0  # fsum removes it


def test_weights_that_do_not_genuinely_cancel_are_not_reported_as_zero():
    # 0.1 + 0.2 and 0.3 are DIFFERENT floats, so this group genuinely
    # does not cancel and must not be called zero.
    definition = _intermarket(
        (("SOFR", 0, 0.1), ("SOFR", 0, 0.2), ("SOFR", 0, -0.3)),
    )
    assert aggregate_leg_weights(definition)[("SOFR", 0)] != 0.0
    assert not is_structurally_zero(definition)


def test_aggregate_is_order_independent():
    legs = (("SOFR", 0, 0.1), ("SOFR", 0, 0.2), ("SOFR", 0, 0.35))
    forward = aggregate_leg_weights(_intermarket(legs))
    backward = aggregate_leg_weights(_intermarket(tuple(reversed(legs))))
    assert forward == backward


# ---------------------------------------------------------------------
# Structural zero vs. historically flat -- different layers
# ---------------------------------------------------------------------

def test_historically_flat_series_is_not_a_structural_zero_concern():
    """A real cross-market strategy whose realized prices happen to be
    flat is NOT structurally zero: its definition carries real exposure.
    It stays a candidate and is measured normally by the existing
    analytics (which report 0.0 for genuine zeros and NaN for genuine
    0/0) -- exactly as they already do for any single-market candidate.
    Nothing in this phase filters on data."""
    from strategy_engine.intermarket_combinations import IntermarketStrategyInstance

    definition = _composed(_fly("SOFR"), _fly("CORRA"))
    assert not is_structurally_zero(definition)

    dates = pd.date_range("2026-01-05", periods=30, freq="B")
    history = StrategyHistory(
        instance=IntermarketStrategyInstance(definition=definition, rics=("X",) * 6),
        price_field="Close",
        history=pd.DataFrame({"Date": dates, "Strategy": [0.0] * 30}),
    )
    result = analyze_range(history, lookback=20)

    assert result.observation_count == 20
    assert result.realized_vol_price == 0.0          # a real, defined zero
    assert result.mean_abs_change_price == 0.0
    assert result.oscillation_count == 0
    assert pd.isna(result.efficiency_ratio)          # a genuine 0/0
    assert pd.isna(result.ar1_beta)


# ---------------------------------------------------------------------
# Applied inside resolve_composite_combinations()
# ---------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path) -> StrategySetRepository:
    repository = StrategySetRepository(base_dir=str(tmp_path / "strategy_sets"))
    repository.save(
        StrategySet(
            name="STIR Flys",
            entries=(
                StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR")),
                StrategySetEntry(name="SON Fly", definition=_fly("SONIA")),
                StrategySetEntry(name="SR3 Spread", definition=_spread("SOFR")),
            ),
        )
    )
    repository.save(
        StrategySet(
            name="Other Flys",
            entries=(
                StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR")),
                StrategySetEntry(name="CRA Fly", definition=_fly("CORRA")),
            ),
        )
    )
    return repository


def _composite(a_selected, b_selected, a_source="STIR Flys", b_source="Other Flys") -> StrategySet:
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


# --- (L) same strategy on both sides ------------------------------------

def test_same_strategy_on_both_sides_is_generated_then_dropped(repo):
    composite = _composite(("SR3 Fly",), ("SR3 Fly",))
    # The selection itself is never rejected -- composition still happens
    # and still produces the full negated shape...
    composed = compose_definition(
        _source(_fly("SOFR"), "STIR Flys", "SR3 Fly"),
        _source(_fly("SOFR"), "Other Flys", "SR3 Fly"),
    )
    assert composed.weights == (1.0, -2.0, 1.0, -1.0, 2.0, -1.0)
    # ... it is simply not returned.
    assert resolve_composite_combinations(composite, repo) == []


def test_structurally_zero_pairs_are_dropped_from_a_larger_product(repo):
    composite = _composite(("SR3 Fly", "SON Fly", "SR3 Spread"), ("SR3 Fly", "CRA Fly"))
    assert _names(resolve_composite_combinations(composite, repo)) == [
        # "SR3 Fly - SR3 Fly" dropped
        "SR3 Fly - CRA Fly",
        "SON Fly - SR3 Fly",
        "SON Fly - CRA Fly",
        "SR3 Spread - SR3 Fly",
        "SR3 Spread - CRA Fly",
    ]


# --- (K) Group selection order preserved among survivors ----------------

def test_group_a_outer_group_b_inner_order_is_preserved_among_survivors(repo):
    composite = _composite(("SON Fly", "SR3 Fly"), ("SR3 Fly", "CRA Fly"))
    assert _names(resolve_composite_combinations(composite, repo)) == [
        "SON Fly - SR3 Fly",
        "SON Fly - CRA Fly",
        # "SR3 Fly - SR3 Fly" dropped, leaving Group A's second strategy
        # with only its second Group B partner
        "SR3 Fly - CRA Fly",
    ]


def test_surviving_names_are_never_sorted_alphabetically(repo):
    composite = _composite(("SON Fly",), ("SR3 Fly", "CRA Fly"))
    names = _names(resolve_composite_combinations(composite, repo))
    assert names == ["SON Fly - SR3 Fly", "SON Fly - CRA Fly"]
    assert names != sorted(names)


# --- (J) reverse-direction dedup unchanged ------------------------------

def test_reverse_direction_dedup_still_applies_among_survivors(repo):
    composite = _composite(
        ("SR3 Fly", "SR3 Spread"), ("SR3 Fly",), a_source="STIR Flys", b_source="STIR Flys"
    )
    names = _names(resolve_composite_combinations(composite, repo))
    # (SR3 Fly, SR3 Fly) is structurally zero -> dropped.
    # (SR3 Spread, SR3 Fly) survives, in its Group-A-first orientation.
    assert names == ["SR3 Spread - SR3 Fly"]


def test_orientation_of_survivors_is_still_group_a_then_group_b(repo):
    composite = _composite(("SR3 Spread",), ("CRA Fly",))
    combination = resolve_composite_combinations(composite, repo)[0]
    assert combination.name == "SR3 Spread - CRA Fly"
    assert combination.group_a.entry_name == "SR3 Spread"
    assert combination.group_b.entry_name == "CRA Fly"
    # Leg order carries the same orientation: Group A's legs first.
    assert combination.definition.market_keys == ("SOFR", "SOFR", "CORRA", "CORRA", "CORRA")
    assert combination.definition.weights == (1.0, -1.0, -1.0, 2.0, -1.0)


def test_filtering_is_deterministic_across_repeated_resolution(repo):
    composite = _composite(("SR3 Fly", "SON Fly", "SR3 Spread"), ("SR3 Fly", "CRA Fly"))
    first = resolve_composite_combinations(composite, repo)
    second = resolve_composite_combinations(composite, repo)
    assert _names(first) == _names(second)
    assert [c.definition.legs for c in first] == [c.definition.legs for c in second]


def test_both_self_pairs_drop_and_the_cross_pair_survives_once(repo):
    # A = B = [SR3 Fly, SR3 Spread]: (Fly, Fly) and (Spread, Spread) are
    # structurally zero; (Fly, Spread) survives and (Spread, Fly) is its
    # reverse-direction duplicate.
    composite = _composite(("SR3 Fly", "SR3 Spread"), ("SR3 Fly", "SR3 Spread"),
                           a_source="STIR Flys", b_source="STIR Flys")
    assert _names(resolve_composite_combinations(composite, repo)) == [
        "SR3 Fly - SR3 Spread"
    ]


def test_a_wholly_structurally_zero_product_resolves_to_no_combinations(repo):
    composite = _composite(("SR3 Fly",), ("SR3 Fly",))
    assert resolve_composite_combinations(composite, repo) == []


# --- (M) the optimization boundary: no downstream work ------------------

def test_a_structurally_zero_pair_never_reaches_instance_generation(repo, mocker):
    """The architectural property: a dropped pair costs no calendar
    rolling at all -- generate_intermarket_instances() is never called
    with its definition."""
    # Spy on the name as composite.py resolves it -- expand_combinations()
    # calls it through this module's own namespace.
    spy = mocker.spy(composite_module, "generate_intermarket_instances")
    composite = _composite(("SR3 Fly",), ("SR3 Fly", "CRA Fly"))
    instances = expand_strategy_set(composite, _START, _END, repository=repo)

    # Exactly one definition was rolled -- the surviving cross-market one.
    assert spy.call_count == 1
    rolled_definition = spy.call_args_list[0].args[0]
    assert rolled_definition.market_keys == ("SOFR",) * 3 + ("CORRA",) * 3
    assert not is_structurally_zero(rolled_definition)
    assert instances


def test_a_structurally_zero_pair_reaches_no_provider_or_cache_work(repo, mocker):
    """End of the same boundary: the dropped pair's legs are never
    fetched. SONIA appears ONLY in the structurally-zero pair here, so
    if any SONIA RIC is requested the pair leaked downstream."""
    from template_scanner.scanner import run_scan_on_instances

    dates = pd.date_range("2026-02-02", periods=20, freq="B")

    def _batch(rics, interval, start, end):
        return {
            ric: pd.DataFrame(
                {
                    "Date": dates, "Open": 96.0, "High": 96.0, "Low": 96.0,
                    "Close": 96.0, "Volume": 1000.0,
                }
            )
            for ric in rics
        }

    fetch = mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch)
    lazy = mocker.patch("strategy_engine.pricing.get_history")

    # The ONLY pair in this product is structurally zero.
    zero_only = _composite(("SR3 Fly",), ("SR3 Fly",))
    instances = expand_strategy_set(zero_only, _START, _END, repository=repo)
    assert instances == []
    run_scan_on_instances(instances, "2026-02-02", "2026-02-27", lookbacks=(5,))
    assert fetch.call_count == 0   # no batch prewarm
    assert lazy.call_count == 0    # no lazy per-RIC fetch either

    # Sanity: a surviving combination in the same setup DOES get priced,
    # so the assertions above are not vacuous.
    surviving = _composite(("SR3 Fly",), ("CRA Fly",))
    priced = expand_strategy_set(surviving, _START, _END, repository=repo)
    run_scan_on_instances(priced, "2026-02-02", "2026-02-27", lookbacks=(5,))
    assert fetch.call_count > 0


# --- (P) end-to-end through the real expansion entry point --------------

def test_end_to_end_composite_expansion_excludes_structurally_zero(repo):
    composite = _composite(("SR3 Fly", "SON Fly"), ("SR3 Fly", "CRA Fly"))
    instances = expand_strategy_set(composite, _START, _END, repository=repo)
    assert instances

    market_shapes = {i.definition.market_keys for i in instances}
    # The three surviving combinations, and nothing SOFR-only (which is
    # the only shape "SR3 Fly - SR3 Fly" could have produced).
    assert market_shapes == {
        ("SOFR",) * 3 + ("CORRA",) * 3,   # SR3 Fly - CRA Fly
        ("SONIA",) * 3 + ("SOFR",) * 3,   # SON Fly - SR3 Fly
        ("SONIA",) * 3 + ("CORRA",) * 3,  # SON Fly - CRA Fly
    }
    assert all(not is_structurally_zero(i.definition) for i in instances)


def test_saved_composite_round_trips_and_still_filters(repo):
    repo.save(_composite(("SR3 Fly", "SON Fly"), ("SR3 Fly", "CRA Fly")))
    reloaded = repo.load("Combos")
    assert _names(resolve_composite_combinations(reloaded, repo)) == [
        "SR3 Fly - CRA Fly",
        "SON Fly - SR3 Fly",
        "SON Fly - CRA Fly",
    ]


# --- (N) (O) existing behaviour untouched -------------------------------

def test_ordinary_single_market_expansion_is_unaffected(repo):
    plain = StrategySet(
        name="Plain", entries=(StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR")),)
    )
    instances = expand_strategy_set(plain, _START, _END)
    assert instances
    assert all(i.definition is plain.entries[0].definition for i in instances)


def test_a_hand_authored_self_cancelling_intermarket_entry_is_never_dropped(repo):
    """Structural-zero filtering applies ONLY to composed Group A x
    Group B combinations. A trader who hand-authors a self-cancelling
    Module 9 intermarket entry still gets it expanded -- Phase 3 does
    not reach into the intermarket_entries path at all."""
    self_cancelling = IntermarketStrategySetEntry(
        name="Self cancelling",
        definition=_intermarket((("SOFR", 0, 1.0), ("SOFR", 0, -1.0), ("CORRA", 0, 1.0),
                                 ("CORRA", 0, -1.0))),
    )
    assert is_structurally_zero(self_cancelling.definition)

    strategy_set = StrategySet(
        name="Hand Authored", entries=(), intermarket_entries=(self_cancelling,)
    )
    instances = expand_strategy_set(strategy_set, _START, _END)
    assert instances
    assert all(i.definition is self_cancelling.definition for i in instances)
