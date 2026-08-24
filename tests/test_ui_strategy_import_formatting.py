"""
tests/test_ui_strategy_import_formatting.py

Pure, Streamlit-free tests for ui/strategy_import_formatting.py's text-
content helpers -- market breakdown lines, per-candidate summary lines,
invalid/unavailable row lines, and the post-import summary. Same
convention as tests/test_ui_formatting.py / tests/test_ui_strategy_set_
formatting.py: plain data in, plain strings out, no Streamlit rendering
involved.
"""

from __future__ import annotations

import pytest

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition

from strategy_import.commit import ImportSummary
from strategy_import.market_mapping import UNAVAILABLE_MARKET_CODES
from strategy_import.preview import ImportCandidate
from strategy_import.validation import DEFAULT_IMPORT_INTERVAL, InvalidRow, ReadyRow, UnavailableRow

from strategy_sets.model import StrategySetEntry

from ui.strategy_import_formatting import (
    candidate_summary_line,
    import_summary_lines,
    invalid_row_lines,
    market_breakdown_lines,
    unavailable_row_lines,
)

from strategy_import.preview import ImportPreview


@pytest.fixture
def synthetic_unavailable_markets(monkeypatch):
    """market_breakdown_lines() looks up each unavailable code's reason
    text via UNAVAILABLE_MARKET_CODES (see that function's own
    docstring/investigation) -- ER/YBA/FSR are now genuinely SUPPORTED,
    so tests exercising this code path use clearly-synthetic codes
    registered ONLY here, never in the real, committed dict.
    """
    codes = {
        "ZZZ": "ZZZ is not currently configured in Oscill8.",
        "ZZY": "ZZY market data is not currently configured in Oscill8.",
        "ZZX": "ZZX market data is not currently configured in Oscill8.",
    }
    for code, reason in codes.items():
        monkeypatch.setitem(UNAVAILABLE_MARKET_CODES, code, reason)
    return codes


def _ready(market_key: str, label: str, row_number: int = 2) -> ReadyRow:
    definition = StrategyDefinition(
        market_key=market_key, offsets=(0,), weights=(1.0,), interval=DEFAULT_IMPORT_INTERVAL,
    )
    return ReadyRow(row_number=row_number, entry=StrategySetEntry(name=label, definition=definition))


def _candidate(name, ready=(), unavailable=(), invalid=(), import_name=None, sheet_error=None) -> ImportCandidate:
    return ImportCandidate(
        sheet_name=name,
        import_name=import_name if import_name is not None else (name if sheet_error is None else None),
        ready=tuple(ready),
        unavailable=tuple(unavailable),
        invalid=tuple(invalid),
        sheet_error=sheet_error,
    )


# ---------------------------------------------------------------------
# market_breakdown_lines
# ---------------------------------------------------------------------

def test_breakdown_shows_ready_markets_with_checkmark():
    candidate = _candidate("Set", ready=[_ready("SOFR", "A"), _ready("SONIA", "B")])
    preview = ImportPreview(candidates=(candidate,))

    lines = market_breakdown_lines(preview)

    assert "SRA ✓" in lines
    assert "SON ✓" in lines


def test_breakdown_shows_synthetic_unavailable_market_with_reason(synthetic_unavailable_markets):
    unavailable = UnavailableRow(row_number=4, label="Synthetic Trade", market_code="ZZZ", reason=synthetic_unavailable_markets["ZZZ"])
    candidate = _candidate("Set", unavailable=[unavailable])
    preview = ImportPreview(candidates=(candidate,))

    lines = market_breakdown_lines(preview)

    assert lines == [f"ZZZ ⚠ {synthetic_unavailable_markets['ZZZ']}"]


def test_breakdown_matches_the_product_brief_worked_example(synthetic_unavailable_markets):
    candidate = _candidate(
        "Set",
        ready=[_ready("SOFR", "A"), _ready("SONIA", "B"), _ready("CORRA", "C")],
        unavailable=[UnavailableRow(5, "D", "ZZZ", synthetic_unavailable_markets["ZZZ"])],
    )
    preview = ImportPreview(candidates=(candidate,))

    lines = market_breakdown_lines(preview)

    assert lines == ["CRA ✓", "SON ✓", "SRA ✓", f"ZZZ ⚠ {synthetic_unavailable_markets['ZZZ']}"]


def test_breakdown_empty_preview_is_empty():
    assert market_breakdown_lines(ImportPreview(candidates=())) == []


