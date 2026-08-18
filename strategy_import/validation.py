"""
validation.py

Classifies one already-parsed row (see strategy_import/parsing.py) into
exactly one of three outcomes -- ReadyRow, UnavailableRow, or
InvalidRow -- never silently dropping a row and never conflating an
unavailable market (recognized, not yet configured -- e.g. "ER") with
a genuinely malformed row (bad market code, bad position value). This
three-way split is a direct product requirement: an ER row must be
visibly reported as "unavailable", never merged into "invalid" or
"ready", and never simply absent from the count.

Reuses, never duplicates, the project's existing shape validation:
template_scanner.templates.template_from_dense_weights() (the same
dense-weight -> StrategyDefinition translator the manual strategy grid
uses, via ui.formatting.build_definitions_from_grid) is called directly
here. strategy_import intentionally does NOT import from ui.* --
ui.formatting is a UI-layer helper (grid-cell/TextColumn conventions);
depending on it from this package would invert the project's layering
(ui depends on domain packages, not the other way round). Calling
template_from_dense_weights()/StrategyDefinition directly keeps
strategy_import at the same layer as template_scanner itself.

Position-cell parsing is deliberately STRICTER than the manual grid's
_cell_to_float (ui/formatting.py): the grid treats an unparseable cell
as 0 (silently), because it's a live, actively-edited widget where a
mid-typed "-" is a normal transient state. An imported spreadsheet cell
has no "mid-edit" state -- a cell that isn't blank and isn't a valid
number is a genuine data problem in the source file, so it is reported
as an InvalidRow, never coerced to 0. Losing a leg's weight to a silent
0 would silently change the strategy's shape, which the "no invalid row
may be silently dropped" requirement rules out.

No interval column is read from the row -- see parsing.py's module
docstring. Every imported entry gets DEFAULT_IMPORT_INTERVAL, exactly
as if it were a freshly-typed manual grid row before the user touched
that row's own Interval cell; this matches today's StrategySetEntry
model, where interval lives per-entry (see strategy_sets/model.py) and
there is no set-level "run at this interval" override yet. Introducing
a true set-level runtime interval selector is a materially different,
NOT-implemented feature (it would need a change to strategy_sets/
expansion.py's or the scanner's execution path, out of scope for this
import feature) -- this default only avoids blocking import on a
concept that doesn't exist in the persisted model yet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from core.config import BarInterval

from template_scanner.templates import template_from_dense_weights

from strategy_sets.model import StrategySetEntry

from strategy_import.market_mapping import resolve_market_code
from strategy_import.parsing import LABEL_COLUMN, MARKET_COLUMN

# See the module docstring's "No interval column is read" note.
DEFAULT_IMPORT_INTERVAL: BarInterval = BarInterval.DAILY


@dataclass(frozen=True)
class ReadyRow:
    """A row that resolved to a fully valid StrategySetEntry."""

    row_number: int
    entry: StrategySetEntry


@dataclass(frozen=True)
class UnavailableRow:
    """A row whose market is recognized by name but has no configured
    core.config.MARKETS entry -- e.g. ER/Euribor. Never persisted, but
    always reported (row number, label, market code, and the exact
    reason) -- see market_mapping.UNAVAILABLE_MARKET_CODES."""

    row_number: int
    label: str
    market_code: str
    reason: str


@dataclass(frozen=True)
class InvalidRow:
    """A row that could not be turned into a strategy at all -- unknown
    market code, non-numeric position value, or a position pattern
    StrategyDefinition itself rejects (e.g. all-zero weights). Never
    persisted."""

    row_number: int
    label: str
    message: str


RowOutcome = ReadyRow | UnavailableRow | InvalidRow


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _parse_position_cell(value: object) -> float | None:
    """A position cell's numeric value, 0.0 for blank/NaN, or None if the
    cell holds a non-blank value that isn't a valid number -- the None
    case is the caller's signal to raise an InvalidRow rather than
    silently treat the leg as absent."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 0.0 if isinstance(value, float) and math.isnan(value) else float(value)
    text = str(value).strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def validate_row(
    row: dict,
    row_number: int,
    position_columns: Sequence[str],
    interval: BarInterval = DEFAULT_IMPORT_INTERVAL,
) -> RowOutcome:
    """Classify one parsed row into a ReadyRow, UnavailableRow, or
    InvalidRow. `row` is one entry from SheetFrame.rows (already blank-
    row-filtered by parsing.py); `row_number` is that row's 1-based
    spreadsheet row number, used only for user-facing messages.
    """
    label = _clean_text(row.get(LABEL_COLUMN)) or f"Row {row_number}"
    market_text = _clean_text(row.get(MARKET_COLUMN))

    if not market_text:
        return InvalidRow(row_number, label, "Missing Market value")

    resolution = resolve_market_code(market_text)

    if resolution.status == "unrecognized":
        return InvalidRow(row_number, label, f"Unknown market '{resolution.code}'")

    if resolution.status == "unavailable":
        return UnavailableRow(row_number, label, resolution.code, resolution.reason)

    dense_weights: list[float] = []
    for col in position_columns:
        parsed = _parse_position_cell(row.get(col))
        if parsed is None:
            return InvalidRow(
                row_number, label, f"Position '{col}' contains a non-numeric value: {row.get(col)!r}"
            )
        dense_weights.append(parsed)

    try:
        definition = template_from_dense_weights(resolution.market_key, dense_weights, interval)
    except ValueError as exc:
        return InvalidRow(row_number, label, str(exc))

    return ReadyRow(row_number, StrategySetEntry(name=label, definition=definition))


__all__ = [
    "ReadyRow",
    "UnavailableRow",
    "InvalidRow",
    "RowOutcome",
    "DEFAULT_IMPORT_INTERVAL",
    "validate_row",
]
