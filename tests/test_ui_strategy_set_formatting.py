"""
tests/test_ui_strategy_set_formatting.py

Tests for Module 7B's pure UI helper logic (ui/strategy_set_formatting.py):
the "Strategies in Set" display-row translation (Enabled/Name/Market/
Interval/Weights -- deliberately no derived Structure/Shape column, see
the module docstring), applying edited Enabled values back onto a
draft, and building a new StrategySetEntry from the same curve-position
grid row shape ui.controls' Strategy Templates grid produces. No
Streamlit rendering is exercised here -- plain functions over plain
data and the real, unmodified strategy_engine/strategy_sets objects,
matching tests/test_ui_formatting.py's own convention for Module 6A.
"""

from __future__ import annotations

import pytest

from core.config import MARKETS, BarInterval

from strategy_engine.definitions import StrategyDefinition

from strategy_sets.model import StrategySetEntry

from ui.formatting import position_column
from ui.strategy_set_formatting import (
    ENABLED_COLUMN,
    ENTRY_TABLE_COLUMNS,
    INTERVAL_COLUMN,
    MARKET_COLUMN,
    NAME_COLUMN,
    WEIGHTS_COLUMN,
    apply_enabled_edits,
    build_entry_from_grid_row,
    entries_to_rows,
    entry_names,
    format_weights,
    remove_entry_by_name,
)


def _entry(
    name="SOFR Fly", weights=(1.0, -2.0, 1.0), enabled=True, market_key="SOFR", interval=BarInterval.DAILY
) -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key=market_key,
        offsets=tuple(range(len(weights))),
        weights=weights,
        interval=interval,
    )
    return StrategySetEntry(name=name, definition=definition, enabled=enabled)


# ---------------------------------------------------------------------
# ENTRY_TABLE_COLUMNS: exact column set, no Structure/Shape classification
# ---------------------------------------------------------------------

def test_entry_table_columns_are_exactly_enabled_name_market_interval_weights():
    assert ENTRY_TABLE_COLUMNS == (
        ENABLED_COLUMN, NAME_COLUMN, MARKET_COLUMN, INTERVAL_COLUMN, WEIGHTS_COLUMN,
    )


def test_entry_table_columns_contain_no_structure_or_shape_label():
    lowered = [c.lower() for c in ENTRY_TABLE_COLUMNS]
    for forbidden in ("structure", "shape", "fly", "condor", "butterfly", "curve"):
        assert forbidden not in lowered


def test_no_structure_classification_function_exists():
    # Locks in the design brief's core rule: the application must never
    # infer/assign a generic trading-shape label (Fly/Condor/Butterfly/
    # Curve) as a separate field -- an earlier version of this module
    # had exactly that (describe_structure()) and it was removed.
    import ui.strategy_set_formatting as module

    assert not hasattr(module, "describe_structure")


def test_format_weights_matches_ui_formatting_fmt_number_style():
    assert format_weights((1.0, -2.0, 1.0)) == "1.00 / -2.00 / 1.00"


# ---------------------------------------------------------------------
# entries_to_rows
# ---------------------------------------------------------------------

def test_entries_to_rows_builds_one_row_per_entry_in_order():
    entries = [
        _entry(name="SOFR 6M Fly", weights=(1.0, -2.0, 1.0), interval=BarInterval.DAILY),
        _entry(
            name="SOFR Curve", weights=(1.0, -1.0, -1.0, -1.0, 1.0), enabled=False,
            interval=BarInterval.HOURLY,
        ),
    ]
    rows = entries_to_rows(entries)

    assert len(rows) == 2
    assert rows[0][NAME_COLUMN] == "SOFR 6M Fly"
    assert rows[0][MARKET_COLUMN] == MARKETS["SOFR"].name
    assert rows[0][INTERVAL_COLUMN] == "DAILY"
    assert rows[0][WEIGHTS_COLUMN] == "1.00 / -2.00 / 1.00"
    assert rows[0][ENABLED_COLUMN] is True

    assert rows[1][NAME_COLUMN] == "SOFR Curve"
    assert rows[1][INTERVAL_COLUMN] == "HOURLY"
    assert rows[1][ENABLED_COLUMN] is False

    for row in rows:
        assert set(row.keys()) == set(ENTRY_TABLE_COLUMNS)


def test_entries_to_rows_empty_list_returns_empty():
    assert entries_to_rows([]) == []


