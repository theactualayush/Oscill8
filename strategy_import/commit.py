"""
commit.py

The ONE place in strategy_import that writes to StrategySetRepository.
Everything in parsing.py/validation.py/preview.py is pure and in-memory
-- commit_import() is the sole write boundary, called only after the
user has seen the ImportPreview and explicitly confirmed Import/"Import
All" (per product decision: nothing is written to the repository before
that confirmation).

Only `preview.importable_candidates` are saved -- a candidate with a
sheet_error (bad header, invalid worksheet name, duplicate labels) is
never saved even partially; a candidate with zero ready rows has
nothing to save. Unavailable/invalid rows are never persisted under any
circumstance, on any candidate -- they exist only to be reported.

Each candidate is saved under its already-computed, de-duplicated
`import_name` (see strategy_import.naming.unique_strategy_set_name(),
applied at preview time) via the existing, unmodified
StrategySetRepository.save() -- an ordinary upsert to a name that
should not already exist by construction, since `import_name` was
computed against the same repository's own `exists()`. No new
persistence path, no new file format: an imported Strategy Set is
saved exactly like a manually-built one and is fully ordinary
afterward (select/edit/save/delete/run all work unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy_sets.model import StrategySet
from strategy_sets.repository import StrategySetRepository

from strategy_import.preview import ImportPreview


@dataclass(frozen=True)
class ImportSummary:
    """What the "Import successful" screen reports -- see the product
    brief's Strategy Sets created / Strategies imported / Unavailable /
    Invalid summary."""

    created_set_names: tuple[str, ...]
    strategies_imported: int
    unavailable_count: int
    invalid_count: int


def commit_import(preview: ImportPreview, repo: StrategySetRepository) -> ImportSummary:
    """Save every importable candidate in `preview` and return a summary
    for the post-import confirmation screen.

    unavailable_count/invalid_count on the returned summary are the
    PREVIEW's full totals (across every candidate, importable or not)
    -- the user should see how many strategies were left out of the
    whole upload, not just the ones belonging to sheets that ended up
    saved.
    """
    created: list[str] = []
    strategies_imported = 0

    for candidate in preview.importable_candidates:
        strategy_set = StrategySet(
            name=candidate.import_name, entries=tuple(row.entry for row in candidate.ready)
        )
        repo.save(strategy_set)
        created.append(candidate.import_name)
        strategies_imported += len(candidate.ready)

    return ImportSummary(
        created_set_names=tuple(created),
        strategies_imported=strategies_imported,
        unavailable_count=preview.unavailable_count,
        invalid_count=preview.invalid_count,
    )


__all__ = ["ImportSummary", "commit_import"]
