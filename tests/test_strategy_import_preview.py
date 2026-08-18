"""
tests/test_strategy_import_preview.py

strategy_import.preview.build_preview(): grouping validated rows into
per-sheet ImportCandidates and a whole-upload ImportPreview -- totals,
the ER-style unavailable-by-market breakdown, de-duplicated import
names, strategy-identity deduplication (StrategyDefinition, never the
Label), and sheet-level blocking errors (bad header, invalid sheet
name) that never silently swallow a sheet's individually-classified
rows.
"""

from __future__ import annotations

from strategy_import.parsing import SheetFrame
from strategy_import.preview import build_preview


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
# Matches the product brief's worked example:
#   Total strategies: 200 / Ready: 180 / Unavailable: 20 / ER breakdown
# ---------------------------------------------------------------------

def test_totals_match_ready_plus_unavailable_plus_invalid():
    sheet = _sheet(
        "EZ8 GENERAL MEDIUM VOL",
        [
            _row("SRA", "A", 1, -1, 0),
            _row("SON", "B", 1, -2, 1),
            _row("ER", "C", 1, -1, 0),
            _row("XYZ", "D", 1, -1, 0),
        ],
    )
    preview = build_preview([sheet], _never_exists)
    assert preview.total_strategies == 4
    assert preview.ready_count == 2
    assert preview.unavailable_count == 1
    assert preview.invalid_count == 1


def test_unavailable_by_market_breakdown():
    sheet = _sheet(
        "6mo Spreads",
        [_row("ER", "A", 1, -1, 0), _row("ER", "B", 1, -2, 1), _row("SRA", "C", 1, -1, 0)],
    )
    preview = build_preview([sheet], _never_exists)
    assert preview.unavailable_by_market == {"ER": 2}


def test_er_rows_are_never_silently_dropped_from_the_candidate():
    sheet = _sheet("Set", [_row("ER", "Euribor Strategy", 1, -1, 0)])
    preview = build_preview([sheet], _never_exists)
    candidate = preview.candidates[0]
    assert len(candidate.unavailable) == 1
    assert candidate.unavailable[0].label == "Euribor Strategy"
    assert candidate.unavailable[0].market_code == "ER"


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


def test_sheet_with_zero_ready_rows_is_not_importable_without_being_an_error():
    sheet = _sheet("All Unavailable", [_row("ER", "A", 1, -1, 0)])
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