def test_entries_to_rows_preserves_user_defined_names_verbatim_regardless_of_weights():
    # A Strategy Set entry's name is entirely user-defined -- it must be
    # displayed exactly as saved, never replaced or annotated with a
    # derived shape label, no matter how many legs/what weights it has.
    cases = [
        ("6M Churning", (1.0, -2.0, 1.0)),
        ("Intermarket Churning", (1.0, -1.0)),
        ("RBS Intermarket", (1.0,)),
        ("12M Range Bounds", (1.0, -1.0, -1.0, 1.0)),
        ("3M Double Butterfly", (1.0, -3.0, 3.0, -1.0)),
        ("My SOFR Strategy", (1.0, -2.0, 2.0, -1.0, 1.0)),
    ]
    entries = [_entry(name=name, weights=weights) for name, weights in cases]
    rows = entries_to_rows(entries)

    assert [row[NAME_COLUMN] for row in rows] == [name for name, _ in cases]


def test_entries_to_rows_interval_reflects_the_entrys_own_definition():
    entries = [
        _entry(name="Daily One", interval=BarInterval.DAILY),
        _entry(name="Hourly One", interval=BarInterval.HOURLY),
        _entry(name="Four Hour One", interval=BarInterval.FOUR_HOUR),
    ]
    rows = entries_to_rows(entries)
    assert [row[INTERVAL_COLUMN] for row in rows] == ["DAILY", "HOURLY", "4H"]


# ---------------------------------------------------------------------
# apply_enabled_edits
# ---------------------------------------------------------------------

def test_apply_enabled_edits_toggles_enabled_flags_by_position():
    entries = [_entry(name="A", enabled=True), _entry(name="B", enabled=False)]
    edited_rows = [
        {ENABLED_COLUMN: False, NAME_COLUMN: "A"},
        {ENABLED_COLUMN: True, NAME_COLUMN: "B"},
    ]

    updated = apply_enabled_edits(entries, edited_rows)

    assert updated[0].name == "A" and updated[0].enabled is False
    assert updated[1].name == "B" and updated[1].enabled is True
    # original untouched (StrategySetEntry is frozen; this also proves
    # apply_enabled_edits returns a new list rather than mutating).
    assert entries[0].enabled is True
    assert entries[1].enabled is False


def test_apply_enabled_edits_preserves_definition_and_name():
    entries = [_entry(name="A", weights=(1.0, -1.0))]
    updated = apply_enabled_edits(entries, [{ENABLED_COLUMN: False}])
    assert updated[0].definition == entries[0].definition
    assert updated[0].name == entries[0].name


def test_apply_enabled_edits_returns_unchanged_on_row_count_mismatch():
    entries = [_entry(name="A"), _entry(name="B")]
    updated = apply_enabled_edits(entries, [{ENABLED_COLUMN: False}])
    assert updated == entries


# ---------------------------------------------------------------------
# entry_names / remove_entry_by_name
# ---------------------------------------------------------------------

def test_entry_names_returns_names_in_order():
    entries = [_entry(name="A"), _entry(name="B")]
    assert entry_names(entries) == ["A", "B"]


def test_remove_entry_by_name_drops_the_matching_entry_only():
    entries = [_entry(name="A"), _entry(name="B"), _entry(name="C")]
    remaining = remove_entry_by_name(entries, "B")
    assert entry_names(remaining) == ["A", "C"]


def test_remove_entry_by_name_is_a_noop_for_unknown_name():
    entries = [_entry(name="A")]
    assert remove_entry_by_name(entries, "Does Not Exist") == entries


# ---------------------------------------------------------------------
# build_entry_from_grid_row
# ---------------------------------------------------------------------

_POS3 = tuple(position_column(i) for i in (1, 2, 3))


def test_build_entry_from_grid_row_translates_a_valid_row():
    row = {"Label": "Fly Row", _POS3[0]: "1", _POS3[1]: "-2", _POS3[2]: "1"}
    entry = build_entry_from_grid_row(row, _POS3, "SOFR", BarInterval.DAILY, "SOFR 6M Fly")

    assert entry.name == "SOFR 6M Fly"
    assert entry.definition.market_key == "SOFR"
    assert entry.definition.offsets == (0, 1, 2)
    assert entry.definition.weights == (1.0, -2.0, 1.0)
    assert entry.enabled is True


def test_build_entry_from_grid_row_falls_back_to_grid_label_when_name_blank():
    row = {"Label": "My Label", _POS3[0]: "1", _POS3[1]: "-1", _POS3[2]: ""}
    entry = build_entry_from_grid_row(row, _POS3, "SOFR", BarInterval.DAILY, "  ")
    assert entry.name == "My Label"


def test_build_entry_from_grid_row_rejects_all_blank_row():
    row = {"Label": "Empty", _POS3[0]: "", _POS3[1]: "", _POS3[2]: ""}
    with pytest.raises(ValueError):
        build_entry_from_grid_row(row, _POS3, "SOFR", BarInterval.DAILY, "Whatever")
