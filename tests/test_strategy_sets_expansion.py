"""
tests/test_strategy_sets_expansion.py

expand_strategy_set() tested against REAL contract RIC generation (via
core.futures_calendar -- pure calendar arithmetic, no I/O, no
LSEG/database involved), mirroring tests/test_template_scanner_
universe.py's own approach. Every expected RIC list here was verified
against generate_candidates() directly, the same function
expand_strategy_set() delegates to per entry -- so these tests check
"expansion produces existing StrategyInstance objects, orchestrated
correctly across a whole StrategySet's entries", not re-deriving the
rolling logic itself.

contract_start/contract_end are call-time arguments to
expand_strategy_set() (matching ScanRequest), NOT stored on any entry
-- see model.py's "Design correction" note for why. Only
max_curve_position/eligible_rics remain per-entry.
"""

from __future__ import annotations

import inspect

import template_scanner.scanner
from core.config import BarInterval
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import ExpansionSettings, StrategySet, StrategySetEntry
from template_scanner.universe import generate_candidates

_START, _END = "2026-01-01", "2027-12-31"


def _fly_definition() -> StrategyDefinition:
    return StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )


def _spread_definition() -> StrategyDefinition:
    return StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )


def _sonia_definition() -> StrategyDefinition:
    return StrategyDefinition(
        market_key="SONIA", offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )


def _fly_entry(name="SOFR Fly", **expansion_overrides) -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_fly_definition(), expansion=ExpansionSettings(**expansion_overrides))


def _spread_entry(name="SOFR Spread") -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_spread_definition(), expansion=ExpansionSettings())


def _sonia_entry(name="SONIA Outright") -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_sonia_definition(), expansion=ExpansionSettings())


def test_expand_returns_strategy_instance_objects():
    s = StrategySet(name="Churning", entries=(_fly_entry(),))
    result = expand_strategy_set(s, _START, _END)
    assert result
    assert all(isinstance(inst, StrategyInstance) for inst in result)


def test_expand_single_entry_matches_generate_candidates_directly():
    s = StrategySet(name="Churning", entries=(_fly_entry(),))
    expected = generate_candidates(_fly_definition(), _START, _END)
    result = expand_strategy_set(s, _START, _END)
    assert result == expected
    assert len(result) == 6  # verified against test_template_scanner_universe.py's own fly count


def test_expand_combines_multiple_distinct_entries():
    s = StrategySet(name="Churning", entries=(_fly_entry(), _spread_entry()))
    result = expand_strategy_set(s, _START, _END)
    expected = generate_candidates(_fly_definition(), _START, _END) + generate_candidates(_spread_definition(), _START, _END)
    assert len(result) == len(expected)
    assert result == expected  # no overlap between a fly and a spread -- dedupe is a no-op here


def test_expand_dedupes_duplicate_entries_by_default():
    single = expand_strategy_set(StrategySet(name="A", entries=(_fly_entry("A"),)), _START, _END)
    s = StrategySet(name="Churning", entries=(_fly_entry("A"), _fly_entry("B")))
    result = expand_strategy_set(s, _START, _END)
    assert len(result) == len(single)


def test_expand_dedupe_false_keeps_duplicates():
    single = expand_strategy_set(StrategySet(name="A", entries=(_fly_entry("A"),)), _START, _END)
    s = StrategySet(name="Churning", entries=(_fly_entry("A"), _fly_entry("B")))
    result = expand_strategy_set(s, _START, _END, dedupe=False)
    assert len(result) == 2 * len(single)


def test_expand_skips_disabled_entries_by_default():
    enabled_entry = _fly_entry("SOFR Fly")
    disabled_entry = StrategySetEntry(
        name="SOFR Spread", definition=_spread_definition(), expansion=ExpansionSettings(), enabled=False,
    )
    s = StrategySet(name="Churning", entries=(enabled_entry, disabled_entry))
    result = expand_strategy_set(s, _START, _END)
    assert result == generate_candidates(_fly_definition(), _START, _END)


