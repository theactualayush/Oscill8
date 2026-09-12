"""
intermarket_formatting.py

Pure translation between a StrategySet's INTERMARKET entries
(strategy_sets.model.IntermarketStrategySetEntry, Module 9) and the
read-only display rows the Strategy Set panel renders for them.

Why this module exists: Module 7B's Strategy Templates grid is
single-market only -- it composes ordinary StrategySetEntry/
StrategyDefinition rows and has no representation for an entry whose
legs span different markets. Loading a Strategy Set that carries
`intermarket_entries` therefore used to show the trader NOTHING for
those entries at all: the file was neither corrupted nor the entries
lost, but from the trader's seat that is indistinguishable from data
loss. This module is the smallest useful fix for that specific gap --
VISIBILITY, not editability.

Read-only by design, and deliberately so:
  * There is no grid-row/edit translation here, and no inverse of these
    functions -- nothing in ui/ can construct or modify an
    IntermarketStrategySetEntry. Hand-editing a Strategy Set's JSON
    file (or a script) remains the only authoring route, exactly as
    Module 9 documents.
  * `market_label` is strategy_engine.intermarket_definitions.
    resolve_display_market_key()'s COSMETIC composite label (e.g.
    "SOFR/CORRA"). Module 9's rule applies verbatim: a value produced
    here is for display only and must never reach provider resolution
    (core.providers.resolve_provider), a cache/database key, a
    QuantHub/LSEG instrument mapping, a core.config market-registry
    lookup, or a bp conversion. Each LegSpec's own `market_key` is
    reported per leg (see leg_display_rows) precisely so a trader can
    still see the real, authoritative per-leg markets rather than only
    the composite.

No Streamlit import here -- unit-testable directly against plain data,
the same convention ui.formatting and ui.strategy_set_formatting
already follow (Module 6A/7B). Weight rendering reuses ui.
strategy_set_formatting.format_grid_weight() rather than
re-implementing "1" vs "1.0" formatting a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from strategy_engine.intermarket_definitions import (
    IntermarketDefinition,
    resolve_display_market_key,
)

from strategy_sets.model import IntermarketStrategySetEntry, StrategySet

from ui.strategy_set_formatting import format_grid_weight

# Column labels of the per-entry read-only leg table. Deliberately NOT
# ui.formatting's LABEL_COLUMN/MARKET_COLUMN/INTERVAL_COLUMN constants:
# those name the EDITABLE grid's columns, and reusing them here would
# suggest these rows can be fed back into that grid (they cannot -- an
# intermarket entry has no single-market grid representation).
LEG_COLUMN = "Leg"
LEG_MARKET_COLUMN = "Market"
LEG_OFFSET_COLUMN = "Offset"
LEG_WEIGHT_COLUMN = "Weight"

LEG_COLUMNS: tuple[str, ...] = (
    LEG_COLUMN,
    LEG_MARKET_COLUMN,
    LEG_OFFSET_COLUMN,
    LEG_WEIGHT_COLUMN,
)

SECTION_CAPTION = "INTERMARKET STRATEGIES (READ-ONLY)"

LEG_OFFSET_HELP = (
    "Each leg's offset is a position on THAT leg's own contract curve, "
    "counted from the anchor period -- not a position on one shared curve."
)


@dataclass(frozen=True)
class IntermarketLegDisplay:
    """One leg of an intermarket entry, already formatted for display.

    `market_key` is the leg's REAL, authoritative market key (from its
    own LegSpec) -- not a composite display label. It is still display
    output here; nothing downstream of this module resolves providers
    or cache keys from it.
    """

    leg_number: int
    market_key: str
    offset: int
    weight: str

    def as_row(self) -> dict:
        """This leg as a plain dict keyed by LEG_COLUMNS, ready for a
        read-only table."""
        return {
            LEG_COLUMN: self.leg_number,
            LEG_MARKET_COLUMN: self.market_key,
            LEG_OFFSET_COLUMN: self.offset,
            LEG_WEIGHT_COLUMN: self.weight,
        }


@dataclass(frozen=True)
class IntermarketEntryDisplay:
    """One IntermarketStrategySetEntry, flattened into exactly what the
    read-only panel shows: the entry's own name/enabled flag, its
    definition's interval/price field/optional bp override, a cosmetic
    composite market label, and one IntermarketLegDisplay per leg."""

    name: str
    enabled: bool
    market_label: str
    interval: str
    price_field: str
    bp_per_point: float | None
    legs: tuple[IntermarketLegDisplay, ...]

    @property
    def leg_rows(self) -> list[dict]:
        """The leg table as plain dicts, in leg order."""
        return [leg.as_row() for leg in self.legs]


def leg_display_rows(definition: IntermarketDefinition) -> tuple[IntermarketLegDisplay, ...]:
    """Every leg of `definition`, in leg order, 1-indexed for display.

    Leg order is preserved exactly as authored -- never sorted by
    offset, market, or weight: an IntermarketDefinition's legs may
    legitimately repeat an offset across markets (see LegSpec), so leg
    order is the only stable way to refer to a specific leg.
    """
    return tuple(
        IntermarketLegDisplay(
            leg_number=index,
            market_key=leg.market_key,
            offset=leg.offset,
            weight=format_grid_weight(leg.weight),
        )
        for index, leg in enumerate(definition.legs, start=1)
    )


def entry_display(entry: IntermarketStrategySetEntry) -> IntermarketEntryDisplay:
    """One saved intermarket entry, translated for read-only display.

    `market_label` is resolve_display_market_key()'s composite label --
    see the module docstring's DISPLAY-ONLY caveat.
    """
    definition = entry.definition
    return IntermarketEntryDisplay(
        name=entry.name,
        enabled=entry.enabled,
        market_label=resolve_display_market_key(definition),
        interval=definition.interval.value,
        price_field=definition.price_field,
        bp_per_point=definition.bp_per_point,
        legs=leg_display_rows(definition),
    )


def entry_displays(strategy_set: StrategySet | None) -> list[IntermarketEntryDisplay]:
    """Every intermarket entry of `strategy_set`, in saved order.

    Empty for None (nothing loaded / "+ New Strategy Set") and for a
    set with only single-market entries -- which is what keeps the
    panel completely absent, and the existing single-market experience
    byte-for-byte unchanged, in the ordinary case.

    Unlike ui.strategy_set_formatting.grid_rows_from_strategy_set(),
    DISABLED entries are included: the grid omits a disabled entry
    because re-saving the grid legitimately drops it, but this panel
    only ever displays -- hiding a disabled entry here would recreate
    the exact invisibility problem this module exists to fix.
    """
    if strategy_set is None:
        return []
    return [entry_display(entry) for entry in strategy_set.intermarket_entries]


def has_intermarket_entries(strategy_set: StrategySet | None) -> bool:
    """Whether `strategy_set` carries any intermarket entries at all --
    the one condition the read-only panel renders under."""
    return bool(entry_displays(strategy_set))


def entry_summary_line(display: IntermarketEntryDisplay) -> str:
    """One compact metadata line for an entry: composite market label,
    leg count, interval, price field, and the explicit bp_per_point
    override if (and only if) one is set -- an unset override is a real,
    meaningful state (bp-derived metrics are left NaN, see Module 9),
    so it is reported explicitly rather than omitted."""
    leg_count = len(display.legs)
    leg_word = "leg" if leg_count == 1 else "legs"
    parts = [
        display.market_label,
        f"{leg_count} {leg_word}",
        display.interval,
        display.price_field,
    ]
    if display.bp_per_point is None:
        parts.append("bp/point not set")
    else:
        parts.append(f"bp/point {display.bp_per_point:g}")
    return "  ·  ".join(parts)


def entry_status_label(display: IntermarketEntryDisplay) -> str:
    """"Enabled"/"Disabled" -- the entry's own saved flag, shown as-is
    and never toggleable from this panel."""
    return "Enabled" if display.enabled else "Disabled"


def panel_title(displays: Sequence[IntermarketEntryDisplay]) -> str:
    """The read-only panel's own heading, including how many entries it
    covers."""
    count = len(displays)
    noun = "strategy" if count == 1 else "strategies"
    return f"🔗 {count} intermarket {noun} — read-only"


def intermarket_notice(strategy_set: StrategySet | None) -> str | None:
    """The one-line explanation of why these entries are not in the
    editable grid above -- None when there is nothing to explain.

    This is the whole point of the panel: without it, an intermarket
    entry's absence from the grid is silently indistinguishable from
    data loss.
    """
    displays = entry_displays(strategy_set)
    if not displays:
        return None
    count = len(displays)
    noun = "strategy" if count == 1 else "strategies"
    verb = "is" if count == 1 else "are"
    return (
        f"This Strategy Set also contains {count} intermarket {noun} "
        f"(legs spanning different markets). They {verb} shown read-only here "
        "because the editable grid above is single-market only. Saving this "
        "Strategy Set preserves them exactly as loaded; editing them means "
        "hand-editing the set's JSON file."
    )


__all__ = [
    "LEG_COLUMN",
    "LEG_MARKET_COLUMN",
    "LEG_OFFSET_COLUMN",
    "LEG_WEIGHT_COLUMN",
    "LEG_COLUMNS",
    "SECTION_CAPTION",
    "LEG_OFFSET_HELP",
    "IntermarketLegDisplay",
    "IntermarketEntryDisplay",
    "leg_display_rows",
    "entry_display",
    "entry_displays",
    "has_intermarket_entries",
    "entry_summary_line",
    "entry_status_label",
    "panel_title",
    "intermarket_notice",
]
