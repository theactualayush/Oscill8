"""
strategy_set_formatting.py

Pure helper functions bridging the Strategy Templates grid (ui.controls)
and strategy_sets.StrategySet -- translating a StrategySet's entries
into grid rows (loading), and building a new StrategySet directly from
the grid's current rows (saving).

Design principle (Module 7B simplification): "Strategy Templates is
the working strategy grid; a Strategy Set is simply a saved named
version of that grid." There is exactly ONE editable strategy surface
(the grid) -- a Strategy Set is not a second, richer editing model.

Multi-market/multi-interval fix: a Strategy Set is explicitly allowed
to mix markets (e.g. "Intermarket Churning": SOFR + SONIA + CORRA
entries) and each entry keeps its own interval. An earlier version of
this module bound the WHOLE grid to one scan-bar-selected market_key/
interval, which meant loading a mixed set and saving it again would
silently normalize every row to a single market/interval -- corrupting
the saved file. Fixed by giving the grid its OWN per-row Market/
Interval columns (see ui.controls' column_config and ui.formatting.
MARKET_COLUMN/INTERVAL_COLUMN/build_definitions_from_grid, which now
prefers a row's own Market/Interval over any grid-wide default) --
every entry's market_key/interval, offsets, weights, and name now
round-trip losslessly through load -> edit -> save -> reload, whether
the set is single- or multi-market. There is no more "mixed markets
can't be represented" case to warn about.

No Streamlit import here -- unit-testable directly against plain data,
the same convention ui.formatting follows for Module 6A. No shape/
weight validation is duplicated: row translation routes through the
existing, unmodified ui.formatting.build_definitions_from_grid()/
template_from_dense_weights()/StrategyDefinition, and StrategySet's own
validation (unique entry names, >=1 entry) is never re-implemented --
callers see it surface as a ValueError from strategy_sets.model
unchanged.
"""

from __future__ import annotations

from typing import Sequence

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition

from strategy_sets.model import StrategySet, StrategySetEntry

from ui.formatting import INTERVAL_COLUMN, LABEL_COLUMN, MARKET_COLUMN, build_definitions_from_grid


def format_grid_weight(value: float) -> str:
    """Render a weight as the grid's own TextColumn would want it typed
    -- "1" for a whole number (not "1.0"), "1.5" otherwise."""
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def dense_row_from_definition(
    entry_name: str, definition: StrategyDefinition, position_columns: Sequence[str]
) -> dict:
    """One grid row (Label + Market + Interval + position columns)
    reproducing `definition` exactly -- the inverse of ui.formatting.
    build_definitions_from_grid()'s row-to-StrategyDefinition
    translation (itself template_scanner.templates.
    template_from_dense_weights()). market_key/interval are carried on
    the row itself (not a grid-wide default), which is what lets a
    multi-market entry round-trip correctly alongside others.

    Offsets beyond what `position_columns` can hold are silently
    dropped (a StrategySet entry with more legs than the grid's current
    "Positions" count) -- the caller's grid still loads with whatever
    fits; growing "Positions" before loading avoids this entirely.
    """
    row = {
        LABEL_COLUMN: entry_name,
        MARKET_COLUMN: definition.market_key,
        INTERVAL_COLUMN: definition.interval.value,
    }
    row.update({col: "" for col in position_columns})
    for offset, weight in zip(definition.offsets, definition.weights):
        if offset < len(position_columns):
            row[position_columns[offset]] = format_grid_weight(weight)
    return row


def grid_rows_from_strategy_set(strategy_set: StrategySet, position_columns: Sequence[str]) -> list[dict]:
    """Enabled entries of `strategy_set`, translated into grid rows, in
    entry order -- each row carrying its OWN Market/Interval (see the
    module docstring), so a set mixing markets loads correctly into the
    one grid. Disabled entries are omitted -- the unified grid has no
    enabled/disabled toggle of its own, so "disabled" is preserved by
    simply not loading that entry into the working grid (it still
    exists, disabled, in the saved file; a resave from the grid without
    it will drop it, which is the expected consequence of editing a
    richer saved set through this simplified surface).
    """
    return [
        dense_row_from_definition(entry.name, entry.definition, position_columns)
        for entry in strategy_set.entries
        if entry.enabled
    ]


def build_strategy_set_from_grid(
    name: str,
    grid_rows: Sequence[dict],
    position_columns: Sequence[str],
    market_key: str,
    interval: BarInterval,
) -> StrategySet:
    """Snapshot the grid's current rows into a new StrategySet named
    `name`. Each entry's market_key/interval comes from that ROW's own
    Market/Interval cell (see ui.formatting.build_definitions_from_grid,
    which now resolves per-row first) -- `market_key`/`interval` here
    are only the fallback for a row that somehow lacks its own values
    (e.g. a hand-built row dict in a test). Row translation/validation
    is entirely build_definitions_from_grid()'s (offsets/weights,
    market, interval, price_field) -- never duplicated here.

    Raises:
        ValueError: a row's shape is rejected by StrategyDefinition's
            own validation (surfaced with that row's label), no row had
            a nonzero weight at all, or StrategySet's own validation
            rejects the result (invalid name, duplicate row labels).
    """
    results = build_definitions_from_grid(grid_rows, position_columns, market_key, interval)
    errors = [r for r in results if r.error is not None]
    if errors:
        raise ValueError("; ".join(f"{r.label}: {r.error}" for r in errors))

    definitions = [(r.label, r.definition) for r in results if r.definition is not None]
    if not definitions:
        raise ValueError("Add at least one strategy row with a nonzero ratio before saving.")

    entries = tuple(StrategySetEntry(name=label, definition=definition) for label, definition in definitions)
    return StrategySet(name=name, entries=entries)


__all__ = [
    "format_grid_weight",
    "dense_row_from_definition",
    "grid_rows_from_strategy_set",
    "build_strategy_set_from_grid",
]
