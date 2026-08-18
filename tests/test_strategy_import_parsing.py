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


# ---------------------------------------------------------------------
# Real-workbook regression: Excel header cells for position columns are
# very often typed as NUMBERS, not text (this is exactly how a trader
# naturally types "1", "2", "3" into a header row) -- pandas then reads
# df.columns as int for those columns, not str. Every fixture above
# uses string dict keys ("1", "2", ...), which pandas keeps as str
# columns from construction, so none of them exercise this path. These
# fixtures instead use bare int dict keys, which round-trip through
# .to_excel()/pd.read_excel() as genuinely int-typed columns -- exactly
# reproducing the real RBS_Template.xlsx's structure that surfaced this
# bug (every row in every sheet was silently treated as blank and
# dropped before ever reaching validation.py).
# ---------------------------------------------------------------------

def test_workbook_integer_typed_position_headers_parse_correctly():
    sheet = pd.DataFrame({"Market": ["SRA"], "Label": ["3M Spread"], 1: [1], 2: [-1], 3: [0]})
    frames = parse_workbook(_xlsx_bytes({"Sheet1": sheet}))
    frame = frames[0]

    assert frame.parse_error is None
    assert len(frame.rows) == 1  # previously 0 -- every row was misread as blank
    assert frame.rows[0]["Market"] == "SRA"
    assert frame.rows[0]["Label"] == "3M Spread"
    assert frame.rows[0]["1"] == 1
    assert frame.rows[0]["2"] == -1


def test_workbook_integer_headers_yield_string_position_columns():
    # The OUTPUT representation is always str, regardless of how the
    # source file typed its header cells -- callers (validation.py,
    # ui.*) never need to know or care.
    sheet = pd.DataFrame({"Market": ["SRA"], "Label": ["A"], 1: [1], 2: [-1]})
    frame = parse_workbook(_xlsx_bytes({"Sheet1": sheet}))[0]

    assert frame.position_columns == ("1", "2")
    assert all(isinstance(c, str) for c in frame.position_columns)
    assert all(isinstance(k, str) for k in frame.rows[0].keys())


def test_integer_and_string_header_xlsx_produce_equivalent_sheetframes():
    int_headed = pd.DataFrame(
        {"Market": ["SRA", "SON"], "Label": ["3M Spread", "6M Fly"], 1: [1, 1], 2: [-1, -2], 3: [0, 1]}
    )
    str_headed = pd.DataFrame(
        {
            "Market": ["SRA", "SON"], "Label": ["3M Spread", "6M Fly"],
            "1": [1, 1], "2": [-1, -2], "3": [0, 1],
        }
    )

    int_frame = parse_workbook(_xlsx_bytes({"Sheet1": int_headed}))[0]
    str_frame = parse_workbook(_xlsx_bytes({"Sheet1": str_headed}))[0]

    assert int_frame.position_columns == str_frame.position_columns
    assert int_frame.rows == str_frame.rows
    assert int_frame.row_numbers == str_frame.row_numbers


def test_workbook_integer_headers_blank_rows_still_dropped():
    # The blank-row filter itself must still work correctly once real
    # (non-blank) rows are no longer misclassified as blank.
    sheet = pd.DataFrame(
        {
            "Market": ["SRA", None], "Label": ["3M Spread", None],
            1: [1, None], 2: [-1, None], 3: [0, None],
        }
    )
    frame = parse_workbook(_xlsx_bytes({"Sheet1": sheet}))[0]
    assert len(frame.rows) == 1
    assert frame.rows[0]["Label"] == "3M Spread"
