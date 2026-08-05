"""
test_ui_controls.py

Tests for ui/controls.py's pure grid-construction helper. Only
_default_grid() is pure (plain pandas, no Streamlit widget calls) --
everything else in controls.py renders Streamlit widgets directly and
is exercised via the browser smoke test instead, per the project's
"avoid brittle Streamlit rendering tests" guidance.
"""

from __future__ import annotations

import dataclasses

from ui.controls import _DEFAULT_LOWER_PERCENTILE, _DEFAULT_UPPER_PERCENTILE, ScanSetup, _default_grid
from ui.formatting import position_column


def test_default_grid_populates_the_example_fly():
    df = _default_grid(6)
    assert list(df["Label"]) == ["3M Fly"]
    assert df.loc[0, position_column(1)] == "1"
    assert df.loc[0, position_column(2)] == "-2"
    assert df.loc[0, position_column(3)] == "1"


def test_default_grid_unpopulated_positions_are_blank_not_zero():
    # Unpopulated cells must be genuinely blank in the grid, not a typed
    # 0 -- 0 is a meaningful user choice ("skip this position"), not the
    # default appearance of an untouched cell. Position cells are
    # TextColumns specifically so an empty string renders blank (see
    # ui.controls._render_strategy_grid's column_config for why -- this
    # Streamlit build renders an unpopulated NumberColumn cell as the
    # literal text "None", verified empirically, regardless of dtype).
    df = _default_grid(6)
    for i in range(4, 7):
        assert df.loc[0, position_column(i)] == ""


def test_default_grid_column_count_matches_requested_positions():
    df = _default_grid(4)
    position_cols = [c for c in df.columns if c != "Label"]
    assert position_cols == [position_column(i) for i in range(1, 5)]


# ---------------------------------------------------------------------
# Percentile Range control -- ScanSetup carries lower/upper_percentile
# ---------------------------------------------------------------------

def test_default_percentile_constants_are_5_and_95():
    # The Streamlit number_input widgets default to these -- pinned here
    # so a future edit that changes the trader-facing default is a
    # visible, deliberate test change, not silent.
    assert _DEFAULT_LOWER_PERCENTILE == 5
    assert _DEFAULT_UPPER_PERCENTILE == 95


def test_scan_setup_has_percentile_fields():
    field_names = {f.name for f in dataclasses.fields(ScanSetup)}
    assert {"lower_percentile", "upper_percentile"}.issubset(field_names)
