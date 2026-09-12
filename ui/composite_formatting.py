"""
composite_formatting.py

Pure translation between the composite ("Group A x Group B") authoring
panel's widget values and strategy_sets.model's StrategyGroup/
StrategyGroupPair -- the last missing piece of the composite feature,
whose model/persistence (Phase 1), composition (Phase 2), structural-
zero filtering (Phase 3), and execution wiring (Phase 4) are all
already implemented and unmodified by this layer.

Scope, deliberately narrow: this module AUTHORS a group CONFIGURATION
and nothing else. It never resolves a group against its source
Strategy Sets, never forms an A x B pair, never composes an
IntermarketDefinition, never applies structural-zero filtering, and
never generates, dedupes, or prices a candidate. Every one of those is
strategy_sets.composite's job and is reached only through the existing
execution path (ui.scan_view -> strategy_sets.execution.
run_strategy_set -> expansion -> composite). Cartesian-product results
are therefore never built here and never persisted -- exactly as
strategy_sets/model.py's own docstring requires.

No Streamlit import here -- unit-testable directly against plain data,
the same convention ui.formatting, ui.strategy_set_formatting and
ui.intermarket_formatting already follow.

Three rules this module exists to enforce, all of them structural
rather than advisory:

  * NO NESTED COMPOSITES. A Strategy Set that itself carries `groups`
    is never offered as a group source (see eligible_source_names) --
    strategy_sets.composite.resolve_group_entries() rejects one at
    resolution time with a CompositeResolutionError, and this is the
    UI-side half of that same rule: a trader cannot pick one in the
    first place. The set currently being edited is likewise excluded
    (a composite cannot source itself).

  * SELECTION ORDER IS THE USER'S. `selected_entry_names` is stored
    exactly as selected -- clamp_selection() only ever DROPS names, and
    preserves the order of what survives. Nothing here sorts a
    selection, and Group A / Group B are never reordered relative to
    each other (the backend's unordered-pair dedup handles reverse
    duplicates; see strategy_sets.composite.pair_identity).

  * A SAVED SELECTION IS PRESERVED, NEVER SILENTLY REPAIRED. Two
    separate things can go stale between saving a composite and opening
    it again: the source Strategy Set itself can disappear (or become a
    composite), and an individual selected strategy can be removed from
    a source set that still exists. Neither is quietly fixed here. An
    unresolvable SOURCE is returned unchanged, by reference (see
    resolve_source_state / SourceState.available); a stale selected
    NAME stays in the selection and stays offered by the widget (see
    selection_options / stale_selection), so re-saving the set writes
    back exactly what it held. The trader is told what is broken and
    decides; nothing about a saved configuration is rewritten on their
    behalf, and a scan still reports the same actionable
    CompositeResolutionError it always did rather than quietly running
    a smaller selection than the one on file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from strategy_sets.model import StrategyGroup, StrategyGroupPair, StrategySet

# The "no source chosen" sentinel of each group's source selectbox.
# Not a valid StrategySet name (StrategySet's own name pattern allows
# only letters/digits/spaces/'-'/'_'), so it can never collide with a
# real saved set.
NO_SOURCE_LABEL = "— None —"

GROUP_A = "A"
GROUP_B = "B"

PANEL_TITLE = "Composite (Group A × Group B)"

PANEL_HELP = (
    "Pair every selected Group A strategy against every selected Group B strategy. "
    "Each combination is priced as 'Group A strategy − Group B strategy'. Combinations "
    "are generated at scan time and never saved — only the source Strategy Sets and the "
    "selected strategy names are stored."
)

GROUP_A_HELP = "Required to build a composite. Its selection order is preserved exactly as chosen."

GROUP_B_HELP = (
    "Optional. Leave the source as “— None —” to keep this Strategy Set behaving exactly "
    "as it does today (no combinations are generated without a Group B)."
)

SELECTION_HELP = (
    "Selected in the order you pick them. Selecting the same strategy on both sides is "
    "valid — a combination that cancels completely is dropped automatically."
)

GROUP_B_WITHOUT_A_ERROR = (
    "Group A is required for a composite Strategy Set. Choose a Group A source Strategy Set, "
    "or set Group B's source back to “— None —”."
)


@dataclass(frozen=True)
class SourceState:
    """What the panel knows about ONE group's chosen source Strategy Set.

    `available` is None -- distinct from an empty tuple -- when the
    source could not be resolved at all (no such saved set, or it is
    itself a composite). That distinction is the whole point of this
    type: an empty tuple means "resolved, and it has no selectable
    strategies" (a selection can legitimately be clamped to nothing),
    while None means "unknown", and a group whose availability is
    unknown must be preserved verbatim rather than clamped (see the
    module docstring's third rule).
    """

    name: str | None
    available: tuple[str, ...] | None
    problem: str | None = None

    @property
    def resolved(self) -> bool:
        return self.available is not None


def selectable_entry_names(strategy_set: StrategySet) -> tuple[str, ...]:
    """Every strategy in `strategy_set` that a group may select, in the
    set's own order: ordinary `entries` first, then
    `intermarket_entries`.

    One shared namespace, exactly as strategy_sets.composite.
    resolve_group_entries() resolves them -- StrategySet's own
    validation already guarantees a name is unique across both
    collections, so a selected name identifies at most one strategy of
    either kind. Both are legitimate group members: each flattens to a
    flat LegSpec tuple during composition, so no nesting can arise.

    An entry's `enabled` flag is deliberately NOT consulted, matching
    resolve_group_entries()'s own documented rule: naming a strategy in
    a group's selection IS the decision to include it, and hiding a
    strategy here because of a flag set in a different, source Strategy
    Set would make the panel disagree with what a scan actually runs.
    """
    return tuple(
        [entry.name for entry in strategy_set.entries]
        + [entry.name for entry in strategy_set.intermarket_entries]
    )


def is_eligible_source(strategy_set: StrategySet) -> bool:
    """Whether `strategy_set` may be offered as a group's source.

    Rejects a composite set (`groups is not None`) -- the UI-side half
    of strategy_sets.composite.resolve_group_entries()'s own
    no-nested-composites rule -- and a set with nothing selectable in
    it, which could only ever produce an empty group.
    """
    return strategy_set.groups is None and bool(selectable_entry_names(strategy_set))


def eligible_source_names(
    sets_by_name: Mapping[str, StrategySet], exclude_name: str | None = None
) -> list[str]:
    """The saved Strategy Sets offerable as a group source, in
    `sets_by_name`'s own iteration order (the repository already lists
    names sorted -- this function never re-sorts, so the panel shows
    them in exactly the order the caller supplied).

    `exclude_name` is the set currently being edited: a composite
    Strategy Set can never be its own source.
    """
    return [
        name
        for name, strategy_set in sets_by_name.items()
        if name != exclude_name and is_eligible_source(strategy_set)
    ]


def source_options(eligible: Sequence[str], stored_source: str | None) -> list[str]:
    """The source selectbox's options: the "no source" sentinel, then
    every eligible name.

    A `stored_source` that is NOT eligible (a saved group whose source
    set was since deleted, renamed, or turned into a composite) is
    still appended, because it is the group's real persisted value and
    a selectbox cannot hold a value outside its own options. Offering
    it keeps the saved configuration visible and intact; the panel
    reports the problem separately (see resolve_source_state) rather
    than silently resetting the selection to "none".
    """
    options = [NO_SOURCE_LABEL, *eligible]
    if stored_source is not None and stored_source not in options:
        options.append(stored_source)
    return options


def resolve_source_state(
    source_name: str | None, sets_by_name: Mapping[str, StrategySet], own_name: str | None = None
) -> SourceState:
    """What the panel can do with the currently-chosen `source_name`.

    Returns `available=None` plus a trader-facing `problem` whenever the
    source cannot be used as-is -- it no longer exists, it is the set
    being edited, or it has itself become a composite. In every one of
    those cases the group's stored selection must be preserved
    untouched (see the module docstring's third rule), which is exactly
    what `available is None` signals to the caller.
    """
    if source_name is None:
        return SourceState(name=None, available=(), problem=None)

    if own_name is not None and source_name == own_name:
        return SourceState(
            name=source_name,
            available=None,
            problem=(
                f"'{source_name}' cannot be a source for itself — pick a different "
                "Strategy Set."
            ),
        )

    strategy_set = sets_by_name.get(source_name)
    if strategy_set is None:
        return SourceState(
            name=source_name,
            available=None,
            problem=(
                f"Source Strategy Set '{source_name}' was not found. Its saved selection is "
                "kept as-is, but this composite cannot run until the set exists again or a "
                "different source is chosen."
            ),
        )

    if strategy_set.groups is not None:
        return SourceState(
            name=source_name,
            available=None,
            problem=(
                f"Source Strategy Set '{source_name}' is itself a composite. Nested composite "
                "Strategy Sets are not supported — pick an ordinary Strategy Set."
            ),
        )

    return SourceState(
        name=source_name, available=selectable_entry_names(strategy_set), problem=None
    )


def selection_options(available: Sequence[str], selected: Sequence[str]) -> list[str]:
    """The strategy multiselect's options: everything currently
    selectable in the source set, in that set's own order, followed by
    any ALREADY-SELECTED name the source set no longer offers.

    Appending the stale names is what keeps a saved selection intact. A
    Streamlit multiselect cannot hold a value outside its own options,
    so the alternative would be to drop those names -- silently
    rewriting a saved configuration the trader never asked to change,
    and silently turning a scan that should report "selected strategy
    'X' was not found" into one that quietly runs a smaller selection
    instead. They are offered, flagged (see stale_selection), and
    removable by hand.
    """
    options = [name for name in available]
    known = set(options)
    for name in selected:
        if name not in known:
            options.append(name)
            known.add(name)
    return options


def stale_selection(selected: Sequence[str], available: Sequence[str]) -> list[str]:
    """Selected names the source Strategy Set no longer offers, in
    `selected`'s own order -- what the panel flags so a broken
    selection is visible at authoring time instead of only as a
    CompositeResolutionError at Run Scan."""
    allowed = set(available)
    stale: list[str] = []
    for name in selected:
        if name not in allowed and name not in stale:
            stale.append(name)
    return stale


def stored_group(groups: StrategyGroupPair | None, slot: str) -> StrategyGroup | None:
    """Group A or Group B of an already-saved configuration, or None --
    the panel's seed values. `slot` is GROUP_A or GROUP_B."""
    if groups is None:
        return None
    return groups.group_a if slot == GROUP_A else groups.group_b


def build_group(source_name: str | None, selected_names: Sequence[str]) -> StrategyGroup | None:
    """One StrategyGroup, or None when no source is chosen.

    An EMPTY selection with a chosen source is preserved as a real
    group (StrategyGroup explicitly allows it -- "source chosen,
    nothing selected yet"), never collapsed to None: collapsing it
    would silently discard the trader's source choice on the next save.
    """
    if source_name is None:
        return None
    return StrategyGroup(
        source_set_name=source_name, selected_entry_names=tuple(selected_names)
    )


def build_group_pair(
    group_a: StrategyGroup | None, group_b: StrategyGroup | None
) -> tuple[StrategyGroupPair | None, str | None]:
    """`(pair, error)` for the two authored groups.

    * No Group A and no Group B -> `(None, None)`: not a composite at
      all, which is the ordinary, non-composite Strategy Set and never
      an error.
    * Group A only -> a pair with `group_b=None`. Valid and persisted:
      strategy_sets.composite.resolve_composite_combinations() returns
      no combinations for it, so the set keeps behaving exactly as an
      ordinary Strategy Set does today.
    * Group B without Group A -> `(None, GROUP_B_WITHOUT_A_ERROR)`.
      "Group B but no Group A" has no meaning under an A x B
      definition, and StrategyGroupPair has no way to represent it.

    Never reorders the two sides -- Group A stays Group A regardless of
    names, sizes, or alphabetical order (see the module docstring).
    """
    if group_a is None:
        if group_b is None:
            return None, None
        return None, GROUP_B_WITHOUT_A_ERROR
    return StrategyGroupPair(group_a=group_a, group_b=group_b), None


def reuse_unchanged(
    authored: StrategyGroupPair | None, stored: StrategyGroupPair | None
) -> StrategyGroupPair | None:
    """`stored` when the panel authored something equal to it,
    otherwise `authored`.

    Keeps OBJECT IDENTITY stable across a render that changed nothing,
    so an untouched composite Strategy Set saves back exactly the
    object it was loaded with -- the same preservation guarantee
    ui.strategy_set_formatting.build_strategy_set_from_grid() already
    documents for `intermarket_entries` and `groups`, and what makes a
    load -> save round trip byte-identical.
    """
    if stored is not None and authored == stored:
        return stored
    return authored


def group_summary(group: StrategyGroup | None) -> str:
    """One compact, trader-facing line describing a group's current
    state -- shown next to the group so the saved configuration is
    legible without expanding anything."""
    if group is None:
        return "No source Strategy Set selected."
    count = len(group.selected_entry_names)
    if count == 0:
        return f"'{group.source_set_name}' — no strategies selected yet."
    noun = "strategy" if count == 1 else "strategies"
    return f"'{group.source_set_name}' — {count} {noun} selected."


def composite_summary(pair: StrategyGroupPair | None) -> str:
    """One line describing the whole authored configuration, including
    what it means for a scan. Deliberately states no combination COUNT:
    counting means resolving and composing, which is
    strategy_sets.composite's job, not this module's (see the module
    docstring)."""
    if pair is None:
        return "Not a composite Strategy Set — this set scans its own strategies only."
    if pair.group_b is None:
        return (
            "Group A only — no combinations are generated; this set scans its own "
            "strategies exactly as an ordinary Strategy Set does."
        )
    return (
        f"Group A ({len(pair.group_a.selected_entry_names)}) × "
        f"Group B ({len(pair.group_b.selected_entry_names)}) — combinations are generated "
        "at scan time and never saved."
    )


__all__ = [
    "NO_SOURCE_LABEL",
    "GROUP_A",
    "GROUP_B",
    "PANEL_TITLE",
    "PANEL_HELP",
    "GROUP_A_HELP",
    "GROUP_B_HELP",
    "SELECTION_HELP",
    "GROUP_B_WITHOUT_A_ERROR",
    "SourceState",
    "selectable_entry_names",
    "is_eligible_source",
    "eligible_source_names",
    "source_options",
    "resolve_source_state",
    "selection_options",
    "stale_selection",
    "stored_group",
    "build_group",
    "build_group_pair",
    "reuse_unchanged",
    "group_summary",
    "composite_summary",
]
