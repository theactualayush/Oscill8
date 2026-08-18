"""
parsing.py

Pure Excel/CSV -> SheetFrame parsing. No market-code resolution, no
StrategyDefinition/StrategySet construction, no filesystem access
beyond reading the bytes handed in -- see strategy_import/validation.py
for row-level classification and strategy_import/preview.py for
grouping SheetFrames into an ImportPreview. This module only knows
about spreadsheet/CSV shape (sheets, header row, columns, blank rows),
never about Oscill8 markets or strategy shapes.

One Excel worksheet == one SheetFrame == one future Strategy Set (per
the product brief: "Each Excel worksheet represents EXACTLY ONE
Strategy Set"). One CSV file == one SheetFrame, named from the
filename (extension stripped) rather than a sheet name.

Expected column shape: a "Market" column, a "Label" column (both
matched case-insensitively -- traders' own headers, not guaranteed to
match Oscill8's exact casing), and one or more curve-position columns
in whatever order they appear, holding the leg's weight (0/blank =
"no leg at this position", exactly like the manual strategy grid's own
dense-weight columns -- see ui.formatting.build_definitions_from_grid's
identical convention, intentionally mirrored here rather than
reinvented). No interval column is expected or read -- per the product
brief, interval is chosen when the imported Strategy Set is run, not
stored in the file; imported entries get a fixed default interval
(see strategy_import/validation.py) exactly like a freshly-added manual
grid row would.

A row where every position column is blank/NaN is dropped here, never
surfaced as an error -- identical to ui.formatting.
build_definitions_from_grid()'s own "an all-zero/blank row ... is
silently skipped, not an error" rule for the manual grid. This keeps a
trailing blank row at the bottom of a worksheet from being reported as
an invalid strategy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO

import pandas as pd

MARKET_COLUMN = "Market"
LABEL_COLUMN = "Label"

_REQUIRED_HEADERS = (MARKET_COLUMN, LABEL_COLUMN)


@dataclass(frozen=True)
class SheetFrame:
    """One worksheet or CSV file, parsed into plain rows -- no market or
    strategy-shape validation performed yet.

    `rows` holds one dict per non-blank data row, each with canonical
    "Market"/"Label" keys (regardless of the original header casing)
    plus one key per entry in `position_columns` (original column names,
    order preserved). `row_numbers[i]` is the 1-based spreadsheet row
    number `rows[i]` came from (accounting for the header row), for
    user-facing error messages that match what the trader sees in
    Excel.

    `parse_error` is set instead of `rows` being trusted when the sheet
    itself doesn't have the expected shape (e.g. no "Market" or "Label"
    column at all) -- a sheet-level problem, distinct from a row-level
    one. `rows`/`row_numbers`/`position_columns` are still present
    (empty) in that case so callers don't need to null-check them.
    """

    name: str
    position_columns: tuple[str, ...]
    rows: list[dict]
    row_numbers: list[int]
    parse_error: str | None = None


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() == ""


def _find_column(columns: list, target: str):
    """Case-insensitive lookup of `target` among `columns` -- the
    DataFrame's own, UN-stringified column labels (which pandas may
    have typed as `str`, `int`, or `float` depending on how the source
    cell was formatted -- e.g. an Excel header cell typed as a number
    rather than text). Returns the column's ORIGINAL object (not a
    stringified copy), so it can be used directly for Series/DataFrame
    lookups afterward -- see _frame_to_sheet()'s module-docstring note
    on why this matters. Returns None if not found.
    """
    for col in columns:
        if str(col).strip().lower() == target.lower():
            return col
    return None


def _frame_to_sheet(name: str, df: pd.DataFrame) -> SheetFrame:
    # Real-workbook regression: a trader-typed Excel header cell for a
    # position column ("1", "2", ...) is very often stored as a NUMBER,
    # not text -- pandas then reads df.columns as int/float for those
    # columns, not str. Every row lookup below MUST use these ORIGINAL,
    # un-stringified column objects (df.columns as pandas actually
    # produced them) -- a `series.get("1")` against a Series indexed by
    # the integer `1` returns None, silently, which previously made
    # _is_blank() treat every real data row as blank and drop it before
    # it ever reached validation.py. Only the OUTPUT representation
    # (SheetFrame.position_columns / each row dict's keys) is
    # stringified, once, at the very end -- so every downstream caller
    # still sees a stable "1", "2", ... str key regardless of how the
    # source file happened to type its header cells.
    original_columns = list(df.columns)
    market_col = _find_column(original_columns, MARKET_COLUMN)
    label_col = _find_column(original_columns, LABEL_COLUMN)

    if market_col is None or label_col is None:
        missing = [
            header for header, found in ((MARKET_COLUMN, market_col), (LABEL_COLUMN, label_col))
            if found is None
        ]
        return SheetFrame(
            name=name,
            position_columns=(),
            rows=[],
            row_numbers=[],
            parse_error=f"Missing required column(s): {', '.join(missing)}",
        )

    original_position_columns = [c for c in original_columns if c not in (market_col, label_col)]
    position_columns = tuple(str(c) for c in original_position_columns)

    rows: list[dict] = []
    row_numbers: list[int] = []
    for offset, (_, series) in enumerate(df.iterrows()):
        position_values = [series.get(col) for col in original_position_columns]
        if all(_is_blank(v) for v in position_values):
            continue  # a genuinely blank row -- silently skipped, not an error

        row = {
            MARKET_COLUMN: series.get(market_col),
            LABEL_COLUMN: series.get(label_col),
        }
        row.update({
            str_col: series.get(orig_col)
            for str_col, orig_col in zip(position_columns, original_position_columns)
        })
        rows.append(row)
        row_numbers.append(offset + 2)  # +1 for the header row, +1 for 1-based numbering

    return SheetFrame(
        name=name, position_columns=position_columns, rows=rows, row_numbers=row_numbers
    )


def parse_workbook(file_bytes: bytes) -> list[SheetFrame]:
    """Parse an .xlsx workbook into one SheetFrame per worksheet, in the
    workbook's own sheet order.

    Raises:
        ValueError: `file_bytes` is not a readable Excel workbook at
            all (corrupt file, wrong format) -- a whole-file problem,
            distinct from a per-sheet `parse_error`.
    """
    try:
        sheets = pd.read_excel(BytesIO(file_bytes), sheet_name=None, engine="openpyxl")
    except Exception as exc:  # noqa: BLE001 -- re-raised as a single, callable-typed error
        raise ValueError(f"Could not read this file as an Excel workbook: {exc}") from exc

    return [_frame_to_sheet(sheet_name, df) for sheet_name, df in sheets.items()]


def parse_csv(file_bytes: bytes, filename: str) -> SheetFrame:
    """Parse a single CSV file into one SheetFrame, named after
    `filename` with its extension stripped (e.g. "6mo Spreads.csv" ->
    "6mo Spreads").

    Raises:
        ValueError: `file_bytes` is not readable as CSV at all.
    """
    try:
        df = pd.read_csv(BytesIO(file_bytes))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not read this file as CSV: {exc}") from exc

    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    return _frame_to_sheet(name, df)


__all__ = ["SheetFrame", "MARKET_COLUMN", "LABEL_COLUMN", "parse_workbook", "parse_csv"]
