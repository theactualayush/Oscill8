"""
tests/test_strategy_import_parsing.py

strategy_import.parsing: Excel workbook -> one SheetFrame per
worksheet, CSV -> one SheetFrame named from the filename, blank-row
dropping, and sheet-level parse errors for a missing Market/Label
column. No market-code or strategy-shape validation happens here (see
test_strategy_import_validation.py) -- these tests only check the
spreadsheet/CSV shape is read correctly.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from strategy_import.parsing import parse_csv, parse_workbook


def _csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def _xlsx_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


# ---------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------

def test_csv_one_file_is_one_sheetframe_named_from_filename():
    csv = "Market,Label,1,2,3\nSRA,3M Spread,1,-1,\n"
    sheet = parse_csv(_csv_bytes(csv), "6mo Spreads and Flys.csv")
    assert sheet.name == "6mo Spreads and Flys"
    assert sheet.parse_error is None
    assert len(sheet.rows) == 1


def test_csv_position_columns_preserve_order():
    csv = "Market,Label,1,2,3,4\nSRA,Fly,1,-2,1,\n"
    sheet = parse_csv(_csv_bytes(csv), "strategies.csv")
    assert sheet.position_columns == ("1", "2", "3", "4")


def test_csv_row_values_and_row_numbers():
    csv = "Market,Label,1,2\nSRA,3M Spread,1,-1\nSON,6M Spread,1,0\n"
    sheet = parse_csv(_csv_bytes(csv), "strategies.csv")
    assert sheet.row_numbers == [2, 3]
    assert sheet.rows[0]["Market"] == "SRA"
    assert sheet.rows[0]["Label"] == "3M Spread"
    assert sheet.rows[1]["Market"] == "SON"


def test_csv_blank_rows_are_dropped_silently():
    csv = "Market,Label,1,2\nSRA,3M Spread,1,-1\n,,,\nSON,6M Spread,1,0\n"
    sheet = parse_csv(_csv_bytes(csv), "strategies.csv")
    assert len(sheet.rows) == 2
    assert sheet.row_numbers == [2, 4]  # the blank row (3) never appears


def test_csv_header_matched_case_insensitively():
    csv = "market,label,1,2\nSRA,3M Spread,1,-1\n"
    sheet = parse_csv(_csv_bytes(csv), "strategies.csv")
    assert sheet.parse_error is None
    assert sheet.rows[0]["Market"] == "SRA"


def test_csv_missing_market_column_is_a_sheet_level_parse_error():
    csv = "Label,1,2\n3M Spread,1,-1\n"
    sheet = parse_csv(_csv_bytes(csv), "strategies.csv")
    assert sheet.parse_error is not None
    assert "Market" in sheet.parse_error
    assert sheet.rows == []


def test_csv_missing_label_column_is_a_sheet_level_parse_error():
    csv = "Market,1,2\nSRA,1,-1\n"
    sheet = parse_csv(_csv_bytes(csv), "strategies.csv")
    assert sheet.parse_error is not None
    assert "Label" in sheet.parse_error


def test_csv_extension_stripped_but_dots_in_name_kept_reasonable():
    csv = "Market,Label,1\nSRA,X,1\n"
    sheet = parse_csv(_csv_bytes(csv), "EZ8.General.csv")
    assert sheet.name == "EZ8.General"


def test_unreadable_csv_raises_value_error():
    with pytest.raises(ValueError):
        parse_csv(b"\x00\x01\x02not a csv at all\xff\xfe", "bad.csv")


# ---------------------------------------------------------------------
# Excel workbook
# ---------------------------------------------------------------------

def test_workbook_one_sheetframe_per_worksheet_in_order():
    sheets = {
        "EZ8 GENERAL MEDIUM VOL": pd.DataFrame(
            {"Market": ["SRA"], "Label": ["3M Spread"], "1": [1], "2": [-1]}
        ),
        "6mo Spreads and Flys": pd.DataFrame(
            {"Market": ["SON"], "Label": ["6M Fly"], "1": [1], "2": [-2], "3": [1]}
        ),
    }
    frames = parse_workbook(_xlsx_bytes(sheets))
    assert [f.name for f in frames] == ["EZ8 GENERAL MEDIUM VOL", "6mo Spreads and Flys"]
    assert len(frames[0].rows) == 1
    assert len(frames[1].rows) == 1


def test_workbook_sheet_name_becomes_sheetframe_name_verbatim():
    sheets = {"Churning Low Vol": pd.DataFrame({"Market": ["CRA"], "Label": ["X"], "1": [1]})}
    frames = parse_workbook(_xlsx_bytes(sheets))
    assert frames[0].name == "Churning Low Vol"


def test_workbook_each_sheet_gets_independent_position_columns():
    sheets = {
        "Short": pd.DataFrame({"Market": ["SRA"], "Label": ["A"], "1": [1], "2": [-1]}),
        "Long": pd.DataFrame(
            {"Market": ["CRA"], "Label": ["B"], "1": [1], "2": [0], "3": [-1], "4": [0]}
        ),
    }
    frames = parse_workbook(_xlsx_bytes(sheets))
    assert frames[0].position_columns == ("1", "2")
    assert frames[1].position_columns == ("1", "2", "3", "4")


def test_workbook_bad_sheet_reported_independently_of_good_sheet():
    sheets = {
        "Good": pd.DataFrame({"Market": ["SRA"], "Label": ["A"], "1": [1]}),
        "Missing Market Column": pd.DataFrame({"Label": ["A"], "1": [1]}),
    }
    frames = parse_workbook(_xlsx_bytes(sheets))
    good = next(f for f in frames if f.name == "Good")
    bad = next(f for f in frames if f.name == "Missing Market Column")
    assert good.parse_error is None
    assert bad.parse_error is not None


def test_unreadable_workbook_raises_value_error():
    with pytest.raises(ValueError):
        parse_workbook(b"not an xlsx file at all")
