"""
tests/test_composite_execution.py

Phase 4 execution foundation: strategy_sets.execution.run_strategy_set()
as the ONE downstream path into template_scanner.scanner.
run_scan_on_instances() for every kind of Strategy Set content --
ordinary entries, hand-authored Module 9 intermarket entries, and
composite Group A x Group B groups, freely mixed.

Real objects throughout: real StrategyDefinition/IntermarketDefinition
shapes, a real tmp_path-backed StrategySetRepository, and real contract
RIC generation via core.futures_calendar. Only the leg-batch fetch is
mocked (strategy_engine.pricing.get_history_batch), the same convention
tests/test_composite_strategy_set_end_to_end.py uses -- no LSEG, no
QuantHub, no SQLite.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pandas as pd
import pytest

from core.config import BarInterval

from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_combinations import IntermarketStrategyInstance
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec

import strategy_sets.composite as composite_module
import strategy_sets.execution as execution_module
from strategy_sets.composite import CompositeResolutionError, is_structurally_zero
from strategy_sets.execution import run_strategy_set
from strategy_sets.model import (
    IntermarketStrategySetEntry,
    StrategyGroup,
    StrategyGroupPair,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository

_CONTRACT_START, _CONTRACT_END = "2026-01-01", "2026-09-30"
_PRICE_START, _PRICE_END = "2026-02-02", "2026-02-27"
_PRICE_DATES = pd.date_range(_PRICE_START, _PRICE_END, freq="B")

# Distinct level per RIC so a composed strategy's arithmetic is checkable.
_CLOSES = {
    "SRAH26": 96.10, "SRAM26": 96.24, "SRAU26": 96.33,
    "CRAH6": 97.11, "CRAM6": 97.27, "CRAU6": 97.35,
    "SONH6": 95.10, "SONM6": 95.22, "SONU6": 95.31,
}


def _leg_df(ric: str) -> pd.DataFrame:
    base = _CLOSES.get(ric, 99.0)
    n = len(_PRICE_DATES)
    closes = [base + (i % 5) * 0.01 for i in range(n)]
    return pd.DataFrame(
        {
            "Date": _PRICE_DATES, "Open": closes, "High": closes,
            "Low": closes, "Close": closes, "Volume": [1000.0] * n,
        }
    )


@pytest.fixture
def fetch(mocker):
    """The one mocked boundary: every leg's history arrives through
    prewarm_leg_cache()'s batch call. Returned so tests can assert on
    exactly which RICs were requested."""
    return mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=lambda rics, interval, start, end: {r: _leg_df(r) for r in rics},
    )


# ---------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------

def _fly(market_key: str, interval=BarInterval.DAILY, price_field="Close") -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0),
        interval=interval, price_field=price_field,
    )


def _spread(market_key: str, interval=BarInterval.DAILY) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1), weights=(1.0, -1.0), interval=interval,
    )


def _basis_entry(name="SOFR vs CORRA") -> IntermarketStrategySetEntry:
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("CORRA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    return IntermarketStrategySetEntry(name=name, definition=definition)


def _groups(a=("SR3 Fly", "SON Fly"), b=("SR3 Fly", "CRA Fly"),
            a_source="STIR Flys", b_source="Other Flys") -> StrategyGroupPair:
    return StrategyGroupPair(
        group_a=StrategyGroup(source_set_name=a_source, selected_entry_names=a),
        group_b=StrategyGroup(source_set_name=b_source, selected_entry_names=b),
    )


@pytest.fixture
def repo(tmp_path) -> StrategySetRepository:
    """Two source Strategy Sets, deliberately persisted at intervals
    that differ from each other AND from the runtime scan interval the
    tests request -- see the interval-override tests below."""
    repository = StrategySetRepository(base_dir=str(tmp_path / "strategy_sets"))
    repository.save(
        StrategySet(
            name="STIR Flys",
            entries=(
                StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR", BarInterval.HOURLY)),
                StrategySetEntry(name="SON Fly", definition=_fly("SONIA", BarInterval.HOURLY)),
            ),
        )
    )
    repository.save(
        StrategySet(
            name="Other Flys",
            entries=(
                StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR", BarInterval.FOUR_HOUR)),
                StrategySetEntry(name="CRA Fly", definition=_fly("CORRA", BarInterval.FOUR_HOUR)),
            ),
        )
    )
    return repository


def _run(strategy_set, repo=None, interval=BarInterval.DAILY, **overrides):
    kwargs = dict(
        lookbacks=(5,),
        repository=repo,
    )
    kwargs.update(overrides)
    return run_strategy_set(
        strategy_set, interval, _CONTRACT_START, _CONTRACT_END,
        _PRICE_START, _PRICE_END, **kwargs,
    )


def _labels(report) -> set[str]:
    return {r.label for r in report.results}


# ---------------------------------------------------------------------
# 1-3. Repository context and composite-only execution
# ---------------------------------------------------------------------

def test_run_strategy_set_with_repository_resolves_composite_groups(repo, fetch):
    _request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups()), repo=repo
    )
    assert report.skipped == ()
    assert report.results
    assert _labels(report) == {
        "SR3 Fly - CRA Fly", "SON Fly - SR3 Fly", "SON Fly - CRA Fly",
    }


def test_run_strategy_set_without_repository_fails_clearly_for_a_composite(repo, fetch):
    with pytest.raises(CompositeResolutionError, match="repository"):
        _run(StrategySet(name="Combos", entries=(), groups=_groups()), repo=None)


def test_run_strategy_set_never_constructs_its_own_repository():
    """Repository context is explicit/call-time -- execution must not
    reach for a default StrategySetRepository (hidden filesystem I/O
    that would read the real data/strategy_sets/ in tests).

    Checked against the parsed AST, not the source text: the module
    docstring legitimately mentions StrategySetRepository() while
    explaining why it is never constructed here."""
    tree = ast.parse(inspect.getsource(execution_module))
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "StrategySetRepository" not in constructed


def test_composite_only_strategy_set_executes(repo, fetch):
    """entries == () is legitimate: a composite's strategies are
    referenced through `groups`. The ScanRequest returned for UI session
    state therefore carries an empty `definitions` rather than a
    fabricated placeholder."""
    request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups()), repo=repo
    )
    assert request.definitions == ()
    assert request.price_start == _PRICE_START
    assert request.lookbacks == (5,)
    assert len(report.results) == 3


# ---------------------------------------------------------------------
# 4-6. Ordinary / intermarket / mixed
# ---------------------------------------------------------------------

def test_ordinary_only_strategy_set_still_executes(repo, fetch):
    ordinary = StrategySet(
        name="Plain",
        entries=(
            StrategySetEntry(name="SOFR Fly", definition=_fly("SOFR")),
            StrategySetEntry(name="SONIA Spread", definition=_spread("SONIA")),
        ),
    )
    _request, report = _run(ordinary, repo=repo)
    assert report.results
    assert all(isinstance(r.instance, StrategyInstance) for r in report.results)
    assert _labels(report) == {"SOFR Fly", "SONIA Spread"}


def test_intermarket_only_strategy_set_executes(repo, fetch):
    """The Module 9 gap Phase 4 closes: a hand-authored intermarket
    entry was displayed but never scannable."""
    entry = _basis_entry()
    strategy_set = StrategySet(name="Basis", entries=(), intermarket_entries=(entry,))
    _request, report = _run(strategy_set, repo=repo)
    assert report.results
    assert all(isinstance(r.instance, IntermarketStrategyInstance) for r in report.results)
    assert _labels(report) == {"SOFR vs CORRA"}


def test_mixed_strategy_set_executes_as_one_report(repo, fetch):
    mixed = StrategySet(
        name="Everything",
        entries=(StrategySetEntry(name="SOFR Fly", definition=_fly("SOFR")),),
        intermarket_entries=(_basis_entry(),),
        groups=_groups(a=("SR3 Fly",), b=("CRA Fly",)),
    )
    _request, report = _run(mixed, repo=repo)

    single = [r for r in report.results if isinstance(r.instance, StrategyInstance)]
    inter = [r for r in report.results if isinstance(r.instance, IntermarketStrategyInstance)]
    assert single and inter
    # ONE report, not one per category.
    assert len(report.results) == len(single) + len(inter)
    assert _labels(report) == {"SOFR Fly", "SOFR vs CORRA", "SR3 Fly - CRA Fly"}


# ---------------------------------------------------------------------
# 7-8. Runtime interval reaches composite sources; sources unchanged
# ---------------------------------------------------------------------

def test_runtime_interval_reaches_every_composite_source_definition(repo, fetch):
    """The sources are persisted at HOURLY (Group A) and FOUR_HOUR
    (Group B) -- deliberately mismatched, and neither is the requested
    scan interval."""
    _request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups()),
        repo=repo, interval=BarInterval.DAILY,
    )
    assert report.results
    assert {r.interval for r in report.results} == {BarInterval.DAILY}
    for result in report.results:
        assert result.instance.definition.interval is BarInterval.DAILY


def test_mismatched_persisted_source_intervals_no_longer_block_a_scan(repo, fetch):
    """Group A persisted HOURLY, Group B persisted FOUR_HOUR. Before the
    runtime override reached them, composing the pair raised "a combined
    strategy needs one interval". compose_definition()'s check is
    untouched -- it simply cannot fire once both sides are normalised
    first."""
    _request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups()),
        repo=repo, interval=BarInterval.HOURLY,
    )
    assert len(report.results) == 3
    assert {r.interval for r in report.results} == {BarInterval.HOURLY}


def test_runtime_interval_applies_to_every_entry_kind_at_once(repo, fetch):
    mixed = StrategySet(
        name="Everything",
        entries=(StrategySetEntry(name="SOFR Fly", definition=_fly("SOFR", BarInterval.HOURLY)),),
        intermarket_entries=(_basis_entry(),),
        groups=_groups(a=("SR3 Fly",), b=("CRA Fly",)),
    )
    _request, report = _run(mixed, repo=repo, interval=BarInterval.FOUR_HOUR)
    assert report.results
    assert {r.interval for r in report.results} == {BarInterval.FOUR_HOUR}


def test_source_strategy_sets_are_not_modified_by_execution(repo, fetch):
    before = {
        name: (Path(repo.base_dir) / f"{name}.json").read_text()
        for name in repo.list_names()
    }
    mtimes = {
        name: (Path(repo.base_dir) / f"{name}.json").stat().st_mtime_ns
        for name in repo.list_names()
    }

    _run(StrategySet(name="Combos", entries=(), groups=_groups()), repo=repo)

    for name, text in before.items():
        path = Path(repo.base_dir) / f"{name}.json"
        assert path.read_text() == text
        assert path.stat().st_mtime_ns == mtimes[name]
    # No transient set was written either.
    assert sorted(repo.list_names()) == sorted(before)


def test_source_definitions_keep_their_own_persisted_interval(repo, fetch):
    _run(StrategySet(name="Combos", entries=(), groups=_groups()), repo=repo,
         interval=BarInterval.DAILY)
    reloaded = repo.load("STIR Flys")
    assert {e.definition.interval for e in reloaded.entries} == {BarInterval.HOURLY}
    assert json.loads((Path(repo.base_dir) / "STIR Flys.json").read_text())["entries"][0][
        "interval"
    ] == "HOURLY"


# ---------------------------------------------------------------------
# 9-11. Labels, orientation, reverse-direction duplicates
# ---------------------------------------------------------------------

def test_composite_labels_propagate_to_scan_candidate_results(repo, fetch):
    _request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups(a=("SR3 Fly",), b=("CRA Fly",))),
        repo=repo,
    )
    assert report.results
    assert all(r.label == "SR3 Fly - CRA Fly" for r in report.results)


def test_labels_preserve_group_a_then_group_b_orientation(repo, fetch):
    _request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups(a=("CRA Fly",), b=("SR3 Fly",),
                                                             a_source="Other Flys",
                                                             b_source="STIR Flys")),
        repo=repo,
    )
    assert _labels(report) == {"CRA Fly - SR3 Fly"}
    # Leg order carries the same orientation: Group A's market first.
    for result in report.results:
        assert result.instance.definition.market_keys[:3] == ("CORRA",) * 3


def test_reverse_direction_duplicates_are_not_generated(repo, fetch):
    """A = [SR3, CRA], B = [CRA, SR3] over one source set: the product
    reaches both (SR3, CRA) and (CRA, SR3) -- the same unordered pair --
    plus two self-pairs that are structurally zero."""
    _request, report = _run(
        StrategySet(
            name="Combos", entries=(),
            groups=_groups(a=("SR3 Fly", "CRA Fly"), b=("CRA Fly", "SR3 Fly"),
                           a_source="Other Flys", b_source="Other Flys"),
        ),
        repo=repo,
    )
    assert _labels(report) == {"SR3 Fly - CRA Fly"}
    assert "CRA Fly - SR3 Fly" not in _labels(report)


# ---------------------------------------------------------------------
# 12-14. Structural zero on the execution path
# ---------------------------------------------------------------------

def test_structurally_zero_pairs_never_reach_instance_generation(repo, fetch, mocker):
    """The Phase 3 optimization boundary must survive Phase 4: a dropped
    pair costs no calendar rolling, no pricing, no analytics."""
    spy = mocker.spy(composite_module, "generate_intermarket_instances")
    _run(
        StrategySet(name="Combos", entries=(), groups=_groups(a=("SR3 Fly",),
                                                             b=("SR3 Fly", "CRA Fly"))),
        repo=repo,
    )
    assert spy.call_count == 1  # only the surviving cross-market pair
    rolled = spy.call_args_list[0].args[0]
    assert not is_structurally_zero(rolled)


def test_partially_surviving_composite_product_produces_only_survivors(repo, fetch):
    _request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups()), repo=repo
    )
    assert _labels(report) == {
        "SR3 Fly - CRA Fly", "SON Fly - SR3 Fly", "SON Fly - CRA Fly",
    }
    assert "SR3 Fly - SR3 Fly" not in _labels(report)


def test_all_zero_composite_product_yields_an_empty_report_not_an_exception(repo, fetch):
    _request, report = _run(
        StrategySet(name="Combos", entries=(),
                    groups=_groups(a=("SR3 Fly",), b=("SR3 Fly",))),
        repo=repo,
    )
    assert report.results == ()
    assert report.skipped == ()
    # Nothing was priced at all.
    assert fetch.call_count == 0


# ---------------------------------------------------------------------
# 15-17. Clear failures
# ---------------------------------------------------------------------

def test_nested_composite_source_still_fails_clearly(repo, fetch):
    repo.save(StrategySet(name="Inner Combo", entries=(), groups=_groups()))
    with pytest.raises(CompositeResolutionError, match="[Nn]ested composite"):
        _run(
            StrategySet(name="Outer", entries=(),
                        groups=_groups(a=("SR3 Fly",), a_source="Inner Combo")),
            repo=repo,
        )


def test_missing_source_strategy_set_fails_clearly(repo, fetch):
    with pytest.raises(CompositeResolutionError) as exc:
        _run(
            StrategySet(name="Combos", entries=(), groups=_groups(b_source="Gone")),
            repo=repo,
        )
    message = str(exc.value)
    assert "Combos" in message and "Group B" in message and "Gone" in message


def test_missing_selected_strategy_fails_clearly(repo, fetch):
    with pytest.raises(CompositeResolutionError) as exc:
        _run(
            StrategySet(name="Combos", entries=(),
                        groups=_groups(a=("SR3 Fly", "Deleted"))),
            repo=repo,
        )
    message = str(exc.value)
    assert "Group A" in message and "STIR Flys" in message and "Deleted" in message


def test_incompatible_price_fields_fail_clearly(repo, fetch):
    repo.save(
        StrategySet(
            name="High Field",
            entries=(
                StrategySetEntry(
                    name="SR3 Fly High", definition=_fly("SOFR", price_field="High")
                ),
            ),
        )
    )
    with pytest.raises(CompositeResolutionError, match="one price field"):
        _run(
            StrategySet(
                name="Combos", entries=(),
                groups=_groups(a=("SR3 Fly",), b=("SR3 Fly High",), b_source="High Field"),
            ),
            repo=repo,
        )


# ---------------------------------------------------------------------
# 19-20. One downstream path; no composite-specific provider logic
# ---------------------------------------------------------------------

def test_every_entry_kind_reaches_the_one_run_scan_on_instances_call(repo, fetch, mocker):
    spy = mocker.spy(execution_module, "run_scan_on_instances")
    mixed = StrategySet(
        name="Everything",
        entries=(StrategySetEntry(name="SOFR Fly", definition=_fly("SOFR")),),
        intermarket_entries=(_basis_entry(),),
        groups=_groups(a=("SR3 Fly",), b=("CRA Fly",)),
    )
    _run(mixed, repo=repo)

    spy.assert_called_once()
    instances = spy.call_args[0][0]
    # ONE mixed list, both instance types, handed to ONE call.
    assert any(isinstance(i, StrategyInstance) for i in instances)
    assert any(isinstance(i, IntermarketStrategyInstance) for i in instances)


def test_composite_execution_introduces_no_provider_specific_logic():
    """Everything below expansion must stay unaware of where an instance
    came from: neither the composition layer nor the execution layer may
    reach into provider/cache/downloader code."""
    for module in (composite_module, execution_module):
        source = inspect.getsource(module)
        for forbidden in ("core.providers", "core.downloader", "core.quanthub", "lseg"):
            assert f"import {forbidden}" not in source
            assert f"from {forbidden}" not in source


def test_composite_legs_are_fetched_as_ordinary_rics(repo, fetch):
    """A composite instance reaches the provider boundary as nothing but
    a list of RICs -- no composite label, no source-set identity."""
    _run(StrategySet(name="Combos", entries=(),
                     groups=_groups(a=("SR3 Fly",), b=("CRA Fly",))), repo=repo)
    requested = {ric for call in fetch.call_args_list for ric in call.args[0]}
    assert requested == {"SRAH26", "SRAM26", "SRAU26", "CRAH6", "CRAM6", "CRAU6"}


def test_a_shared_leg_is_fetched_once_across_entry_kinds(repo, fetch):
    """No duplicate provider work: prewarm_leg_cache() dedupes across
    the whole mixed list, ordinary and composite instances alike."""
    mixed = StrategySet(
        name="Everything",
        entries=(StrategySetEntry(name="SOFR Fly", definition=_fly("SOFR")),),
        groups=_groups(a=("SR3 Fly",), b=("CRA Fly",)),
    )
    _run(mixed, repo=repo)
    requested = [ric for call in fetch.call_args_list for ric in call.args[0]]
    assert len(requested) == len(set(requested))


# ---------------------------------------------------------------------
# Empty-result semantics (Part 12)
# ---------------------------------------------------------------------

def test_contract_window_too_short_yields_no_instances_not_a_crash(repo, fetch):
    """A three-leg fly needs three listed contracts; a one-month window
    has too few. Existing Module 9 semantics: no instance, no error."""
    _request, report = run_strategy_set(
        StrategySet(name="Combos", entries=(), groups=_groups(a=("SR3 Fly",), b=("CRA Fly",))),
        BarInterval.DAILY,
        "2026-01-01", "2026-01-31",
        _PRICE_START, _PRICE_END,
        lookbacks=(5,), repository=repo,
    )
    assert report.results == ()
    assert report.skipped == ()


def test_insufficient_history_is_reported_as_analytics_nan_not_an_exception(repo, mocker):
    """Existing behaviour: a short series is not an error -- it flows
    through as NaN-heavy RangeAnalytics with the real observation count."""
    short_dates = pd.date_range(_PRICE_START, periods=2, freq="B")
    mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=lambda rics, interval, start, end: {
            r: pd.DataFrame(
                {"Date": short_dates, "Open": 96.0, "High": 96.0, "Low": 96.0,
                 "Close": 96.0, "Volume": 1.0}
            )
            for r in rics
        },
    )
    _request, report = _run(
        StrategySet(name="Combos", entries=(), groups=_groups(a=("SR3 Fly",), b=("CRA Fly",))),
        repo=repo,
    )
    assert report.results
    headline = report.results[0].multi_lookback.per_lookback[0]
    assert headline.observation_count == 2
    assert pd.isna(headline.efficiency_ratio) or headline.efficiency_ratio == 0.0
