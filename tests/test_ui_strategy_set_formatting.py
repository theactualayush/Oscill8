"""
tests/test_ui_strategy_set_formatting.py

Tests for the Module 7B simplification's pure helper logic (ui/
strategy_set_formatting.py): translating a StrategySet's enabled
entries into Strategy Templates grid rows (Label + Market + Interval +
dense weights) and building a brand-new StrategySet directly from the
grid's current rows. No Streamlit rendering is exercised here -- plain
functions over plain data and the real, unmodified strategy_engine/
strategy_sets objects, matching tests/test_ui_formatting.py's own
convention.

Multi-market fix: every grid row carries its OWN Market/Interval (see
ui.formatting.MARKET_COLUMN/INTERVAL_COLUMN), so a Strategy Set mixing
markets (e.g. SOFR + SONIA + CORRA) is representable and round-trips
losslessly -- there is no more "resolve one market/interval for the
whole set, warn if they disagree" step; that function was removed.
"""

from __future__ import annotations

import pytest

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition

from strategy_sets.model import StrategySet, StrategySetEntry

from ui.formatting import INTERVAL_COLUMN, LABEL_COLUMN, MARKET_COLUMN, position_column
from ui.strategy_set_formatting import (
    build_strategy_set_from_grid,
    dense_row_from_definition,
    format_grid_weight,
    grid_rows_from_strategy_set,
)

_POS6 = tuple(position_column(i) for i in range(1, 7))


def _definition(weights=(1.0, -2.0, 1.0), market_key="SOFR", interval=BarInterval.DAILY) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=tuple(range(len(weights))), weights=weights, interval=interval,
    )


def _entry(name="SOFR Fly", weights=(1.0, -2.0, 1.0), enabled=True, market_key="SOFR", interval=BarInterval.DAILY) -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_definition(weights, market_key, interval), enabled=enabled)


# ---------------------------------------------------------------------
# format_grid_weight
# ---------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [(1.0, "1"), (-2.0, "-2"), (0.5, "0.5"), (-1.5, "-1.5")])
def test_format_grid_weight(value, expected):
    assert format_grid_weight(value) == expected


# ---------------------------------------------------------------------
# dense_row_from_definition / grid_rows_from_strategy_set
# ---------------------------------------------------------------------

def test_dense_row_from_definition_places_weights_at_offset_positions():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 2, 4), weights=(1.0, -2.0, 1.0), interval=BarInterval.DAILY,
    )
    row = dense_row_from_definition("My Fly", definition, _POS6)

    assert row[LABEL_COLUMN] == "My Fly"
    assert row[_POS6[0]] == "1"
    assert row[_POS6[1]] == ""
    assert row[_POS6[2]] == "-2"
    assert row[_POS6[3]] == ""
    assert row[_POS6[4]] == "1"
    assert row[_POS6[5]] == ""


def test_dense_row_from_definition_includes_the_definitions_own_market_and_interval():
    definition = StrategyDefinition(
        market_key="SONIA", offsets=(0,), weights=(1.0,), interval=BarInterval.HOURLY,
    )
    row = dense_row_from_definition("SONIA Outright", definition, _POS6)

    assert row[MARKET_COLUMN] == "SONIA"
    assert row[INTERVAL_COLUMN] == "HOURLY"


def test_dense_row_from_definition_drops_offsets_beyond_available_positions():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1.0, -1.0), interval=BarInterval.DAILY,
    )
    narrow_columns = _POS6[:1]  # only 1 position column available
    row = dense_row_from_definition("Too Wide", definition, narrow_columns)
    assert row[narrow_columns[0]] == "1"
    # Label + Market + Interval + the one position column -- the
    # offset-1 leg is silently dropped, nothing else is.
    assert len(row) == 4


def test_grid_rows_from_strategy_set_includes_only_enabled_entries_in_order():
    strategy_set = StrategySet(
        name="Test Set",
        entries=(
            _entry(name="A", enabled=True),
            _entry(name="B", enabled=False),
            _entry(name="C", enabled=True),
        ),
    )
    rows = grid_rows_from_strategy_set(strategy_set, _POS6)
    assert [row[LABEL_COLUMN] for row in rows] == ["A", "C"]


def test_grid_rows_from_strategy_set_preserves_custom_names_verbatim():
    strategy_set = StrategySet(
        name="Butterflies",
        entries=(
            _entry(name="3M Double Butterfly", weights=(1.0, -3.0, 3.0, -1.0)),
            _entry(name="My SOFR Strategy", weights=(1.0, -1.0)),
        ),
    )
    rows = grid_rows_from_strategy_set(strategy_set, _POS6)
    assert rows[0][LABEL_COLUMN] == "3M Double Butterfly"
    assert rows[0][_POS6[0]] == "1"
    assert rows[0][_POS6[1]] == "-3"
    assert rows[0][_POS6[2]] == "3"
    assert rows[0][_POS6[3]] == "-1"
    assert rows[1][LABEL_COLUMN] == "My SOFR Strategy"


