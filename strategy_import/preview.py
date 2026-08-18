"""
preview.py

Groups already-classified rows (strategy_import/validation.py) into
one ImportPreview per uploaded workbook/CSV -- the object the UI layer
renders as the "Import Preview" screen and, unchanged, the object the
confirmed import step saves from. Building a preview NEVER touches
StrategySetRepository for writes -- see the module docstring below for
the one read-only exception (name-existence checks, needed to compute
the de-duplicated import name the user will actually see before they
confirm anything).

Per-sheet outcome counts are three-way, mirroring validation.py:
ready / unavailable / invalid. A sheet can additionally carry a
`sheet_error` -- a BLOCKING problem with the sheet as a whole, distinct
from any individual row:
    - the sheet is missing a "Market" or "Label" column entirely
      (SheetFrame.parse_error, from parsing.py)
    - the worksheet/file name doesn't satisfy StrategySet's own name
      rules (strategy_sets.model._SET_NAME_PATTERN) -- per product
      decision, this is NEVER auto-sanitized; the sheet is reported as
      an error telling the user to rename the worksheet, exactly as
      given
    - two ready rows in the same sheet share a Label -- StrategySet
      itself requires unique entry names (strategy_sets/model.py); like
      the sheet-name case, this is reported for the user to fix in the
      source file rather than silently renamed
A sheet_error means that sheet cannot be imported at all this round,
even if some of its rows individually validated -- but its ready/
unavailable/invalid rows are still fully populated and shown, so
nothing about it looks silently dropped; only the ACT of importing it
is blocked until the user fixes the workbook and re-uploads.

Duplicate Strategy Set names: `name_exists` is called once per sheet
with at least one ready row (read-only -- see
strategy_sets.repository.StrategySetRepository.exists(), never
.save()/.load() with side effects) to compute a de-duplicated
`import_name` via strategy_import.naming.unique_strategy_set_name() --
"Name", then "Name 2", "Name 3", ... An existing saved set is NEVER
overwritten; the resolved name is shown in the preview so the user
knows exactly what will be saved before they confirm Import, not
after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from core.config import BarInterval

from strategy_sets.model import StrategySet

from strategy_import.naming import unique_strategy_set_name
from strategy_import.parsing import SheetFrame
from strategy_import.validation import (
    DEFAULT_IMPORT_INTERVAL,
    InvalidRow,
    ReadyRow,
    UnavailableRow,
    validate_row,
)


@dataclass(frozen=True)
class ImportCandidate:
    """One worksheet/CSV file's import outcome."""

    sheet_name: str
    import_name: str | None  # resolved, de-duplicated Strategy Set name; None if not importable
    ready: tuple[ReadyRow, ...]
    unavailable: tuple[UnavailableRow, ...]
    invalid: tuple[InvalidRow, ...]
    sheet_error: str | None = None

    @property
    def total(self) -> int:
        return len(self.ready) + len(self.unavailable) + len(self.invalid)

    @property
    def importable(self) -> bool:
        """Whether Import/"Import All" can create a Strategy Set from
        this candidate at all -- a blocking sheet_error or zero ready
        rows both make a sheet non-importable this round (a sheet whose
        every row is unavailable/invalid has nothing left to save)."""
        return self.sheet_error is None and len(self.ready) > 0


@dataclass(frozen=True)
class ImportPreview:
    """The whole upload's import outcome -- one or more ImportCandidates."""

    candidates: tuple[ImportCandidate, ...]

    @property
    def total_strategies(self) -> int:
        return sum(c.total for c in self.candidates)

    @property
    def ready_count(self) -> int:
        return sum(len(c.ready) for c in self.candidates)

    @property
    def unavailable_count(self) -> int:
        return sum(len(c.unavailable) for c in self.candidates)

    @property
    def invalid_count(self) -> int:
        return sum(len(c.invalid) for c in self.candidates)

    @property
    def unavailable_by_market(self) -> dict[str, int]:
        """e.g. {"ER": 20} -- aggregated across every sheet, for the
        "ER ⚠ Euribor is not currently configured" summary line."""
        counts: dict[str, int] = {}
        for candidate in self.candidates:
            for row in candidate.unavailable:
                counts[row.market_code] = counts.get(row.market_code, 0) + 1
        return counts

    @property
    def importable_candidates(self) -> tuple[ImportCandidate, ...]:
        return tuple(c for c in self.candidates if c.importable)


def _classify_rows(
    sheet: SheetFrame, interval: BarInterval
) -> tuple[list[ReadyRow], list[UnavailableRow], list[InvalidRow]]:
    ready: list[ReadyRow] = []
    unavailable: list[UnavailableRow] = []
    invalid: list[InvalidRow] = []
    for row, row_number in zip(sheet.rows, sheet.row_numbers):
        outcome = validate_row(row, row_number, sheet.position_columns, interval)
        if isinstance(outcome, ReadyRow):
            ready.append(outcome)
        elif isinstance(outcome, UnavailableRow):
            unavailable.append(outcome)
        else:
            invalid.append(outcome)
    return ready, unavailable, invalid


def build_preview(
    sheets: Sequence[SheetFrame],
    name_exists: Callable[[str], bool],
    interval: BarInterval = DEFAULT_IMPORT_INTERVAL,
) -> ImportPreview:
    """Build the full ImportPreview for a parsed workbook/CSV.

    `name_exists` should be a read-only existence check (e.g.
    `StrategySetRepository.exists`) -- called at most once per sheet
    that has at least one ready row. `interval` is the fixed interval
    every imported entry gets (see validation.py's module docstring);
    left as a parameter rather than hard-coded so a future "choose the
    import interval up front" UI refinement is a one-argument change,
    not a rewrite.
    """
    candidates: list[ImportCandidate] = []

    for sheet in sheets:
        if sheet.parse_error:
            candidates.append(
                ImportCandidate(sheet.name, None, (), (), (), sheet_error=sheet.parse_error)
            )
            continue

        ready, unavailable, invalid = _classify_rows(sheet, interval)

        import_name: str | None = None
        sheet_error: str | None = None
        if ready:
            candidate_name = unique_strategy_set_name(sheet.name, name_exists)
            try:
                StrategySet(name=candidate_name, entries=tuple(r.entry for r in ready))
                import_name = candidate_name
            except ValueError as exc:
                # Either the sheet/file name itself fails StrategySet's
                # name pattern, or two ready rows share a Label --
                # StrategySet's own validation is the single source of
                # truth for both; never duplicated or re-derived here.
                # Per product decision: never auto-sanitized/auto-
                # renamed, always surfaced for the user to fix at the
                # source.
                sheet_error = str(exc)

        candidates.append(
            ImportCandidate(
                sheet_name=sheet.name,
                import_name=import_name,
                ready=tuple(ready),
                unavailable=tuple(unavailable),
                invalid=tuple(invalid),
                sheet_error=sheet_error,
            )
        )

    return ImportPreview(tuple(candidates))


__all__ = ["ImportCandidate", "ImportPreview", "build_preview"]
