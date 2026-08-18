"""
tests/test_strategy_import_dedup.py

Regression coverage for the real-workbook finding that a Strategy
Label is NOT a unique identifier: strategy identity for import
deduplication is the resulting StrategyDefinition (market + offsets +
weights), never the Label. Exercised through the full parse -> preview
pipeline (strategy_import.parsing.parse_csv/parse_workbook ->
strategy_import.preview.build_preview), not just hand-built SheetFrames
-- see tests/test_strategy_import_preview.py for the lower-level
unit coverage of the same rule.
"""

from __future__ import annotations

from strategy_import.preview import build_preview
from strategy_import.parsing import parse_csv


def _never_exists(name: str) -> bool:
    return False


def _preview_for(csv_text: str, filename: str = "strategies.csv"):
    sheet = parse_csv(csv_text.encode("utf-8"), filename)
    return build_preview([sheet], _never_exists)


# ---------------------------------------------------------------------
# same label + different markets -> both accepted
# ---------------------------------------------------------------------

def test_same_label_across_markets_both_accepted():
    csv = (
        "Market,Label,1,2,3\n"
        "ER,1Yr Fly,1,-2,1\n"
        "SRA,1Yr Fly,1,-2,1\n"
    )
    preview = _preview_for(csv)
    candidate = preview.candidates[0]

    # ER is unavailable (recognized, not configured) -- SRA is ready.
    assert len(candidate.ready) == 1
    assert len(candidate.unavailable) == 1
    assert candidate.ready[0].entry.definition.market_key == "SOFR"
    assert candidate.ready[0].entry.name == "1Yr Fly"
    assert candidate.unavailable[0].market_code == "ER"


def test_same_label_two_ready_markets_both_accepted_and_disambiguated():
    csv = (
        "Market,Label,1,2,3\n"
        "SRA,1Yr Fly,1,-2,1\n"
        "SON,1Yr Fly,1,-2,1\n"
    )
    preview = _preview_for(csv)
    candidate = preview.candidates[0]

    assert candidate.importable is True
    assert len(candidate.ready) == 2
    markets = {row.entry.definition.market_key for row in candidate.ready}
    assert markets == {"SOFR", "SONIA"}
    names = sorted(row.entry.name for row in candidate.ready)
    assert names == ["1Yr Fly", "1Yr Fly 2"]


# ---------------------------------------------------------------------
# same label + same market + different structure -> both accepted
# ---------------------------------------------------------------------

def test_same_label_same_market_different_structure_both_accepted():
    csv = (
        "Market,Label,1,2,3,4\n"
        "SRA,Churn,1,-1,,\n"
        "SRA,Churn,1,0,-1,\n"
    )
    preview = _preview_for(csv)
    candidate = preview.candidates[0]

    assert candidate.importable is True
    assert len(candidate.ready) == 2
    offsets = {row.entry.definition.offsets for row in candidate.ready}
    assert offsets == {(0, 1), (0, 2)}


# ---------------------------------------------------------------------
# same label + same market + identical structure -> duplicate (one entry)
# ---------------------------------------------------------------------

def test_same_label_same_market_identical_structure_is_one_strategy():
    csv = (
        "Market,Label,1,2,3\n"
        "SRA,3M Spread,1,-1,\n"
        "SRA,3M Spread,1,-1,\n"
    )
    preview = _preview_for(csv)
    candidate = preview.candidates[0]

    assert len(candidate.ready) == 1
    assert candidate.ready[0].entry.name == "3M Spread"


# ---------------------------------------------------------------------
# blank vs 0 -> identical strategy, deduplicates
# ---------------------------------------------------------------------

def test_blank_vs_explicit_zero_position_is_the_same_strategy():
    # SRA | 1Yr Fly | 1 | -2 | blank | 1  ==  SRA | 1Yr Fly | 1 | -2 | 0 | 1
    csv = (
        "Market,Label,1,2,3,4\n"
        "SRA,1Yr Fly,1,-2,,1\n"
        "SRA,1Yr Fly,1,-2,0,1\n"
    )
    preview = _preview_for(csv)
    candidate = preview.candidates[0]

    assert len(candidate.ready) == 1
    definition = candidate.ready[0].entry.definition
    assert definition.offsets == (0, 1, 3)
    assert definition.weights == (1.0, -2.0, 1.0)


def test_blank_vs_zero_is_not_a_two_strategy_result_even_with_other_rows_present():
    csv = (
        "Market,Label,1,2,3,4\n"
        "SRA,1Yr Fly,1,-2,,1\n"
        "SRA,1Yr Fly,1,-2,0,1\n"
        "SON,6M Spread,1,-1,,\n"
    )
    preview = _preview_for(csv)
    candidate = preview.candidates[0]

    # 2 raw rows for "1Yr Fly" collapse to 1 + the unrelated SONIA row = 2 total ready.
    assert len(candidate.ready) == 2
    names = sorted(row.entry.name for row in candidate.ready)
    assert names == ["1Yr Fly", "6M Spread"]


# ---------------------------------------------------------------------
# A workbook shaped like the real RBS Template (repeated labels across
# markets, including ER rows) must no longer produce zero strategies.
# ---------------------------------------------------------------------

def test_rbs_template_shaped_sheet_no_longer_produces_zero_strategies():
    # Mirrors the real-world pattern that surfaced this bug: the same
    # trader-facing Labels ("1Yr Fly", "Churn", "6M Spread") repeated
    # across SRA/SON/CRA/ER rows. Before this fix, ANY repeated Label
    # within one sheet made the whole sheet's ready set unimportable
    # (StrategySet's own duplicate-entry-name validation firing on the
    # Label) -- so a realistically-shaped multi-market template
    # produced a sheet_error and 0 ready strategies, even though every
    # individual row was perfectly valid.
    csv = (
        "Market,Label,1,2,3\n"
        "SRA,1Yr Fly,1,-2,1\n"
        "SON,1Yr Fly,1,-2,1\n"
        "CRA,1Yr Fly,1,-2,1\n"
        "ER,1Yr Fly,1,-2,1\n"
        "SRA,Churn,1,-1,\n"
        "SON,Churn,1,-1,\n"
        "CRA,Churn,1,-1,\n"
        "SRA,6M Spread,1,0,-1\n"
        "SON,6M Spread,1,0,-1\n"
    )
    preview = _preview_for(csv, filename="RBS_Strategies.csv")
    candidate = preview.candidates[0]

    assert candidate.sheet_error is None
    assert candidate.importable is True
    assert len(candidate.ready) > 0
    # 9 rows total; the ER row is unavailable (not ready), leaving 8
    # ready rows, each a distinct (market, structure) pair.
    assert len(candidate.ready) == 8
    assert len(candidate.unavailable) == 1
    # No two ready entries share a name -- StrategySet's own invariant
    # is satisfied without any row being dropped.
    names = [row.entry.name for row in candidate.ready]
    assert len(names) == len(set(names))
