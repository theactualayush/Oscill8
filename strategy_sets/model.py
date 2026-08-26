"""
model.py

Domain model for Module 7A: a StrategySet is a named, user-defined,
serializable collection of StrategySetEntry objects, each wrapping one
reusable strategy_engine.StrategyDefinition plus the extra bookkeeping
a saved trading workflow needs on top of it (a human-facing name, an
enabled/disabled toggle, and per-entry candidate-filtering settings).

Naming note: the design brief describes each entry's fields as
"StrategyDefinition (market, name, ratio/weights, offsets, expansion
settings, enabled flag)". That is deliberately NOT implemented as a
second class literally named StrategyDefinition -- strategy_engine.
StrategyDefinition already is that name, project-wide, for the pure
shape (market_key, offsets, weights, interval, price_field), used
throughout strategy_engine/template_scanner/range_analytics. Reusing
the same name for a materially different, richer object in this
package would be a real footgun (ambiguous imports, "StrategyDefinition"
meaning two different things depending on which module you're reading).
Instead, this module composes the existing, unmodified
StrategyDefinition as StrategySetEntry.definition, and models the
brief's remaining fields (name, enabled, expansion settings) as
StrategySetEntry's own fields -- see StrategySetEntry below for the
exact field-by-field mapping.

Design correction (post-review): contract_start/contract_end do NOT
live here. A Strategy Set describes WHAT to scan -- it does not freeze
in WHEN to scan it. strategy_engine.combinations.generate_instances()
and template_scanner.scanner.ScanRequest both already treat the
contract-selection window as an execution-time parameter, shared
across every definition in one call/request, never baked into the
reusable shape object itself. Baking an absolute contract_start/
contract_end into a *saved, reused* StrategySetEntry would go stale
the moment "today" moves past it -- undermining the brief's own
reusability principle -- and would diverge from that established,
shared-window precedent for no compensating benefit. See expansion.py:
expand_strategy_set() now takes contract_start/contract_end as
call-time arguments, exactly like ScanRequest. `max_curve_position`/
`eligible_rics` remain per-entry in ExpansionSettings below, since
those genuinely are strategy-shape/liquidity-dependent (a 12-leg curve
and a 3-leg fly, or two different markets, can legitimately want
different curve-position/eligibility filters even under the same
shared scan window) -- not a calendar concept, so staleness doesn't
apply to them the same way.

A StrategySet knows nothing about StrategyInstance, ScanRequest, LSEG,
or the database -- expansion.py is the only bridge to
strategy_engine/template_scanner, and the scanner itself never imports
this package at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition

# Filesystem-safe by construction: StrategySetRepository uses a
# StrategySet's own `name` directly as a JSON filename (see
# repository.py's module docstring for the exact invariant this buys),
# so this pattern excludes '/', '\', '.', and every other
# path-meaningful character.
_SET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,79}$")


@dataclass(frozen=True)
class ExpansionSettings:
    """Per-entry candidate-filtering settings -- the two optional
    post-filters template_scanner.universe.generate_candidates already
    supports (`max_curve_position`, `eligible_rics`), passed straight
    through unchanged at expansion time.

    Deliberately does NOT include a contract window -- see the module
    docstring's "Design correction" note for why contract_start/
    contract_end are supplied at expand_strategy_set() call time
    instead, shared across the whole expansion the same way
    ScanRequest already shares one window across all its definitions.

    Both fields are optional, so a StrategySetEntry that needs no
    filtering at all can omit `expansion` entirely (see
    StrategySetEntry.expansion's default below).
    """

    max_curve_position: int | None = None
    eligible_rics: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.max_curve_position is not None and self.max_curve_position < 0:
            raise ValueError(f"max_curve_position must be >= 0, got {self.max_curve_position}")

        if self.eligible_rics is not None:
            eligible = tuple(self.eligible_rics)
            if not eligible:
                raise ValueError("eligible_rics, if given, must not be empty")
            if not all(isinstance(r, str) and r for r in eligible):
                raise ValueError("eligible_rics must be a collection of non-empty strings")
            object.__setattr__(self, "eligible_rics", eligible)


@dataclass(frozen=True)
class StrategySetEntry:
    """One named, individually enable-able strategy within a
    StrategySet -- e.g. "SOFR 6M Fly" inside the "6M Strategies" set.

    Field-by-field mapping to the design brief's "StrategyDefinition"
    description (see the module docstring for why this is a distinct
    class rather than reusing that exact name):
        market             -> definition.market_key
        name               -> name
        ratio / weights    -> definition.weights
        offsets            -> definition.offsets
        expansion settings -> expansion (curve-position/eligibility
                               filters only -- NOT a contract window,
                               see the module docstring)
        enabled flag       -> enabled
    """

    name: str
    definition: StrategyDefinition
    expansion: ExpansionSettings = field(default_factory=ExpansionSettings)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(
                f"StrategySetEntry name must be a non-empty string, got {self.name!r}"
            )
        if not isinstance(self.definition, StrategyDefinition):
            raise TypeError(
                "StrategySetEntry.definition must be a StrategyDefinition, "
                f"got {type(self.definition)}"
            )
        if not isinstance(self.expansion, ExpansionSettings):
            raise TypeError(
                "StrategySetEntry.expansion must be an ExpansionSettings, "
                f"got {type(self.expansion)}"
            )


@dataclass(frozen=True)
class IntermarketStrategySetEntry:
    """One named, individually enable-able INTERMARKET strategy within a
    StrategySet -- the additive sibling to StrategySetEntry above, for
    an entry whose legs belong to different markets (strategy_engine.
    intermarket_definitions.IntermarketDefinition) rather than one
    market's own curve (strategy_engine.definitions.StrategyDefinition).

    Field shape deliberately mirrors StrategySetEntry exactly (name,
    definition, expansion, enabled) so both entry types can be handled
    uniformly wherever that's possible (e.g. name-uniqueness checking
    below) and diverge only where the underlying definition genuinely
    requires it.

    `expansion.max_curve_position` is NOT supported here and is
    rejected at construction: "curve position" is a single-market-curve
    concept (see template_scanner.universe.generate_candidates) with no
    well-defined intermarket equivalent (an intermarket instance has no
    one shared curve to measure a position on) -- silently ignoring a
    trader-set filter would be worse than rejecting it outright.
    `expansion.eligible_rics` IS supported (see strategy_sets.expansion.
    expand_strategy_set()) since it needs no curve-position concept at
    all, applying identically to any instance type via its `.rics`.
    """

    name: str
    definition: IntermarketDefinition
    expansion: ExpansionSettings = field(default_factory=ExpansionSettings)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(
                f"IntermarketStrategySetEntry name must be a non-empty string, got {self.name!r}"
            )
        if not isinstance(self.definition, IntermarketDefinition):
            raise TypeError(
                "IntermarketStrategySetEntry.definition must be an IntermarketDefinition, "
                f"got {type(self.definition)}"
            )
        if not isinstance(self.expansion, ExpansionSettings):
            raise TypeError(
                "IntermarketStrategySetEntry.expansion must be an ExpansionSettings, "
                f"got {type(self.expansion)}"
            )
        if self.expansion.max_curve_position is not None:
            raise ValueError(
                "IntermarketStrategySetEntry.expansion.max_curve_position is not "
                "supported -- 'curve position' has no well-defined meaning for a "
                "strategy whose legs span different markets/curves"
            )


@dataclass(frozen=True)
class StrategySet:
    """A named, ordered collection of StrategySetEntry objects
    representing one trading workflow -- e.g. "Churning", "6M
    Strategies", "Medium Vol". A Strategy Set is simply a saved scan
    configuration: no folder/market/template-hierarchy concept, and no
    restriction on which markets or shapes its entries mix (a set can
    freely combine SOFR/SONIA/CORRA/etc. entries -- each still expands
    independently on its own market's curve, see expansion.py).

    `name` is both a human-facing label and the set's identity for
    StrategySetRepository persistence (one JSON file per name) -- see
    repository.py. Entry `name`s must be unique within one set (two
    entries named identically inside the same StrategySet would make
    "which one do you mean" ambiguous for a future rename/duplicate/
    editor UI operating on entries by name) -- uniqueness is checked
    across `entries` AND `intermarket_entries` TOGETHER, one shared
    namespace, since a trader thinks of them as one flat list of named
    strategies in the set (see the module's own JSON schema, where both
    entry shapes live in the same `entries` array).

    `intermarket_entries` (additive sibling to `entries`, defaults to an
    empty tuple so every pre-existing single-market-only StrategySet
    construction is completely unaffected) holds any entries whose legs
    span different markets. A StrategySet may contain `entries` only,
    `intermarket_entries` only, or a genuine mix of both -- there is no
    requirement to keep single-market and intermarket strategies in
    separate sets.
    """

    name: str
    entries: tuple[StrategySetEntry, ...]
    intermarket_entries: tuple[IntermarketStrategySetEntry, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SET_NAME_PATTERN.match(self.name):
            raise ValueError(
                "StrategySet name must be 1-80 characters, start with a "
                "letter or digit, and contain only letters, digits, spaces, "
                f"'-', or '_' -- got {self.name!r}"
            )

        entries = tuple(self.entries)
        intermarket_entries = tuple(self.intermarket_entries)

        if len(entries) + len(intermarket_entries) < 1:
            raise ValueError(
                "A StrategySet needs at least 1 entry (StrategySetEntry or "
                "IntermarketStrategySetEntry)"
            )
        if not all(isinstance(e, StrategySetEntry) for e in entries):
            raise TypeError("StrategySet.entries must contain only StrategySetEntry instances")
        if not all(isinstance(e, IntermarketStrategySetEntry) for e in intermarket_entries):
            raise TypeError(
                "StrategySet.intermarket_entries must contain only "
                "IntermarketStrategySetEntry instances"
            )

        names = [e.name for e in entries] + [e.name for e in intermarket_entries]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"StrategySet entry names must be unique within a set, duplicated: {duplicates}"
            )

        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "intermarket_entries", intermarket_entries)