def test_breakdown_shows_two_unavailable_markets_with_distinct_reasons(synthetic_unavailable_markets):
    candidate = _candidate(
        "Set",
        unavailable=[
            UnavailableRow(38, "3M Spread", "ZZY", synthetic_unavailable_markets["ZZY"]),
            UnavailableRow(43, "3M Spread", "ZZX", synthetic_unavailable_markets["ZZX"]),
        ],
    )
    preview = ImportPreview(candidates=(candidate,))

    lines = market_breakdown_lines(preview)

    assert f"ZZX ⚠ {synthetic_unavailable_markets['ZZX']}" in lines
    assert f"ZZY ⚠ {synthetic_unavailable_markets['ZZY']}" in lines


def test_breakdown_with_three_unavailable_markets_all_present_shows_each_distinctly(synthetic_unavailable_markets):
    candidate = _candidate(
        "Set",
        ready=[_ready("SOFR", "A"), _ready("SONIA", "B"), _ready("CORRA", "C")],
        unavailable=[
            UnavailableRow(5, "D", "ZZZ", synthetic_unavailable_markets["ZZZ"]),
            UnavailableRow(38, "E", "ZZY", synthetic_unavailable_markets["ZZY"]),
            UnavailableRow(43, "F", "ZZX", synthetic_unavailable_markets["ZZX"]),
        ],
    )
    preview = ImportPreview(candidates=(candidate,))

    lines = market_breakdown_lines(preview)

    assert lines == [
        "CRA ✓", "SON ✓", "SRA ✓",
        f"ZZX ⚠ {synthetic_unavailable_markets['ZZX']}",
        f"ZZY ⚠ {synthetic_unavailable_markets['ZZY']}",
        f"ZZZ ⚠ {synthetic_unavailable_markets['ZZZ']}",
    ]


# ---------------------------------------------------------------------
# candidate_summary_line
# ---------------------------------------------------------------------

def test_candidate_summary_line_all_ready():
    candidate = _candidate("Set", ready=[_ready("SOFR", "A"), _ready("SOFR", "B")])
    assert candidate_summary_line(candidate) == "2 strategies (2 ready)"


def test_candidate_summary_line_mixed_counts():
    candidate = _candidate(
        "Set",
        ready=[_ready("SOFR", "A")],
        unavailable=[UnavailableRow(3, "B", "ER", "reason")],
        invalid=[InvalidRow(4, "C", "bad")],
    )
    assert candidate_summary_line(candidate) == "3 strategies (1 ready, 1 unavailable, 1 invalid)"


def test_candidate_summary_line_singular_strategy():
    candidate = _candidate("Set", ready=[_ready("SOFR", "A")])
    assert candidate_summary_line(candidate) == "1 strategy (1 ready)"


def test_candidate_summary_line_omits_zero_categories():
    candidate = _candidate("Set", ready=[_ready("SOFR", "A")])
    line = candidate_summary_line(candidate)
    assert "unavailable" not in line
    assert "invalid" not in line


# ---------------------------------------------------------------------
# invalid_row_lines / unavailable_row_lines -- row number, label, reason
# ---------------------------------------------------------------------

def test_invalid_row_lines_never_omit_row_number_label_or_reason():
    candidate = _candidate("Set", invalid=[InvalidRow(17, "Bad Strategy", "invalid position structure")])
    lines = invalid_row_lines(candidate)
    assert lines == ["Row 17 — Bad Strategy — invalid position structure"]


def test_unavailable_row_lines_never_omit_row_number_label_or_reason():
    candidate = _candidate(
        "Set", unavailable=[UnavailableRow(4, "Euribor Trade", "ER", "Euribor is not currently configured in Oscill8.")]
    )
    lines = unavailable_row_lines(candidate)
    assert lines == ["Row 4 — Euribor Trade — Euribor is not currently configured in Oscill8."]


def test_multiple_invalid_rows_all_appear():
    candidate = _candidate(
        "Set",
        invalid=[
            InvalidRow(17, "A", "invalid position structure"),
            InvalidRow(31, "B", "unknown market 'XYZ'"),
            InvalidRow(42, "C", "position contains a non-numeric value"),
        ],
    )
    lines = invalid_row_lines(candidate)
    assert len(lines) == 3
    assert all(str(row.row_number) in line for row, line in zip(candidate.invalid, lines))


# ---------------------------------------------------------------------
# import_summary_lines
# ---------------------------------------------------------------------

def test_import_summary_lines():
    summary = ImportSummary(
        created_set_names=("A", "B"), strategies_imported=180, unavailable_count=20, invalid_count=3,
    )
    lines = import_summary_lines(summary)
    assert lines == [
        "Strategy Sets created: 2",
        "Strategies imported: 180",
        "Unavailable: 20",
        "Invalid: 3",
    ]
