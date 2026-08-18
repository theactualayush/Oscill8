"""
strategy_import_formatting.py

Pure, Streamlit-free formatting helpers for the Import Strategies panel
(ui.strategy_import_view) -- market breakdown lines, per-candidate
summary lines, invalid/unavailable row lines, and the post-import
summary. Mirrors ui.formatting/ui.strategy_set_formatting's own
convention: zero Streamlit import, directly unit-testable against
plain strategy_import objects, so the panel's actual text content is
covered by fast tests rather than only by slower AppTest rendering
checks.
"""

from __future__ import annotations

from strategy_import.commit import ImportSummary
from strategy_import.market_mapping import SUPPORTED_MARKET_CODES, UNAVAILABLE_MARKET_CODES
from strategy_import.preview import ImportCandidate, ImportPreview

# Reverse of SUPPORTED_MARKET_CODES (market_key -> code) -- used only to
# label a READY row's market by the trader's own workbook code (e.g.
# "SRA") rather than Oscill8's internal registry key ("SOFR") in the
# breakdown line, matching the product brief's worked example ("SRA ✓").
_CODE_BY_MARKET_KEY = {market_key: code for code, market_key in SUPPORTED_MARKET_CODES.items()}


def market_breakdown_lines(preview: ImportPreview) -> list[str]:
    """One line per market code seen anywhere in the upload: "SRA ✓" for
    a market with at least one ready row, "ER ⚠ <reason>" for a
    recognized-but-unavailable market -- supported codes first
    (alphabetical), then unavailable codes (alphabetical), matching the
    product brief's worked example.
    """
    ready_codes: set[str] = set()
    for candidate in preview.candidates:
        for row in candidate.ready:
            code = _CODE_BY_MARKET_KEY.get(row.entry.definition.market_key)
            if code:
                ready_codes.add(code)

    lines = [f"{code} ✓" for code in sorted(ready_codes)]
    lines += [
        f"{code} ⚠ {UNAVAILABLE_MARKET_CODES[code]}" for code in sorted(preview.unavailable_by_market)
    ]
    return lines


def candidate_summary_line(candidate: ImportCandidate) -> str:
    """e.g. "84 strategies (76 ready, 8 unavailable)" -- omits a zero
    count entirely rather than showing "0 invalid" noise."""
    parts = [f"{candidate.total} strateg{'y' if candidate.total == 1 else 'ies'}"]
    detail = []
    if candidate.ready:
        detail.append(f"{len(candidate.ready)} ready")
    if candidate.unavailable:
        detail.append(f"{len(candidate.unavailable)} unavailable")
    if candidate.invalid:
        detail.append(f"{len(candidate.invalid)} invalid")
    if detail:
        parts.append(f"({', '.join(detail)})")
    return " ".join(parts)


def invalid_row_lines(candidate: ImportCandidate) -> list[str]:
    """One line per invalid row -- row number, label, and reason, never
    silently omitted (see strategy_import.validation.InvalidRow)."""
    return [f"Row {row.row_number} — {row.label} — {row.message}" for row in candidate.invalid]


def unavailable_row_lines(candidate: ImportCandidate) -> list[str]:
    """One line per unavailable row -- row number, label, and the exact
    reason (see strategy_import.market_mapping.UNAVAILABLE_MARKET_CODES)."""
    return [f"Row {row.row_number} — {row.label} — {row.reason}" for row in candidate.unavailable]


def import_summary_lines(summary: ImportSummary) -> list[str]:
    """The post-import confirmation screen's four headline lines."""
    return [
        f"Strategy Sets created: {len(summary.created_set_names)}",
        f"Strategies imported: {summary.strategies_imported}",
        f"Unavailable: {summary.unavailable_count}",
        f"Invalid: {summary.invalid_count}",
    ]


__all__ = [
    "market_breakdown_lines",
    "candidate_summary_line",
    "invalid_row_lines",
    "unavailable_row_lines",
    "import_summary_lines",
]
