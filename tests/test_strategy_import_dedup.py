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

from io import BytesIO

import pandas as pd

from strategy_import.preview import build_preview
from strategy_import.parsing import parse_csv, parse_workbook


def _never_exists(name: str) -> bool:
    return False


def _preview_for(csv_text: str, filename: str = "strategies.csv"):
    sheet = parse_csv(csv_text.encode("utf-8"), filename)
    return build_preview([sheet], _never_exists)


def _xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
    return buffer.getvalue()


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


def test_er_yba_fsr_are_all_unavailable_never_invalid_side_by_side():
    # Real-workbook finding: ER, YBA, and FSR are all recognized-but-
    # unavailable markets that appear together in the actual
    # RBS_Template.xlsx. A genuinely unknown code (typo/near-miss) must
    # still be invalid, never swept into "unavailable" alongside them.
    csv = (
        "Market,Label,1,2,3\n"
        "SRA,3M Spread,1,-1,\n"
        "ER,3M Spread,1,-1,\n"
        "YBA,3M Spread,1,-1,\n"
        "FSR,3M Spread,1,-1,\n"
        "ZZZ,3M Spread,1,-1,\n"
    )
    preview = _preview_for(csv, filename="strategies.csv")
    candidate = preview.candidates[0]

    assert len(candidate.ready) == 1
    assert candidate.ready[0].entry.definition.market_key == "SOFR"

    assert len(candidate.unavailable) == 3
    assert {row.market_code for row in candidate.unavailable} == {"ER", "YBA", "FSR"}

    assert len(candidate.invalid) == 1
    assert candidate.invalid[0].message == "Unknown market 'ZZZ'"

    assert preview.unavailable_by_market == {"ER": 1, "YBA": 1, "FSR": 1}


# ---------------------------------------------------------------------
# Real-workbook XLSX regression (parsing.py integer-column-header fix):
# Excel header cells for position columns are typically typed as
# NUMBERS, not text -- pandas then reads df.columns as int, not str.
# Built with bare int dict keys (which round-trip through .to_excel()/
# pd.read_excel() as genuinely int-typed columns), exactly reproducing
# the real RBS_Template.xlsx structure that previously made every row
# in every sheet silently read as blank and get dropped before ever
# reaching validate_row()/build_preview() -- "6 Strategy Set(s)
# detected · 0 strategies detected".
# ---------------------------------------------------------------------

def test_real_workbook_shaped_xlsx_with_integer_headers_produces_strategies():
    sheet = pd.DataFrame(
        {
            "Market": ["SRA", "SON", "CRA", "ER"],
            "Label": ["3M Spread", "3M Spread", "3M Spread", "3M Spread"],
            1: [1, 1, 1, 1],
            2: [-1, -1, -1, -1],
            3: [0, 0, 0, 0],
        }
    )
    frame = parse_workbook(_xlsx_bytes(sheet, "EZ8 GENERAL"))[0]
    preview = build_preview([frame], _never_exists)
    candidate = preview.candidates[0]

    assert candidate.sheet_error is None
    assert len(candidate.ready) == 3  # SRA/SON/CRA
    assert len(candidate.unavailable) == 1  # ER
    assert {r.entry.definition.market_key for r in candidate.ready} == {"SOFR", "SONIA", "CORRA"}


def test_real_workbook_shaped_xlsx_rebasing_dedup_case():
    # Mirrors the actual "6mo Spreads and Flys" worksheet from the real
    # RBS_Template.xlsx: two rows labeled "6M Sprd", raw dense weights
    # shifted by one column ([0,1,0,-1,...] vs [1,0,-1,0,...]), which
    # template_from_dense_weights()'s own leading-zero re-basing already
    # normalizes to the IDENTICAL StrategyDefinition (offsets=(0,2),
    # weights=(1,-1)) -- a genuine duplicate that must collapse to one
    # entry, not two.
    sheet = pd.DataFrame(
        {
            "Market": ["SRA", "SRA"],
            "Label": ["6M Sprd", "6M Sprd"],
            1: [0, 1],
            2: [1, 0],
            3: [0, -1],
            4: [-1, 0],
        }
    )
    frame = parse_workbook(_xlsx_bytes(sheet, "6mo Spreads and Flys"))[0]
    preview = build_preview([frame], _never_exists)
    candidate = preview.candidates[0]

    assert candidate.sheet_error is None
    assert len(candidate.ready) == 1
    entry = candidate.ready[0]
    assert entry.entry.name == "6M Sprd"
    assert entry.entry.definition.offsets == (0, 2)
    assert entry.entry.definition.weights == (1.0, -1.0)


def test_real_workbook_shaped_xlsx_blank_vs_zero_also_dedupes():
    # Same rebasing scenario, but expressed with a genuinely blank
    # (NaN) cell instead of an explicit 0 in one of the two rows --
    # both blank-vs-zero AND rebasing-equivalence must compose
    # correctly, exactly as the real "6M Fly" rows in that worksheet do.
    sheet = pd.DataFrame(
        {
            "Market": ["SRA", "SRA"],
            "Label": ["6M Fly", "6M Fly"],
            1: [0, 1],
            2: [1, None],
            3: [None, -2],
            4: [-2, None],
            5: [None, 1],
            6: [1, None],
        }
    )
    frame = parse_workbook(_xlsx_bytes(sheet, "6mo Spreads and Flys"))[0]
    preview = build_preview([frame], _never_exists)
    candidate = preview.candidates[0]

    assert candidate.sheet_error is None
    assert len(candidate.ready) == 1
    definition = candidate.ready[0].entry.definition
    assert definition.offsets == (0, 2, 4)
    assert definition.weights == (1.0, -2.0, 1.0)
