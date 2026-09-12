"""
composite_view.py

The composite ("Group A x Group B") authoring panel: the Streamlit
surface that finally lets a trader CREATE a composite Strategy Set,
rendered inside the existing Strategy Workspace section beneath the
strategy grid -- not a separate page, not a second grid, and not a
second Run button.

What this module does, and all it does: read/write the group
CONFIGURATION (one source Strategy Set per group, plus that group's
explicitly-ordered strategy selection) and hand the resulting
strategy_sets.model.StrategyGroupPair back to its caller
(ui.controls._render_strategy_templates), which forwards it to exactly
two already-existing places:

    * ui.strategy_set_view.process_save() -> build_strategy_set_from_grid(
      groups=...) -> StrategySetRepository.save()   [persistence]
    * ui.controls.ScanSetup.groups -> ui.scan_view.handle_run_scan()
      -> strategy_sets.execution.run_strategy_set()  [execution]

Both of those paths already existed and are unchanged in substance:
Phase 4 wired a loaded set's `groups` through them, and this panel
simply makes that same `groups` object authorable instead of
hand-edited JSON. Nothing here resolves a group, forms an A x B pair,
composes a definition, filters a structurally-zero combination, or
generates/prices/dedupes a candidate -- strategy_sets.composite owns
every one of those, and the panel reaches them only through the one
read-only preview described below. No Cartesian-product result is ever
built here and none is ever persisted.

Combination preview: the one place this module calls into
strategy_sets.composite at all, via the existing, unmodified
resolve_composite_combinations() -- never a reimplementation, and never
a partial copy of its rules. It exists so the trader sees, before
running anything, how many combinations the current selection actually
produces (structural zeros already excluded, since that function drops
them itself) and sees a CompositeResolutionError's own trader-facing
text at authoring time rather than only at Run Scan. It is a pure read:
it touches no market data, no provider, no cache, and nothing it
computes is stored anywhere.

Widget lifecycle: the "Select all"/"Clear all" buttons write directly
to the strategy multiselect's own session-state key. That is legal
here -- and only here -- because both buttons render EARLIER in the
same script pass than the multiselect they write to; Streamlit only
forbids writing to a widget's key once that widget has already been
instantiated in the current run. This is the same constraint
ui.strategy_set_view's pending-selection indirection exists for, solved
differently because the ordering happens to be favourable in this
panel and unfavourable there.

Widget keys are namespaced by the currently-selected Strategy Set name,
exactly like ui.controls' strategy grid editor key: switching Strategy
Sets must show the newly-loaded set's own groups, not the previous
set's half-edited selection.

Preservation of what cannot be resolved: a saved group whose source
Strategy Set is missing, is the set being edited, or has itself become
a composite is displayed READ-ONLY and returned UNCHANGED, by
reference. Saving such a set therefore rewrites nothing -- the same
lossless-round-trip guarantee ui.strategy_set_formatting already makes
for `intermarket_entries`. The trader is told what is wrong; the saved
configuration is never silently emptied or repaired.
"""

from __future__ import annotations

import streamlit as st

from core.config import BarInterval
from core.utils import get_logger

from strategy_sets.composite import (
    CompositeResolutionError,
    resolve_composite_combinations,
)
from strategy_sets.model import StrategyGroup, StrategyGroupPair, StrategySet
from strategy_sets.repository import StrategySetRepository

from ui.composite_formatting import (
    GROUP_A,
    GROUP_A_HELP,
    GROUP_B,
    GROUP_B_HELP,
    NO_SOURCE_LABEL,
    PANEL_HELP,
    PANEL_TITLE,
    SELECTION_HELP,
    SourceState,
    build_group,
    build_group_pair,
    composite_summary,
    eligible_source_names,
    group_summary,
    resolve_source_state,
    reuse_unchanged,
    selection_options,
    source_options,
    stale_selection,
    stored_group,
)

logger = get_logger(__name__)

# Stand-in name for the combination preview when the workspace is an
# unsaved "+ New Strategy Set". Only ever appears inside a preview
# CompositeResolutionError's own text; nothing is written under it, and
# StrategySet validates it like any other name.
_UNSAVED_SET_NAME = "Unsaved Strategy Set"

_NO_SOURCES_HINT = (
    "No other Strategy Set is available as a source yet. Save at least one ordinary "
    "Strategy Set (a composite set cannot be a source) to build a composite from it."
)


def _widget_key(slot: str, field: str, selected_name: str | None) -> str:
    """Namespaced by the loaded Strategy Set so switching sets reseeds
    the panel from the newly-loaded set's own groups (same reasoning as
    ui.controls' strategy-grid editor key)."""
    return f"oscill8_composite_{slot.lower()}_{field}::{selected_name or 'new'}"


