"""
tests/test_composite_strategy_set_end_to_end.py

Phase 2's integration test: the complete chain for a COMPOSITE
("Group A x Group B") Strategy Set

    saved source Strategy Sets (real StrategySetRepository, tmp_path)
        -> composite StrategySet JSON -> repository.load()
        -> expand_strategy_set(repository=...)
        -> strategy_sets.composite composes each A/B pair into ONE
           IntermarketDefinition
        -> Module 9's own generate_intermarket_instances() rolls it
        -> template_scanner.scanner.run_scan_on_instances()
        -> range_analytics
        -> ScanCandidateResult

proving that a generated combination is indistinguishable from a
hand-authored intermarket entry by the time it reaches pricing and
analytics -- no second expansion engine, no scanner change, no pricing
change.

Follows tests/test_intermarket_strategy_set_end_to_end.py's own
"mock the leg-batch fetch, use real everything else" convention.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_combinations import IntermarketStrategyInstance

from strategy_sets.composite import (
    composite_labels_by_definition_id,
    expand_combinations,
    resolve_composite_combinations,
)
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import (
    StrategyGroup,
    StrategyGroupPair,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository

from template_scanner.scanner import run_scan_on_instances

_CONTRACT_START, _CONTRACT_END = "2026-01-01", "2026-09-30"
_PRICE_START, _PRICE_END = "2026-02-02", "2026-02-27"
_PRICE_DATES = pd.date_range(_PRICE_START, _PRICE_END, freq="B").strftime("%Y-%m-%d").tolist()

# One distinct, deterministic price level per RIC so a composed
# strategy's own arithmetic is verifiable by hand below.
_CLOSES = {
    "SRAH26": 96.10, "SRAM26": 96.20, "SRAU26": 96.30,
    "CRAH6": 97.10, "CRAM6": 97.20, "CRAU6": 97.30,
}


def _leg_df(ric: str) -> pd.DataFrame:
    close = _CLOSES[ric]
    n = len(_PRICE_DATES)
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(_PRICE_DATES),
            "Open": [close] * n,
            "High": [close] * n,
            "Low": [close] * n,
            "Close": [close] * n,
            "Volume": [1000.0] * n,
        }
    )


@pytest.fixture(autouse=True)
def _mock_leg_fetch(mocker):
    """Every leg's history comes from prewarm_leg_cache()'s batch call
    -- no LSEG, no QuantHub, no SQLite involvement in this test."""
    mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=lambda rics, interval, start, end: {ric: _leg_df(ric) for ric in rics},
    )


def _fly(market_key: str) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0),
        interval=BarInterval.DAILY,
    )


@pytest.fixture
def repo(tmp_path) -> StrategySetRepository:
    repository = StrategySetRepository(base_dir=str(tmp_path / "strategy_sets"))
    repository.save(
        StrategySet(
            name="STIR Flys",
            entries=(StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR")),),
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
    repository.save(
        StrategySet(
            name="Combos",
            entries=(),
            groups=StrategyGroupPair(
                group_a=StrategyGroup("STIR Flys", ("SR3 Fly",)),
                group_b=StrategyGroup("Other Flys", ("SR3 Fly", "CRA Fly")),
            ),
        )
    )
    return repository


def test_composite_strategy_set_reaches_scan_candidate_results(repo):
    # --- saved composite -> load -> expand ---
    composite = repo.load("Combos")
    combinations = resolve_composite_combinations(composite, repo)
    # Two pairs are formed; "SR3 Fly - SR3 Fly" is structurally zero and
    # dropped before it ever becomes an instance (Phase 3).
    assert [c.name for c in combinations] == ["SR3 Fly - CRA Fly"]

    # The labelled path: resolve ONCE, roll that same list, so each
    # candidate's definition object is the one the label map is keyed on
    # (see composite_labels_by_definition_id's identity caveat).
    instances = expand_combinations(combinations, _CONTRACT_START, _CONTRACT_END)
    assert instances
    assert all(isinstance(i, IntermarketStrategyInstance) for i in instances)
    # ... and it produces exactly what the unlabelled expand_strategy_set()
    # entry point produces for the same composite.
    assert [i.rics for i in instances] == [
        i.rics
        for i in expand_strategy_set(
            composite, _CONTRACT_START, _CONTRACT_END, repository=repo
        )
    ]

    # --- scanner -> analytics -> ScanCandidateResult ---
    report = run_scan_on_instances(
        instances,
        _PRICE_START,
        _PRICE_END,
        lookbacks=(5,),
        labels_by_definition_id=composite_labels_by_definition_id(combinations),
    )
    assert report.skipped == ()
    assert len(report.results) == len(instances)

    # Every candidate carries its "Group A - Group B" name, unreversed.
    assert set(r.label for r in report.results) == {"SR3 Fly - CRA Fly"}

    # Display market key is the composite label -- cosmetic only; each
    # leg's own market_key stayed authoritative for RIC construction.
    cross = [r for r in report.results if r.label == "SR3 Fly - CRA Fly"]
    assert {r.market_key for r in cross} == {"SOFR/SOFR/SOFR/CORRA/CORRA/CORRA"}
    # The structurally-zero pair produced no candidate at all.
    assert not [r for r in report.results if r.label == "SR3 Fly - SR3 Fly"]


def test_cross_market_composite_prices_as_a_minus_b(repo):
    composite = repo.load("Combos")
    combinations = resolve_composite_combinations(composite, repo)
    instances = expand_combinations(combinations, _CONTRACT_START, _CONTRACT_END)
    report = run_scan_on_instances(
        instances, _PRICE_START, _PRICE_END, lookbacks=(5,),
        labels_by_definition_id=composite_labels_by_definition_id(combinations),
    )

    front = [
        r for r in report.results
        if r.label == "SR3 Fly - CRA Fly"
        and r.rics == ("SRAH26", "SRAM26", "SRAU26", "CRAH6", "CRAM6", "CRAU6")
    ]
    assert len(front) == 1

    sofr_fly = _CLOSES["SRAH26"] - 2 * _CLOSES["SRAM26"] + _CLOSES["SRAU26"]
    corra_fly = _CLOSES["CRAH6"] - 2 * _CLOSES["CRAM6"] + _CLOSES["CRAU6"]
    headline = front[0].multi_lookback.per_lookback[0]
    assert headline.current_price == pytest.approx(sofr_fly - corra_fly)


def test_same_strategy_combination_never_reaches_the_scan(repo):
    """"SR3 Fly - SR3 Fly" would price to an identically-zero series, so
    Phase 3 removes it at composition time -- it never becomes an
    instance, is never priced, and never appears in a ScanReport."""
    composite = repo.load("Combos")
    combinations = resolve_composite_combinations(composite, repo)
    instances = expand_combinations(combinations, _CONTRACT_START, _CONTRACT_END)
    report = run_scan_on_instances(
        instances, _PRICE_START, _PRICE_END, lookbacks=(5,),
        labels_by_definition_id=composite_labels_by_definition_id(combinations),
    )

    assert not [c for c in combinations if c.name == "SR3 Fly - SR3 Fly"]
    assert not [r for r in report.results if r.label == "SR3 Fly - SR3 Fly"]
    # No instance anywhere consists solely of repeated SOFR legs (which
    # is the only shape the dropped pair could have produced).
    assert not [i for i in instances if set(i.definition.market_keys) == {"SOFR"}]
    # The surviving cross-market combination is untouched.
    assert {r.label for r in report.results} == {"SR3 Fly - CRA Fly"}


def test_composite_and_ordinary_entries_scan_together_in_one_report(repo):
    composite = StrategySet(
        name="Combos Plus",
        entries=(StrategySetEntry(name="Own SOFR Fly", definition=_fly("SOFR")),),
        groups=StrategyGroupPair(
            group_a=StrategyGroup("STIR Flys", ("SR3 Fly",)),
            group_b=StrategyGroup("Other Flys", ("CRA Fly",)),
        ),
    )
    instances = expand_strategy_set(composite, _CONTRACT_START, _CONTRACT_END, repository=repo)
    report = run_scan_on_instances(instances, _PRICE_START, _PRICE_END, lookbacks=(5,))

    assert report.skipped == ()
    assert len(report.results) == len(instances)
    market_keys = {r.market_key for r in report.results}
    assert "SOFR" in market_keys                                    # the ordinary entry
    assert "SOFR/SOFR/SOFR/CORRA/CORRA/CORRA" in market_keys        # the combination
