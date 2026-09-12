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

Composite Strategy Sets (additive, Phase 1 of the Group A/Group B
design): StrategyGroup and StrategyGroupPair below add the ability for
one StrategySet to describe a "Group A x Group B" pairing -- each group
REFERENCING one existing source StrategySet by name plus the explicit,
ordered selection of that set's entry names. This is a pure
data-model/persistence foundation: nothing here (or anywhere else in
this package) generates the A x B combinations, which are the future
combination engine's job and are never stored. `StrategySet.groups`
defaults to None, so every StrategySet that existed before this field
is completely unaffected.

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
class StrategyGroup:
    """ONE side of a composite StrategySet's "Group A x Group B" pairing:
    a reference to exactly one source StrategySet, plus the explicit,
    ordered list of that set's entry names the trader actually selected.

    Reference, not a copy: `source_set_name` names an existing saved
    StrategySet (StrategySetRepository's own identity -- one JSON file
    per name, see repository.py), rather than duplicating that set's
    full StrategyDefinition shapes here. The selected strategies are
    likewise identified by their entry `name`, which StrategySet
    already guarantees is unique within one set across `entries` AND
    `intermarket_entries` together -- so a name unambiguously
    identifies at most one strategy in the source set, of either type.

    `selected_entry_names` is the persisted SELECTION ITSELF, never a
    "select all" flag: selecting every strategy in a source set stores
    every one of those names explicitly. This is deliberate and is the
    whole point of the field -- a saved composite set must keep meaning
    what it meant when it was saved, so a strategy later ADDED to the
    source set never silently joins an already-saved selection. (A
    strategy later REMOVED from, or renamed in, the source set leaves a
    selected name that no longer resolves; detecting/reporting that is
    a resolution-time concern for the future combination engine, not a
    model-level one -- this model layer never reads the filesystem, so
    it cannot and must not check whether a referenced set or entry
    actually exists. Compare ExpansionSettings.eligible_rics, which is
    likewise validated structurally here and only ever resolved against
    real data further down the pipeline.)

    An EMPTY `selected_entry_names` is valid -- "this group's source is
    chosen, but nothing in it is selected yet" is a real, representable
    state (the design brief's "clear the selection"), not an error.
    """

    source_set_name: str
    selected_entry_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_set_name, str) or not _SET_NAME_PATTERN.match(
            self.source_set_name
        ):
            raise ValueError(
                "StrategyGroup source_set_name must be a valid StrategySet name "
                "(1-80 characters, starting with a letter or digit, containing "
                "only letters, digits, spaces, '-', or '_') -- got "
                f"{self.source_set_name!r}"
            )

        if isinstance(self.selected_entry_names, str):
            raise TypeError(
                "StrategyGroup selected_entry_names must be a collection of entry "
                "names, not a single string -- got "
                f"{self.selected_entry_names!r}"
            )

        selected = tuple(self.selected_entry_names)
        if not all(isinstance(n, str) and n.strip() for n in selected):
            raise ValueError(
                "StrategyGroup selected_entry_names must all be non-empty strings, "
                f"got {list(selected)!r}"
            )

        duplicates = sorted({n for n in selected if selected.count(n) > 1})
        if duplicates:
            raise ValueError(
                "StrategyGroup selected_entry_names must be unique within a group "
                f"(an entry name identifies at most one strategy), duplicated: {duplicates}"
            )

        object.__setattr__(self, "selected_entry_names", selected)


@dataclass(frozen=True)
class StrategyGroupPair:
    """The composite ("grouped") configuration of a StrategySet: Group A,
    and OPTIONALLY Group B.

    Order is meaningful and is part of the model, not a presentation
    detail: the future combination engine is defined as

        Group A x Group B

    so `group_a` and `group_b` are separate, individually-named fields
    rather than a positional list -- the two sides stay distinguishable
    no matter how they are stored, read, or displayed.

    `group_b` is optional (None). A pair with only `group_a` is the
    ordinary, single-sided case; it is NOT a degenerate or invalid
    state. `group_a`, in contrast, is mandatory: "Group B but no Group
    A" has no meaning under an A x B definition, so it is rejected
    structurally (group_a is simply required) rather than by a
    cross-field check.

    Deliberately holds NO combination results: the A x B expansion is
    generated dynamically by a later phase's combination engine and is
    never stored here (same principle as the contract window, which is
    an expand_strategy_set() call-time argument rather than saved
    state -- see the module docstring's "Design correction" note).
    """

    group_a: StrategyGroup
    group_b: StrategyGroup | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.group_a, StrategyGroup):
            raise TypeError(
                f"StrategyGroupPair.group_a must be a StrategyGroup, got {type(self.group_a)}"
            )
        if self.group_b is not None and not isinstance(self.group_b, StrategyGroup):
            raise TypeError(
                "StrategyGroupPair.group_b must be a StrategyGroup or None, "
                f"got {type(self.group_b)}"
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

    `groups` (optional, defaults to None) is the COMPOSITE
    configuration: Group A, and optionally Group B, each referencing
    one source StrategySet plus the strategies selected from it (see
    StrategyGroupPair/StrategyGroup above). It is purely additive and
    entirely orthogonal to `entries`/`intermarket_entries` -- a
    StrategySet may have neither (every set that existed before this
    field did, unchanged), only entries, only groups, or both. Nothing
    in this package expands or combines `groups`: the "Group A x Group
    B" combination engine is a later phase, and expand_strategy_set()
    (expansion.py) deliberately still rolls only `entries`/
    `intermarket_entries`, exactly as it did before this field existed.

    The at-least-one-entry rule is relaxed accordingly: a set carrying
    ONLY a `groups` configuration (no entries of either kind) is valid,
    since its content genuinely is the grouped selection. A set with
    neither entries nor groups is still rejected -- that is empty, not
    composite.
    """

    name: str
    entries: tuple[StrategySetEntry, ...]
    intermarket_entries: tuple[IntermarketStrategySetEntry, ...] = ()
    description: str = ""
    groups: StrategyGroupPair | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SET_NAME_PATTERN.match(self.name):
            raise ValueError(
                "StrategySet name must be 1-80 characters, start with a "
                "letter or digit, and contain only letters, digits, spaces, "
                f"'-', or '_' -- got {self.name!r}"
            )

        entries = tuple(self.entries)
        intermarket_entries = tuple(self.intermarket_entries)

        if self.groups is not None and not isinstance(self.groups, StrategyGroupPair):
            raise TypeError(
                f"StrategySet.groups must be a StrategyGroupPair or None, got {type(self.groups)}"
            )

        if len(entries) + len(intermarket_entries) < 1 and self.groups is None:
            raise ValueError(
                "A StrategySet needs at least 1 entry (StrategySetEntry or "
                "IntermarketStrategySetEntry), or a `groups` configuration"
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
