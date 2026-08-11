"""
strategy_set_formatting.py

Pure helper functions for Module 7B's Strategy Set panel: translating a
StrategySet's entries into the "Strategies in Set" display table
(Enabled/Name/Market/Structure/Weights), a leg-count-only structure
label (Outright/Spread/Fly/Condor/Curve -- purely informational, never
a shape-classification system: Strategy Set/entry names stay entirely
user-defined per the design brief), applying edited Enabled values back
onto a draft, and building one new StrategySetEntry from the same
curve-position grid shape ui.controls' Strategy Templates grid already
uses.

No Streamlit import here -- unit-testable directly against plain data,
the same convention ui.formatting follows for Module 6A. No shape/
weight validation is duplicated: entry construction routes through the
existing, unmodified ui.formatting.build_definitions_from_grid() (which
itself routes through template_scanner.templates.
template_from_dense_weights()/StrategyDefinition), and StrategySet/
StrategySetEntry's own validation (unique names, etc.) is never
re-implemented -- callers see it surface as a ValueError from
strategy_sets.model unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from core.config import MARKETS, BarInterval

from strategy_sets.model import StrategySetEntry

from ui.formatting import build_definitions_from_grid, fmt_number

_STRUCTURE_BY_LEG_COUNT = {1: "Outright", 2: "Spread", 3: "Fly", 4: "Condor"}

ENABLED_COLUMN = "Enabled"
NAME_COLUMN = "Name"
MARKET_COLUMN = "Market"
STRUCTURE_COLUMN = "Structure"
WEIGHTS_COLUMN = "Weights"

ENTRY_TABLE_COLUMNS: tuple[str, ...] = (
    ENABLED_COLUMN,
    NAME_COLUMN,
    MARKET_COLUMN,
    STRUCTURE_COLUMN,
    WEIGHTS_COLUMN,
)


def describe_structure(weights: Sequence[float]) -> str:
    """Trader-familiar structure label from leg count alone -- 1 leg =
    Outright, 2 = Spread, 3 = Fly, 4 = Condor, 5+ = Curve. Never
    inspects the actual weight values (e.g. does not try to detect
    whether a 3-legger is a "genuine" fly vs. some other custom
    weighting) -- this is purely an informational label for the entries
    table, not a shape-classification system.
    """
    return _STRUCTURE_BY_LEG_COUNT.get(len(weights), "Curve")


def format_weights(weights: Sequence[float]) -> str:
    """"1.00 / -2.00 / 1.00" -- same number formatting ui.formatting
    already uses for the scanner result grid's Ratio column."""
    return " / ".join(fmt_number(w, 2) for w in weights)


def entries_to_rows(entries: Sequence[StrategySetEntry]) -> list[dict]:
    """One display row per entry, in the SAME order as `entries` --
    Enabled/Name/Market/Structure/Weights. Row order/position matches
    `entries` exactly, so a caller pairing an edited row back to
    `entries[i]` (see apply_enabled_edits below) can rely on positional
    correspondence without a name-based lookup.
    """
    rows = []
    for entry in entries:
        definition = entry.definition
        rows.append(
            {
                ENABLED_COLUMN: entry.enabled,
                NAME_COLUMN: entry.name,
                MARKET_COLUMN: MARKETS[definition.market_key].name,
                STRUCTURE_COLUMN: describe_structure(definition.weights),
                WEIGHTS_COLUMN: format_weights(definition.weights),
            }
        )
    return rows


def apply_enabled_edits(
    entries: Sequence[StrategySetEntry], edited_rows: Sequence[dict]
) -> list[StrategySetEntry]:
    """Rebuild `entries` with each entry's `enabled` flag taken from the
    matching (by position) edited row's Enabled cell -- the only field
    the entries table's data_editor allows editing; Name/Market/
    Structure/Weights are read-only there and are never written back.

    Returns a NEW list (StrategySetEntry is frozen, via
    dataclasses.replace); `entries` itself is left untouched. If the
    row count doesn't match `entries` (e.g. a stale rerun mid-edit),
    returns `entries` unchanged rather than misaligning positions.
    """
    if len(entries) != len(edited_rows):
        return list(entries)
    return [
        replace(entry, enabled=bool(row.get(ENABLED_COLUMN, entry.enabled)))
        for entry, row in zip(entries, edited_rows)
    ]


def entry_names(entries: Sequence[StrategySetEntry]) -> list[str]:
    return [e.name for e in entries]


def remove_entry_by_name(entries: Sequence[StrategySetEntry], name: str) -> list[StrategySetEntry]:
    """Drop the entry named `name` (a no-op if no entry has that name)."""
    return [e for e in entries if e.name != name]


def build_entry_from_grid_row(
    row: dict,
    position_columns: Sequence[str],
    market_key: str,
    interval: BarInterval,
    entry_name: str,
) -> StrategySetEntry:
    """Translate one curve-position grid row -- the same row shape
    ui.controls' Strategy Templates grid produces -- into a new
    StrategySetEntry, via the existing, unmodified
    ui.formatting.build_definitions_from_grid(). Shape/weight
    validation is never duplicated here.

    Raises:
        ValueError: the row is all-zero/blank (no strategy to add), or
            the resulting shape is rejected by StrategyDefinition's own
            validation (non-increasing offsets, all-zero weights, ...).
    """
    results = build_definitions_from_grid([row], position_columns, market_key, interval)
    if not results:
        raise ValueError("Enter at least one nonzero curve position before adding.")

    result = results[0]
    if result.error is not None:
        raise ValueError(result.error)

    name = entry_name.strip() or result.label
    return StrategySetEntry(name=name, definition=result.definition)
