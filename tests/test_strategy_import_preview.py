"""
tests/test_strategy_import_preview.py

strategy_import.preview.build_preview(): grouping validated rows into
per-sheet ImportCandidates and a whole-upload ImportPreview -- totals,
the unavailable-by-market breakdown, de-duplicated import names,
strategy-identity deduplication (StrategyDefinition, never the Label),
and sheet-level blocking errors (bad header, invalid sheet name) that
never silently swallow a sheet's individually-classified rows.

Unavailable-market examples use a clearly-synthetic "ZZZ" code
(registered only via the synthetic_unavailable_market fixture, never in
the real, committed strategy_import.market_mapping.UNAVAILABLE_MARKET_
CODES) -- ER/YBA/FSR are now genuinely SUPPORTED (EURIBOR/YBA/SARON
gained core.config.MARKETS entries), so they can no longer illustrate
the unavailable-row code path.
"""

from __future__ import annotations

import pytest

from strategy_import.market_mapping import UNAVAILABLE_MARKET_CODES
from strategy_import.parsing import SheetFrame
from strategy_import.preview import build_preview


@pytest.fixture
def synthetic_unavailable_market(monkeypatch):
    monkeypatch.setitem(UNAVAILABLE_MARKET_CODES, "ZZZ", "ZZZ is not currently configured in Oscill8.")
    return "ZZZ"


def _sheet(name, rows, position_columns=("1", "2", "3")) -> SheetFrame:
    row_numbers = list(range(2, 2 + len(rows)))
    return SheetFrame(name=name, position_columns=position_columns, rows=rows, row_numbers=row_numbers)


def _row(market, label, *weights) -> dict:
    row = {"Market": market, "Label": label}
    row.update({str(i + 1): w for i, w in enumerate(weights)})
    return row


def _never_exists(name: str) -> bool:
    return False


# ---------------------------------------------------------------------
# Matches the product brief's worked example shape:
#   Total strategies: N / Ready: N-k / Unavailable: k / breakdown by market
# ---------------------------------------------------------------------

def test_totals_match_ready_plus_unavailable_plus_invalid(synthetic_unavailable_market):
    sheet = _sheet(
        "EZ8 GENERAL MEDIUM VOL",
        [
            _row("SRA", "A", 1, -1, 0),
            _row("SON", "B", 1, -2, 1),
            _row("ZZZ", "C", 1, -1, 0),
            _row("XYZ", "D", 1, -1, 0),
        ],
    )
    preview = build_preview([sheet], _never_exists)
    assert preview.total_strategies == 4
    assert preview.ready_count == 2
    assert preview.unavailable_count == 1
    assert preview.invalid_count == 1


def test_unavailable_by_market_breakdown(synthetic_unavailable_market):
    sheet = _sheet(
        "6mo Spreads",
        [_row("ZZZ", "A", 1, -1, 0), _row("ZZZ", "B", 1, -2, 1), _row("SRA", "C", 1, -1, 0)],
    )
    preview = build_preview([sheet], _never_exists)
    assert preview.unavailable_by_market == {"ZZZ": 2}


def test_unavailable_rows_are_never_silently_dropped_from_the_candidate(synthetic_unavailable_market):
    sheet = _sheet("Set", [_row("ZZZ", "Synthetic Strategy", 1, -1, 0)])
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]
    assert len(candidate.unavailable) == 1
    assert candidate.unavailable[0].label == "Synthetic Strategy"
    assert candidate.unavailable[0].market_code == "ZZZ"


def test_er_yba_fsr_now_produce_ready_rows_not_unavailable():
    # Direct regression lock for the production mapping change.
    sheet = _sheet("Set", [_row("ER", "A", 1, -1, 0), _row("YBA", "B", 1, -1, 0), _row("FSR", "C", 1, -1, 0)])
    preview = build_preview([sheet], _never_exists)
    assert preview.ready_count == 3
    assert preview.unavailable_count == 0


# ---------------------------------------------------------------------
# Import naming: never overwrite, "Name", "Name 2", "Name 3"
# ---------------------------------------------------------------------

def test_import_name_matches_sheet_name_when_unused():
    sheet = _sheet("Churning", [_row("SRA", "A", 1, -1, 0)])
    preview = build_preview([sheet], _never_exists)
    assert preview.candidates[0].import_name == "Churning"


def test_import_name_is_deduplicated_against_existing_sets():
    existing = {"Churning"}
    sheet = _sheet("Churning", [_row("SRA", "A", 1, -1, 0)])
    preview = build_preview([sheet], existing.__contains__)
    assert preview.candidates[0].import_name == "Churning 2"


def test_existing_set_is_never_referenced_as_overwritten():
    # build_preview only ever READS name_exists -- it must not itself
    # attempt any write. A spy that raises on write-shaped calls isn't
    # meaningful here (name_exists is a pure predicate); this test
    # instead locks in that the resolved name is strictly different
    # from every existing name, which is the observable guarantee.
    existing = {"Set", "Set 2", "Set 3"}
    sheet = _sheet("Set", [_row("SRA", "A", 1, -1, 0)])
    preview = build_preview([sheet], existing.__contains__)
    assert preview.candidates[0].import_name not in existing
    assert preview.candidates[0].import_name == "Set 4"