def test_grid_rows_from_strategy_set_preserves_each_entrys_own_market_and_interval():
    # The multi-market case: "Intermarket Churning"-style set spanning
    # three markets, each with its own interval too.
    strategy_set = StrategySet(
        name="Intermarket Churning",
        entries=(
            _entry(name="SOFR Fly", market_key="SOFR", interval=BarInterval.DAILY),
            _entry(name="SONIA Fly", market_key="SONIA", interval=BarInterval.DAILY),
            _entry(name="CORRA Fly", market_key="CORRA", interval=BarInterval.HOURLY),
        ),
    )
    rows = grid_rows_from_strategy_set(strategy_set, _POS6)

    by_name = {row[LABEL_COLUMN]: row for row in rows}
    assert by_name["SOFR Fly"][MARKET_COLUMN] == "SOFR"
    assert by_name["SOFR Fly"][INTERVAL_COLUMN] == "DAILY"
    assert by_name["SONIA Fly"][MARKET_COLUMN] == "SONIA"
    assert by_name["CORRA Fly"][MARKET_COLUMN] == "CORRA"
    assert by_name["CORRA Fly"][INTERVAL_COLUMN] == "HOURLY"


# ---------------------------------------------------------------------
# build_strategy_set_from_grid
# ---------------------------------------------------------------------

def _row(label, *weights, market_key=None, interval=None):
    row = {LABEL_COLUMN: label}
    if market_key is not None:
        row[MARKET_COLUMN] = market_key
    if interval is not None:
        row[INTERVAL_COLUMN] = interval.value if isinstance(interval, BarInterval) else interval
    row.update({col: "" for col in _POS6})
    for i, w in enumerate(weights):
        row[_POS6[i]] = str(w)
    return row


def test_build_strategy_set_from_grid_translates_rows_into_entries():
    rows = [_row("SOFR Fly", 1, -2, 1)]
    strategy_set = build_strategy_set_from_grid("My New Set", rows, _POS6, "SOFR", BarInterval.DAILY)

    assert strategy_set.name == "My New Set"
    assert len(strategy_set.entries) == 1
    entry = strategy_set.entries[0]
    assert entry.name == "SOFR Fly"
    assert entry.definition.market_key == "SOFR"
    assert entry.definition.offsets == (0, 1, 2)
    assert entry.definition.weights == (1.0, -2.0, 1.0)
    assert entry.definition.interval == BarInterval.DAILY
    assert entry.enabled is True


def test_build_strategy_set_from_grid_falls_back_to_passed_market_interval_when_row_lacks_them():
    # Rows without their own Market/Interval cells (e.g. a hand-built
    # row dict in a test, or any legacy caller) fall back to the
    # function's own market_key/interval parameters.
    rows = [_row("A", 1, -1), _row("B", 1, -2, 1)]
    strategy_set = build_strategy_set_from_grid("Set", rows, _POS6, "SONIA", BarInterval.HOURLY)
    for entry in strategy_set.entries:
        assert entry.definition.market_key == "SONIA"
        assert entry.definition.interval == BarInterval.HOURLY


def test_build_strategy_set_from_grid_preserves_each_rows_own_market_and_interval():
    # The critical multi-market round-trip guarantee: a row's OWN
    # Market/Interval cell wins over whatever the scan bar's top-level
    # selectors currently show -- saving never normalizes every row to
    # one market/interval.
    rows = [
        _row("SOFR Fly", 1, -2, 1, market_key="SOFR", interval=BarInterval.DAILY),
        _row("SONIA Fly", 1, -2, 1, market_key="SONIA", interval=BarInterval.DAILY),
        _row("CORRA Fly", 1, -2, 1, market_key="CORRA", interval=BarInterval.HOURLY),
    ]
    # Deliberately pass a THIRD market/interval as the "current scan
    # bar selection" -- it must not leak into any row that already had
    # its own values.
    strategy_set = build_strategy_set_from_grid("Intermarket Churning", rows, _POS6, "FED_FUNDS", BarInterval.FOUR_HOUR)

    by_name = {e.name: e for e in strategy_set.entries}
    assert by_name["SOFR Fly"].definition.market_key == "SOFR"
    assert by_name["SOFR Fly"].definition.interval == BarInterval.DAILY
    assert by_name["SONIA Fly"].definition.market_key == "SONIA"
    assert by_name["CORRA Fly"].definition.market_key == "CORRA"
    assert by_name["CORRA Fly"].definition.interval == BarInterval.HOURLY


def test_build_strategy_set_from_grid_skips_all_blank_rows():
    rows = [_row("Real Row", 1, -1), _row("Blank Row")]
    strategy_set = build_strategy_set_from_grid("Set", rows, _POS6, "SOFR", BarInterval.DAILY)
    assert len(strategy_set.entries) == 1
    assert strategy_set.entries[0].name == "Real Row"


def test_build_strategy_set_from_grid_raises_when_no_valid_rows():
    rows = [_row("Blank Row")]
    with pytest.raises(ValueError):
        build_strategy_set_from_grid("Empty Set", rows, _POS6, "SOFR", BarInterval.DAILY)


def test_build_strategy_set_from_grid_propagates_duplicate_label_error():
    rows = [_row("Same Name", 1, -1), _row("Same Name", 1, -2, 1)]
    with pytest.raises(ValueError):
        build_strategy_set_from_grid("Set", rows, _POS6, "SOFR", BarInterval.DAILY)


def test_build_strategy_set_from_grid_propagates_invalid_set_name():
    rows = [_row("A", 1, -1)]
    with pytest.raises(ValueError):
        build_strategy_set_from_grid("bad/name", rows, _POS6, "SOFR", BarInterval.DAILY)
