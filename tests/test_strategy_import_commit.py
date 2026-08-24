"""
tests/test_strategy_import_commit.py

strategy_import.commit.commit_import() -- the one write boundary.
Tested against an isolated tmp_path-backed StrategySetRepository, same
convention as tests/test_strategy_sets_repository.py.

Key invariants under test: nothing is written until commit_import() is
called (build_preview() alone must never touch the repository's
filesystem), only importable candidates are saved, unavailable/invalid
rows are never persisted under any circumstance, and an imported
Strategy Set is a completely ordinary one afterward -- loadable via the
same StrategySetRepository.load() a hand-built set would use.
"""

from __future__ import annotations

import pytest

from strategy_sets.repository import StrategySetRepository

from strategy_import.commit import commit_import
from strategy_import.market_mapping import UNAVAILABLE_MARKET_CODES
from strategy_import.parsing import SheetFrame
from strategy_import.preview import build_preview


@pytest.fixture
def synthetic_unavailable_market(monkeypatch):
    # Clearly-synthetic, non-real code -- ER is now genuinely SUPPORTED
    # (EURIBOR gained a core.config.MARKETS entry), so it can no longer
    # illustrate the unavailable-row code path.
    monkeypatch.setitem(UNAVAILABLE_MARKET_CODES, "ZZZ", "ZZZ is not currently configured in Oscill8.")
    return "ZZZ"


def _sheet(name, rows, position_columns=("1", "2", "3")) -> SheetFrame:
    row_numbers = list(range(2, 2 + len(rows)))
    return SheetFrame(name=name, position_columns=position_columns, rows=rows, row_numbers=row_numbers)


def _row(market, label, *weights) -> dict:
    row = {"Market": market, "Label": label}
    row.update({str(i + 1): w for i, w in enumerate(weights)})
    return row


def test_building_a_preview_writes_nothing(tmp_path):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    sheet = _sheet("New Set", [_row("SRA", "A", 1, -1, 0)])
    build_preview([sheet], repo.exists)
    assert repo.list_names() == []


def test_commit_saves_every_importable_candidate(tmp_path):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    sheets = [
        _sheet("Set A", [_row("SRA", "A1", 1, -1, 0)]),
        _sheet("Set B", [_row("SON", "B1", 1, -2, 1)]),
    ]
    preview = build_preview(sheets, repo.exists)
    summary = commit_import(preview, repo)

    assert set(repo.list_names()) == {"Set A", "Set B"}
    assert set(summary.created_set_names) == {"Set A", "Set B"}
    assert summary.strategies_imported == 2


def test_commit_never_saves_a_sheet_with_a_blocking_error(tmp_path):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    sheet = _sheet("Bad & Sheet", [_row("SRA", "A", 1, -1, 0)])
    preview = build_preview([sheet], repo.exists)
    summary = commit_import(preview, repo)

    assert repo.list_names() == []
    assert summary.created_set_names == ()
    assert summary.strategies_imported == 0


def test_commit_never_persists_unavailable_or_invalid_rows(tmp_path, synthetic_unavailable_market):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    sheet = _sheet(
        "Mixed Set",
        [
            _row("SRA", "Ready", 1, -1, 0),
            _row("ZZZ", "Unavailable", 1, -1, 0),
            _row("XYZ", "Invalid", 1, -1, 0),
        ],
    )
    preview = build_preview([sheet], repo.exists)
    summary = commit_import(preview, repo)

    saved = repo.load("Mixed Set")
    assert [e.name for e in saved.entries] == ["Ready"]
    assert summary.strategies_imported == 1
    assert summary.unavailable_count == 1
    assert summary.invalid_count == 1


def test_commit_never_overwrites_an_existing_set(tmp_path):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    # An existing manually-created set already occupies this name.
    first_sheet = _sheet("Churning", [_row("SRA", "Original", 1, -1, 0)])
    commit_import(build_preview([first_sheet], repo.exists), repo)

    # Re-"import" a workbook with a sheet of the same name.
    second_sheet = _sheet("Churning", [_row("SON", "New", 1, -2, 1)])
    preview = build_preview([second_sheet], repo.exists)
    commit_import(preview, repo)

    assert set(repo.list_names()) == {"Churning", "Churning 2"}
    original = repo.load("Churning")
    duplicate = repo.load("Churning 2")
    assert [e.name for e in original.entries] == ["Original"]
    assert [e.name for e in duplicate.entries] == ["New"]


def test_imported_set_is_ordinary_afterward(tmp_path):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    sheet = _sheet("Imported", [_row("SRA", "A", 1, -1, 0)])
    commit_import(build_preview([sheet], repo.exists), repo)

    loaded = repo.load("Imported")
    # Fully ordinary StrategySet operations: rename, duplicate, delete.
    repo.rename("Imported", "Renamed")
    assert repo.exists("Renamed")
    repo.duplicate("Renamed", "Copy")
    assert repo.exists("Copy")
    assert repo.delete("Copy") is True


def test_summary_totals_reflect_whole_preview_not_just_saved_sheets(tmp_path, synthetic_unavailable_market):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    good = _sheet("Good", [_row("SRA", "A", 1, -1, 0)])
    unimportable = _sheet("All Bad", [_row("ZZZ", "B", 1, -1, 0), _row("XYZ", "C", 1, -1, 0)])
    preview = build_preview([good, unimportable], repo.exists)
    summary = commit_import(preview, repo)

    assert summary.created_set_names == ("Good",)
    assert summary.strategies_imported == 1
    assert summary.unavailable_count == 1
    assert summary.invalid_count == 1