# ---------------------------------------------------------------------
# Sheet-level blocking errors
# ---------------------------------------------------------------------

def test_sheet_parse_error_is_not_importable_but_is_still_reported():
    sheet = SheetFrame(name="Bad Sheet", position_columns=(), rows=[], row_numbers=[], parse_error="Missing required column(s): Market")
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]
    assert candidate.importable is False
    assert candidate.sheet_error == "Missing required column(s): Market"
    assert candidate.sheet_name == "Bad Sheet"


def test_invalid_sheet_name_is_a_sheet_error_not_auto_sanitized():
    # '&' is not in StrategySet's own name pattern (letters, digits,
    # spaces, '-', '_' only) -- per product decision, this must be
    # reported for the user to rename, never silently sanitized.
    sheet = _sheet("EZ8 & Friends", [_row("SRA", "A", 1, -1, 0)])
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]
    assert candidate.importable is False
    assert candidate.sheet_error is not None
    assert candidate.import_name is None
    assert candidate.sheet_name == "EZ8 & Friends"  # original name preserved verbatim


def test_same_label_different_markets_are_both_accepted_and_disambiguated():
    # Real-workbook finding: a Label is not an identifier -- "1Yr Fly"
    # legitimately recurs across markets. Both must import; since the
    # Label collides, the surviving entries get disambiguated names.
    sheet = _sheet(
        "Set", [_row("SRA", "Same Name", 1, -1, 0), _row("SON", "Same Name", 1, -2, 1)]
    )
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]

    assert candidate.importable is True
    assert candidate.sheet_error is None
    assert len(candidate.ready) == 2
    names = [row.entry.name for row in candidate.ready]
    assert names == ["Same Name", "Same Name 2"]
    assert candidate.ready[0].entry.definition.market_key == "SOFR"
    assert candidate.ready[1].entry.definition.market_key == "SONIA"


def test_same_label_same_market_different_structure_are_both_accepted():
    sheet = _sheet(
        "Set", [_row("SRA", "Same Name", 1, -1, 0), _row("SRA", "Same Name", 1, -2, 1)]
    )
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]

    assert candidate.importable is True
    assert len(candidate.ready) == 2
    assert [row.entry.name for row in candidate.ready] == ["Same Name", "Same Name 2"]


def test_same_label_same_market_identical_structure_deduplicates_to_one():
    sheet = _sheet(
        "Set", [_row("SRA", "Same Name", 1, -1, 0), _row("SRA", "Same Name", 1, -1, 0)]
    )
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]

    assert len(candidate.ready) == 1
    assert candidate.ready[0].entry.name == "Same Name"


def test_blank_and_zero_position_cells_deduplicate_as_the_same_strategy():
    # SRA | 1Yr Fly | 1 | -2 | blank | 1  ==  SRA | 1Yr Fly | 1 | -2 | 0 | 1
    blank_row = {"Market": "SRA", "Label": "1Yr Fly", "1": 1, "2": -2, "3": None, "4": 1}
    zero_row = {"Market": "SRA", "Label": "1Yr Fly", "1": 1, "2": -2, "3": 0, "4": 1}
    sheet = _sheet("Set", [blank_row, zero_row], position_columns=("1", "2", "3", "4"))

    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]

    assert len(candidate.ready) == 1
    assert candidate.ready[0].entry.definition.offsets == (0, 1, 3)
    assert candidate.ready[0].entry.definition.weights == (1.0, -2.0, 1.0)


def test_different_weight_at_same_offset_is_not_deduplicated():
    sheet = _sheet(
        "Set", [_row("SRA", "Fly", 1, -2, 1), _row("SRA", "Fly", 2, -4, 2)]
    )
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]

    # (1, -2, 1) and (2, -4, 2) are the same shape but different
    # economic exposure -- NOT duplicates, matching StrategyDefinition/
    # dedupe_candidates()'s own project-wide convention.
    assert len(candidate.ready) == 2


def test_sheet_with_zero_ready_rows_is_not_importable_without_being_an_error(synthetic_unavailable_market):
    sheet = _sheet("All Unavailable", [_row("ZZZ", "A", 1, -1, 0)])
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]
    assert candidate.importable is False
    assert candidate.sheet_error is None  # not an error -- just nothing to save
    assert candidate.import_name is None


# ---------------------------------------------------------------------
# Multi-sheet workbook independence
# ---------------------------------------------------------------------

def test_one_bad_sheet_does_not_affect_a_good_sheets_importability():
    good = _sheet("Good Set", [_row("SRA", "A", 1, -1, 0)])
    bad = SheetFrame(name="Broken", position_columns=(), rows=[], row_numbers=[], parse_error="Missing required column(s): Label")
    preview = build_preview([good, bad], _never_exists)

    good_candidate = next(c for c in preview.candidates if c.sheet_name == "Good Set")
    bad_candidate = next(c for c in preview.candidates if c.sheet_name == "Broken")
    assert good_candidate.importable is True
    assert bad_candidate.importable is False
    assert preview.importable_candidates == (good_candidate,)