def test_expand_only_enabled_false_includes_disabled_entries():
    disabled_entry = StrategySetEntry(
        name="SOFR Spread", definition=_spread_definition(), expansion=ExpansionSettings(), enabled=False,
    )
    s = StrategySet(name="Churning", entries=(_fly_entry(), disabled_entry))
    result = expand_strategy_set(s, _START, _END, only_enabled=False)
    expected = generate_candidates(_fly_definition(), _START, _END) + generate_candidates(_spread_definition(), _START, _END)
    assert len(result) == len(expected)


def test_expand_all_disabled_returns_empty_list():
    disabled = StrategySetEntry(name="SOFR Fly", definition=_fly_definition(), expansion=ExpansionSettings(), enabled=False)
    s = StrategySet(name="Churning", entries=(disabled,))
    assert expand_strategy_set(s, _START, _END) == []


def test_expand_per_entry_max_curve_position_passes_through():
    unrestricted = expand_strategy_set(StrategySet(name="A", entries=(_fly_entry(),)), _START, _END)
    restricted_entry = _fly_entry(max_curve_position=2)
    restricted = expand_strategy_set(StrategySet(name="B", entries=(restricted_entry,)), _START, _END)
    assert len(restricted) < len(unrestricted)
    assert restricted == generate_candidates(_fly_definition(), _START, _END, max_curve_position=2)


def test_expand_per_entry_eligible_rics_passes_through():
    eligible = {"SRAH26", "SRAM26", "SRAU26"}
    entry = _fly_entry(eligible_rics=tuple(eligible))
    s = StrategySet(name="Churning", entries=(entry,))
    result = expand_strategy_set(s, _START, _END)
    assert len(result) == 1
    assert set(result[0].rics) <= eligible


def test_expand_two_entries_can_have_different_curve_position_filters_under_one_shared_window():
    unfiltered_entry = _spread_entry()
    filtered_entry = _fly_entry("SOFR Fly Restricted", max_curve_position=2)
    s = StrategySet(name="Churning", entries=(unfiltered_entry, filtered_entry))
    result = expand_strategy_set(s, _START, _END)
    expected = generate_candidates(_spread_definition(), _START, _END) + generate_candidates(
        _fly_definition(), _START, _END, max_curve_position=2
    )
    assert len(result) == len(expected)


def test_expand_same_set_reusable_across_different_windows():
    # The whole point of moving contract_start/contract_end to a
    # call-time argument: the same saved StrategySet expands
    # differently depending on the window supplied "now", with no
    # need to edit the set itself.
    s = StrategySet(name="Churning", entries=(_fly_entry(),))
    narrow = expand_strategy_set(s, "2026-01-01", "2026-06-30")
    wide = expand_strategy_set(s, _START, _END)
    assert narrow == []
    assert len(wide) == 6


def test_expand_multi_market_set_rolls_each_entry_on_its_own_curve():
    s = StrategySet(name="Intermarket Churning", entries=(_fly_entry(), _sonia_entry()))
    result = expand_strategy_set(s, _START, _END)
    market_keys = {inst.definition.market_key for inst in result}
    assert market_keys == {"SOFR", "SONIA"}
    sofr_rics = [ric for inst in result if inst.definition.market_key == "SOFR" for ric in inst.rics]
    sonia_rics = [ric for inst in result if inst.definition.market_key == "SONIA" for ric in inst.rics]
    assert all(ric.startswith("SRA") for ric in sofr_rics)
    assert all(ric.startswith("SON") for ric in sonia_rics)


def test_expand_insufficient_contracts_returns_empty_list_not_error():
    # Only 2 quarterly SOFR contracts are listed in this narrow window --
    # not enough to fill a 3-leg fly's span, mirroring generate_instances()'s
    # own documented "empty, not an error" behaviour.
    s = StrategySet(name="Churning", entries=(_fly_entry(),))
    assert expand_strategy_set(s, "2026-01-01", "2026-06-30") == []


def test_scanner_module_remains_unaware_of_strategy_sets():
    """Design principle: the scanner never imports strategy_sets --
    checked by inspecting the actual source of template_scanner.scanner
    (already imported above), not by a superficial string search over
    some other copy of the file.
    """
    source = inspect.getsource(template_scanner.scanner)
    assert "strategy_sets" not in source