def _load_candidate_sets(repo: StrategySetRepository) -> dict[str, StrategySet]:
    """Every saved Strategy Set the panel can offer as a source, keyed
    by name, in StrategySetRepository.list_names()' own sorted order.

    A file that fails to load (hand-edited into an invalid state, for
    instance) is skipped with a log line rather than taking the whole
    Strategy Workspace down -- this panel is an optional authoring
    surface, and one unreadable unrelated set must not stop a trader
    editing everything else.
    """
    candidates: dict[str, StrategySet] = {}
    for name in repo.list_names():
        try:
            candidates[name] = repo.load(name)
        except Exception as exc:  # noqa: BLE001 -- one bad file must not break the panel
            logger.warning("Composite panel: skipping unreadable Strategy Set '%s': %s", name, exc)
    return candidates


def _render_read_only_group(stored: StrategyGroup | None, state: SourceState) -> StrategyGroup | None:
    """What a group whose source could not be resolved shows: the
    problem, then its stored selection as plain text.

    Returns the STORED group unchanged (by reference) so a save
    rewrites nothing -- see the module docstring's preservation note.
    When there is no stored selection to preserve (the trader just
    picked an unusable source), a source-only group is returned so the
    choice itself is not silently discarded.
    """
    st.warning(state.problem)
    if stored is not None and stored.source_set_name == state.name:
        if stored.selected_entry_names:
            st.caption("Saved selection (kept unchanged): " + ", ".join(stored.selected_entry_names))
        else:
            st.caption("Saved selection (kept unchanged): none.")
        return stored
    return build_group(state.name, ())


def _render_group(
    slot: str,
    heading: str,
    help_text: str,
    stored: StrategyGroup | None,
    candidates: dict[str, StrategySet],
    selected_name: str | None,
) -> StrategyGroup | None:
    """One side of the composite: its source selectbox, its Select all/
    Clear all shortcuts, and its ordered strategy multiselect."""
    st.markdown(f"**{heading}**")

    stored_source = stored.source_set_name if stored is not None else None
    eligible = eligible_source_names(candidates, exclude_name=selected_name)
    options = source_options(eligible, stored_source)

    source_key = _widget_key(slot, "source", selected_name)
    if source_key not in st.session_state:
        st.session_state[source_key] = stored_source or NO_SOURCE_LABEL
    elif st.session_state[source_key] not in options:
        # A source that has since disappeared from the options (the set
        # was deleted this session, say) -- reset rather than let
        # Streamlit raise on a value outside its own options.
        st.session_state[source_key] = NO_SOURCE_LABEL

    choice = st.selectbox(
        f"Group {slot} source Strategy Set", options, key=source_key, help=help_text
    )
    source_name = None if choice == NO_SOURCE_LABEL else choice

    state = resolve_source_state(source_name, candidates, own_name=selected_name)
    if source_name is None:
        st.caption(group_summary(None))
        return None
    if not state.resolved:
        return _render_read_only_group(stored, state)

    available = state.available or ()
    # Namespaced by the SOURCE set as well as the loaded set: switching
    # a group's source must start that group's selection fresh, not
    # carry the previous source's strategy names over as a selection
    # the new source cannot honour. Switching back starts fresh too --
    # Streamlit discards the session state of a widget that was not
    # instantiated in the previous run, so the abandoned source's key
    # is gone by then. That is the correct outcome either way: a saved
    # selection only ever comes back from the file (see `stored`), and
    # a source a trader navigated away from carries no promise.
    entries_key = f"{_widget_key(slot, 'entries', selected_name)}::{source_name}"
    if entries_key not in st.session_state:
        seed = (
            list(stored.selected_entry_names)
            if stored is not None and stored.source_set_name == source_name
            else []
        )
        st.session_state[entries_key] = seed

    current = list(st.session_state.get(entries_key) or [])
    stale = stale_selection(current, available)
    if stale:
        st.warning(
            f"Selected but no longer in '{source_name}': "
            + ", ".join(stale)
            + ". The saved selection is kept as-is -- remove these, or restore them in the "
            "source Strategy Set, before running a scan."
        )
    # Options, not a rewrite of the stored value: a stale name stays
    # selected and stays offered, so nothing about a saved
    # configuration changes behind the trader's back (see
    # ui.composite_formatting.selection_options).
    selection_choices = selection_options(available, current)

    # Both buttons write to the multiselect's own session-state key,
    # which is legal ONLY because they render earlier in this same
    # script pass than the multiselect below -- see the module
    # docstring's widget-lifecycle note.
    col_all, col_clear = st.columns(2)
    with col_all:
        if st.button(
            "Select all",
            key=_widget_key(slot, "select_all", selected_name),
            width="stretch",
            disabled=not available,
        ):
            st.session_state[entries_key] = list(available)
    with col_clear:
        if st.button(
            "Clear all",
            key=_widget_key(slot, "clear_all", selected_name),
            width="stretch",
            disabled=not current,
        ):
            st.session_state[entries_key] = []

    if not selection_choices:
        st.caption(f"'{source_name}' has no strategies to select.")
        return build_group(source_name, ())

    selection = st.multiselect(
        f"Group {slot} strategies", selection_choices, key=entries_key, help=SELECTION_HELP
    )
    group = build_group(source_name, selection)
    st.caption(group_summary(group))
    return group


