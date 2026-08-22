"""
tests/test_ui_strategy_import.py

End-to-end coverage of the Import Strategies panel through the REAL
Streamlit script (streamlit.testing.v1.AppTest), same convention as
tests/test_ui_strategy_set_selector_lifecycle.py: upload -> preview ->
Cancel/Import All, exercised against an isolated tmp_path-backed
StrategySetRepository so nothing ever touches the real data/
strategy_sets/ directory.

Covers the product requirements this stage must prove: uploading never
writes; Cancel never writes; Import All writes only READY rows; ER
stays visible as unavailable, never silently dropped; invalid rows
stay visible; duplicate names never overwrite; both CSV and XLSX work;
and an imported set is immediately selectable for Strategy Set Scan.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from core import config

from strategy_sets.repository import StrategySetRepository

_APP_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")

_CSV_MIXED = (
    b"Market,Label,1,2,3\n"
    b"SRA,3M Spread,1,-1,\n"
    b"SON,SONIA Fly,1,-2,1\n"
    b"ER,Euribor Trade,1,-1,\n"
    b"XYZ,Bad Market,1,-1,\n"
)


def _xlsx_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
    directory = tmp_path / "strategy_sets"
    monkeypatch.setattr(config, "STRATEGY_SETS_DIR", str(directory))
    return StrategySetRepository(base_dir=str(directory))


def _app() -> AppTest:
    return AppTest.from_file(_APP_PATH, default_timeout=60)


def _click(at: AppTest, label: str = None, key: str = None) -> AppTest:
    if key is not None:
        button = [b for b in at.button if b.key == key][0]
    else:
        button = [b for b in at.button if b.label == label][0]
    return button.click().run()


def _open_import_panel(at: AppTest) -> AppTest:
    return _click(at, label="📥 Import")


def _assert_no_exception(at: AppTest) -> None:
    assert list(at.exception) == []


# ---------------------------------------------------------------------
# Upload -> preview never writes
# ---------------------------------------------------------------------

def test_uploading_a_csv_builds_a_preview_without_writing(repo):
    at = _app()
    at.run()
    at = _open_import_panel(at)

    at.file_uploader[0].upload("strategies.csv", _CSV_MIXED, "text/csv").run()
    _assert_no_exception(at)

    assert repo.list_names() == []

    markdowns = [m.value for m in at.markdown]
    assert any("4 strategies detected" in m for m in markdowns)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {"Total": "4", "Ready": "2", "Unavailable": "1", "Invalid": "1"}


def test_cancel_after_preview_does_not_write(repo):
    at = _app()
    at.run()
    at = _open_import_panel(at)
    at.file_uploader[0].upload("strategies.csv", _CSV_MIXED, "text/csv").run()

    at = _click(at, key="oscill8_import_cancel_button")
    _assert_no_exception(at)

    assert repo.list_names() == []
    # The panel itself closed -- no uploader left rendered.
    assert len(at.file_uploader) == 0


# ---------------------------------------------------------------------
# Import writes only ready rows; ER and invalid rows stay visible
# ---------------------------------------------------------------------

def test_import_writes_only_ready_rows(repo):
    at = _app()
    at.run()
    at = _open_import_panel(at)
    at.file_uploader[0].upload("strategies.csv", _CSV_MIXED, "text/csv").run()

    at = _click(at, key="oscill8_import_confirm_button")
    _assert_no_exception(at)

    assert repo.list_names() == ["strategies"]
    saved = repo.load("strategies")
    assert [e.name for e in saved.entries] == ["3M Spread", "SONIA Fly"]  # ER/XYZ rows excluded

    successes = [s.value for s in at.success]
    assert any("Strategies imported: 2" in s for s in successes)
    assert any("Unavailable: 1" in s for s in successes)
    assert any("Invalid: 1" in s for s in successes)


def test_er_remains_visible_as_unavailable_in_the_preview(repo):
    at = _app()
    at.run()
    at = _open_import_panel(at)
    at.file_uploader[0].upload("strategies.csv", _CSV_MIXED, "text/csv").run()

    captions = [c.value for c in at.caption]
    assert any("ER ⚠" in c and "Euribor" in c for c in captions)
    assert any("Euribor Trade" in c for c in captions)  # the row itself, not just the market code


def test_invalid_rows_remain_visible_in_the_preview(repo):
    at = _app()
    at.run()
    at = _open_import_panel(at)
    at.file_uploader[0].upload("strategies.csv", _CSV_MIXED, "text/csv").run()

    captions = [c.value for c in at.caption]
    assert any("Row 5" in c and "Bad Market" in c and "XYZ" in c for c in captions)


# ---------------------------------------------------------------------
# Duplicate names never overwrite
# ---------------------------------------------------------------------

def test_reimporting_the_same_csv_does_not_overwrite(repo):
    at = _app()
    at.run()
    at = _open_import_panel(at)
    at.file_uploader[0].upload("strategies.csv", _CSV_MIXED, "text/csv").run()
    at = _click(at, key="oscill8_import_confirm_button")

    at = _open_import_panel(at)
    at.file_uploader[0].upload("strategies.csv", _CSV_MIXED, "text/csv").run()
    at = _click(at, key="oscill8_import_confirm_button")
    _assert_no_exception(at)

    assert sorted(repo.list_names()) == ["strategies", "strategies 2"]
    original = repo.load("strategies")
    duplicate = repo.load("strategies 2")
    assert [e.name for e in original.entries] == [e.name for e in duplicate.entries]


# ---------------------------------------------------------------------
# XLSX: one worksheet = one Strategy Set
# ---------------------------------------------------------------------

def test_xlsx_import_creates_one_strategy_set_per_worksheet(repo):
    sheets = {
        "Sheet One": pd.DataFrame({"Market": ["SRA"], "Label": ["A"], "1": [1], "2": [-1]}),
        "Sheet Two": pd.DataFrame({"Market": ["CRA"], "Label": ["B"], "1": [1], "2": [-2], "3": [1]}),
    }
    at = _app()
    at.run()
    at = _open_import_panel(at)
    at.file_uploader[0].upload(
        "workbook.xlsx", _xlsx_bytes(sheets),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).run()
    _assert_no_exception(at)

    markdowns = [m.value for m in at.markdown]
    assert any("2 Strategy Set(s) detected" in m for m in markdowns)

    at = _click(at, key="oscill8_import_confirm_button")
    _assert_no_exception(at)

    assert sorted(repo.list_names()) == ["Sheet One", "Sheet Two"]
    assert [e.name for e in repo.load("Sheet One").entries] == ["A"]
    assert [e.name for e in repo.load("Sheet Two").entries] == ["B"]


# ---------------------------------------------------------------------
# Imported sets are immediately usable by Strategy Set Scan
# ---------------------------------------------------------------------

def test_imported_set_is_immediately_selectable_and_runnable(repo):
    # Task 1: there is exactly one Run Scan path -- an imported set
    # becomes selectable and its rows load into the SAME grid "▶ Run
    # Scan" always scans, with no separate per-set run button/interval.
    at = _app()
    at.run()
    at = _open_import_panel(at)
    at.file_uploader[0].upload(
        "csv_import.csv", b"Market,Label,1,2\nSRA,A,1,-1\n", "text/csv",
    ).run()
    at = _click(at, key="oscill8_import_confirm_button")
    _assert_no_exception(at)

    selector = [s for s in at.selectbox if s.label == "Strategy Set"][0]
    assert selector.value == "csv_import"

    run_labels = [b.label for b in at.button if "Run" in b.label]
    assert run_labels == ["▶ Run Scan"]