def _render_preview(
    pair: StrategyGroupPair | None,
    repo: StrategySetRepository,
    interval: BarInterval,
    selected_name: str | None,
) -> None:
    """How many combinations the current configuration produces, via
    the existing strategy_sets.composite.resolve_composite_combinations()
    -- never a second implementation of pairing, composition, or
    structural-zero filtering (that function already applies all
    three). Read-only: nothing it returns is stored, scanned, or
    priced.

    `interval` is the scan bar's Interval as ui.controls already peeks
    it (see _peek_current_interval -- its value as of the PREVIOUS
    rerun), applied exactly the way ui.scan_view's execution path
    applies the live one, so the preview reflects what a Run Scan would
    resolve rather than whatever interval each source file happens to
    carry. A preview one rerun behind the selector is harmless: the
    interval only affects whether two paired sources agree, never how
    many combinations exist, and the scan itself always uses the live
    value.
    """
    if pair is None or pair.group_b is None:
        st.caption(composite_summary(pair))
        return

    preview_name = selected_name or _UNSAVED_SET_NAME
    try:
        preview_set = StrategySet(name=preview_name, entries=(), groups=pair)
        combinations = resolve_composite_combinations(preview_set, repo, interval=interval)
    except CompositeResolutionError as exc:
        # Already written for a trader (it names the set, the group, the
        # source set, and the offending value) -- shown verbatim, the
        # same treatment ui.scan_view gives it at Run Scan.
        st.warning(str(exc))
        return
    except ValueError as exc:
        # e.g. two paired source strategies disagreeing on price_field
        # -- compose_definition()'s own validation, surfaced here rather
        # than only at Run Scan.
        st.warning(str(exc))
        return

    if not combinations:
        st.info(
            "No combinations — either a group has nothing selected, or every pair cancels "
            "completely and was dropped."
        )
        return

    noun = "combination" if len(combinations) == 1 else "combinations"
    st.success(
        f"{len(combinations)} {noun} will be generated at scan time (never saved). "
        f"First: {combinations[0].name}"
    )


def render_composite_panel(
    repo: StrategySetRepository,
    loaded_set: StrategySet | None,
    selected_name: str | None,
    interval: BarInterval,
) -> StrategyGroupPair | None:
    """Render the composite authoring panel and return the configuration
    it currently holds -- the object the caller must pass to BOTH the
    save path and the scan, so an unsaved edit scans exactly what the
    panel shows (the same rule the strategy grid itself already
    follows).

    Returns None when this is not a composite Strategy Set, which is
    every set that existed before this panel and every set whose Group
    A source is left at "— None —".
    """
    stored_pair = loaded_set.groups if loaded_set is not None else None
    candidates = _load_candidate_sets(repo)

    with st.expander(PANEL_TITLE, expanded=stored_pair is not None):
        st.caption(PANEL_HELP)
        if not eligible_source_names(candidates, exclude_name=selected_name) and stored_pair is None:
            st.caption(_NO_SOURCES_HINT)

        col_a, col_b = st.columns(2)
        with col_a:
            group_a = _render_group(
                GROUP_A,
                "Group A (required)",
                GROUP_A_HELP,
                stored_group(stored_pair, GROUP_A),
                candidates,
                selected_name,
            )
        with col_b:
            group_b = _render_group(
                GROUP_B,
                "Group B (optional)",
                GROUP_B_HELP,
                stored_group(stored_pair, GROUP_B),
                candidates,
                selected_name,
            )

        pair, error = build_group_pair(group_a, group_b)
        if error is not None:
            st.error(error)
            # Nothing valid to author or save: the ALREADY-SAVED
            # configuration is kept rather than being wiped by an
            # incomplete edit.
            return stored_pair

        _render_preview(pair, repo, interval, selected_name)

    return reuse_unchanged(pair, stored_pair)


__all__ = ["render_composite_panel"]
