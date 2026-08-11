"""
strategy_set_view.py

Module 7B: the Strategy Set panel -- select a saved strategy_sets.
StrategySet, see its entries (enabled/disabled, market, structure,
weights), run it through the existing scanner pipeline, and edit it
(add/remove/enable-disable entries, rename, duplicate, delete, save,
create new). A thin Streamlit layer, structurally identical in spirit
to ui.controls/ui.results_view: persistence, serialization, validation,
and expansion all delegate to strategy_sets/template_scanner
unmodified -- this module only renders widgets and translates their
values into calls on those existing public functions.

Scanner integration (the smallest clean point found in template_scanner.
scanner.run_scan()): run_scan() itself only accepts a flat
list[StrategyDefinition] plus one shared contract window and internally
rolls/dedupes/prices/analyzes them. A Strategy Set's entries already
roll independently per entry (different max_curve_position/eligible_
rics per entry are a real, supported feature -- see strategy_sets.
model.ExpansionSettings), so building one shared ScanRequest from all
entries' StrategyDefinitions would silently drop that per-entry
filtering. Instead: strategy_sets.expansion.expand_strategy_set()
(unmodified) produces the exact list[StrategyInstance] run_scan()
would have produced internally, respecting each entry's own filters,
and the NEW template_scanner.scanner.run_scan_on_instances() (extracted
from run_scan()'s own body -- see that module's docstring) prices and
analyzes it through the identical pipeline. No scanner logic is
duplicated; run_scan() and this path now share one implementation.

The Universe (contract_start/contract_end) and History (price_start/
price_end) controls, plus Lookbacks/Percentile Range/Primary Lookback,
are NOT duplicated here -- render_strategy_set_panel() takes the
already-rendered ui.controls.ScanSetup and reads those shared fields
directly, exactly as ui.scan_view.handle_run_scan does for the manual
grid. Running a Strategy Set writes into ui.state's existing
SCAN_REQUEST/SCAN_REPORT keys, so ui.results_view/ui.chart_view render
its results with no changes of their own -- from their perspective a
Strategy Set run looks identical to a manual Run Scan.
"""

from __future__ import annotations

import traceback

import pandas as pd
import streamlit as st

from core.config import MARKETS, BarInterval

from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository

from template_scanner.scanner import ScanRequest, run_scan_on_instances

from ui import state
from ui import strategy_set_state as ss_state
from ui.controls import ScanSetup
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
    remove_entry_by_name,
)

_NEW_SET_OPTION = "+ New Strategy Set"
_SELECTOR_KEY = "oscill8_ss_selector"


def render_strategy_set_panel(setup: ScanSetup) -> None:
    """Render the whole Strategy Set panel: selector, entries table,
    Run button, and the "Manage Strategy Set" editing controls.

    `setup` is the SAME ScanSetup the scan bar above already built --
    only its shared Universe/History/Lookbacks/Percentile fields are
    read here (contract_start/contract_end/price_start/price_end/
    lookbacks/display_lookback/lower_percentile/upper_percentile);
    market_key/interval/grid_rows/run_clicked belong to the manual
    grid workflow and are ignored.
    """
    ss_state.init_state()
    repo = StrategySetRepository()

    with st.container(border=True):
        st.subheader("Strategy Sets")

        message = ss_state.pop_message()
        if message is not None:
            level, text = message
            renderer = {"success": st.success, "error": st.error, "info": st.info}.get(level, st.info)
            renderer(text)

        names = repo.list_names()
        _render_selector(repo, names)

        entries = ss_state.get_draft_entries()
        if entries is None:
            st.caption("Select a saved Strategy Set above, or create a new one.")
            return

        selected_name = ss_state.get_selected_name()
        entries = _render_entries_table(entries)
        _render_run_button(setup, selected_name, entries)

        with st.expander("Manage Strategy Set"):
            _render_add_strategy(entries)
            st.divider()
            _render_remove_strategy(entries)
            st.divider()
            _render_save_and_lifecycle(repo, selected_name, entries)


# ---------------------------------------------------------------------
# Selector
#
# Streamlit forbids writing to a widget's own session-state key once
# that widget has been instantiated in the current script run -- and
# save/rename/duplicate/delete run from the "Manage Strategy Set"
# expander, AFTER the selector below has already rendered in the same
# pass. So a lifecycle action never touches `_SELECTOR_KEY` directly;
# it calls ss_state.set_pending_selection(name) and st.rerun(). On the
# FRESH rerun that follows, _render_selector applies that pending value
# to `_SELECTOR_KEY` here, before st.selectbox() (re)creates the widget
# -- the one point in the script where writing to it is legal -- so the
# desired selection takes effect on the next render, never the current
# one.
# ---------------------------------------------------------------------

def _render_selector(repo: StrategySetRepository, names: list[str]) -> None:
    options = [_NEW_SET_OPTION] + names

    pending = ss_state.pop_pending_selection()
    if pending is not None:
        st.session_state[_SELECTOR_KEY] = pending if pending in options else _NEW_SET_OPTION
    elif st.session_state.get(_SELECTOR_KEY) not in options:
        selected = ss_state.get_selected_name()
        st.session_state[_SELECTOR_KEY] = selected if selected in options else options[0]

    choice = st.selectbox("Strategy Set", options, key=_SELECTOR_KEY)

    if choice == _NEW_SET_OPTION:
        if ss_state.get_draft_entries() is None or ss_state.get_selected_name() is not None:
            ss_state.start_new_draft()
    elif choice != ss_state.get_selected_name():
        strategy_set = repo.load(choice)
        ss_state.load_draft(choice, list(strategy_set.entries), strategy_set.description)


# ---------------------------------------------------------------------
# Entries table
# ---------------------------------------------------------------------

def _render_entries_table(entries: list[StrategySetEntry]) -> list[StrategySetEntry]:
    st.caption("Strategies in Set")
    df = pd.DataFrame(entries_to_rows(entries), columns=list(ENTRY_TABLE_COLUMNS))
    column_config = {
        ENABLED_COLUMN: st.column_config.CheckboxColumn(ENABLED_COLUMN, width="small"),
        NAME_COLUMN: st.column_config.TextColumn(NAME_COLUMN, disabled=True),
        MARKET_COLUMN: st.column_config.TextColumn(MARKET_COLUMN, disabled=True, width="small"),
        INTERVAL_COLUMN: st.column_config.TextColumn(INTERVAL_COLUMN, disabled=True, width="small"),
        WEIGHTS_COLUMN: st.column_config.TextColumn(WEIGHTS_COLUMN, disabled=True),
    }
    # Keyed by selection + entry count so the widget's own cached edit
    # state resets whenever the loaded set OR the entry count changes
    # (add/remove) -- the same "change the key when the meaningful
    # input changes" convention ui.controls' position grid uses.
    selected_name = ss_state.get_selected_name()
    editor_key = f"oscill8_ss_entries_{selected_name or 'new'}_{len(entries)}"
    edited = st.data_editor(
        df, hide_index=True, num_rows="fixed", column_config=column_config, key=editor_key
    )
    updated = apply_enabled_edits(entries, edited.to_dict("records"))
    if updated != entries:
        ss_state.set_draft_entries(updated)
    return updated


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------

def _render_run_button(setup: ScanSetup, selected_name: str | None, entries: list[StrategySetEntry]) -> None:
    any_enabled = any(e.enabled for e in entries)
    label = f"▶ Run '{selected_name}'" if selected_name else "▶ Run Strategy Set"
    if st.button(label, type="primary", disabled=not any_enabled, key="oscill8_ss_run_button"):
        strategy_set = StrategySet(
            name=selected_name or "Unsaved Strategy Set",
            entries=tuple(entries),
            description=ss_state.get_draft_description(),
        )
        handle_run_strategy_set(setup, strategy_set)
    if not any_enabled:
        st.caption("Enable at least one strategy to run this set.")


def handle_run_strategy_set(setup: ScanSetup, strategy_set: StrategySet) -> None:
    """Run `strategy_set` through the existing scanner pipeline using
    the shared Universe/History/Lookback/Percentile values already on
    `setup`, and store the result exactly where ui.scan_view.
    handle_run_scan stores a manual scan's result -- ui.results_view/
    ui.chart_view need no changes to render either one.
    """
    state.store_scan_error(None)

    if setup.display_lookback is None:
        st.error("Select at least one lookback before running a scan.")
        return

    enabled_definitions = tuple(e.definition for e in strategy_set.entries if e.enabled)
    if not enabled_definitions:
        st.error("Enable at least one strategy in the set before running it.")
        return

    instances = expand_strategy_set(strategy_set, setup.contract_start, setup.contract_end)
    if not instances:
        st.error(
            "No candidates were generated for the selected contract window -- "
            "widen the Universe start/end dates."
        )
        return

    try:
        request = ScanRequest(
            definitions=enabled_definitions,
            contract_start=setup.contract_start,
            contract_end=setup.contract_end,
            price_start=setup.price_start,
            price_end=setup.price_end,
            lookbacks=setup.lookbacks,
            lower_percentile=setup.lower_percentile,
            upper_percentile=setup.upper_percentile,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    try:
        with st.spinner(f"Scanning '{strategy_set.name}'..."):
            report = run_scan_on_instances(
                instances,
                setup.price_start,
                setup.price_end,
                lookbacks=setup.lookbacks,
                lower_percentile=setup.lower_percentile,
                upper_percentile=setup.upper_percentile,
            )
    except Exception as exc:  # noqa: BLE001 -- UI boundary, same policy as ui.scan_view
        state.store_scan_error(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        return

    state.store_scan_result(request, report, setup.display_lookback)


# ---------------------------------------------------------------------
# Add / remove
# ---------------------------------------------------------------------

def _render_add_strategy(entries: list[StrategySetEntry]) -> None:
    st.caption("Add Strategy")
    col_name, col_market, col_interval, col_positions = st.columns([2, 1, 1, 1])
    with col_name:
        name = st.text_input("Name", key="oscill8_ss_add_name")
    with col_market:
        market_key = st.selectbox(
            "Market", list(MARKETS.keys()), format_func=lambda k: MARKETS[k].name, key="oscill8_ss_add_market"
        )
    with col_interval:
        interval = st.selectbox(
            "Interval",
            (BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR),
            format_func=lambda i: i.value,
            key="oscill8_ss_add_interval",
        )
    with col_positions:
        n_positions = st.number_input(
            "Positions", min_value=2, max_value=12, value=6, step=1, key="oscill8_ss_add_positions"
        )

    position_columns = tuple(position_column(i) for i in range(1, n_positions + 1))
    column_config = {
        col: st.column_config.TextColumn(str(i), width="small", validate=r"^-?\d*\.?\d*$")
        for i, col in enumerate(position_columns, start=1)
    }
    blank_row = pd.DataFrame([{col: "" for col in position_columns}])
    edited = st.data_editor(
        blank_row,
        hide_index=True,
        num_rows="fixed",
        column_config=column_config,
        key=f"oscill8_ss_add_grid_{n_positions}",
    )

    if st.button("Add to Set", key="oscill8_ss_add_button"):
        row = edited.to_dict("records")[0]
        try:
            new_entry = build_entry_from_grid_row(row, position_columns, market_key, interval, name)
        except ValueError as exc:
            st.error(str(exc))
            return
        if new_entry.name in entry_names(entries):
            st.error(f"An entry named '{new_entry.name}' already exists in this set.")
            return
        ss_state.set_draft_entries(entries + [new_entry])
        st.rerun()


def _render_remove_strategy(entries: list[StrategySetEntry]) -> None:
    st.caption("Remove Strategy")
    if not entries:
        st.caption("No strategies to remove yet.")
        return

    names_now = entry_names(entries)
    if st.session_state.get("oscill8_ss_remove_target") not in names_now:
        st.session_state["oscill8_ss_remove_target"] = names_now[0]

    col_select, col_button = st.columns([3, 1])
    with col_select:
        target = st.selectbox("Entry", names_now, key="oscill8_ss_remove_target")
    with col_button:
        st.write("")
        if st.button("Remove", key="oscill8_ss_remove_button"):
            ss_state.set_draft_entries(remove_entry_by_name(entries, target))
            st.rerun()


# ---------------------------------------------------------------------
# Save / rename / duplicate / delete
# ---------------------------------------------------------------------

def _render_save_and_lifecycle(
    repo: StrategySetRepository, selected_name: str | None, entries: list[StrategySetEntry]
) -> None:
    description = st.text_area(
        "Description", value=ss_state.get_draft_description(), key="oscill8_ss_description"
    )
    ss_state.set_draft_description(description)

    if selected_name is None:
        new_name = st.text_input("New Strategy Set name", key="oscill8_ss_new_name")
    else:
        new_name = selected_name
        st.caption(f"Editing **{selected_name}**")

    if st.button("Save", type="primary", key="oscill8_ss_save_button"):
        _handle_save(repo, selected_name, new_name, entries, description)

    if selected_name is None:
        return

    st.divider()
    col_rename, col_duplicate, col_delete = st.columns(3)
    with col_rename:
        rename_to = st.text_input("Rename to", key="oscill8_ss_rename_to")
        if st.button("Rename", key="oscill8_ss_rename_button") and rename_to:
            _handle_rename(repo, selected_name, rename_to)
    with col_duplicate:
        duplicate_to = st.text_input("Duplicate as", key="oscill8_ss_duplicate_to")
        if st.button("Duplicate", key="oscill8_ss_duplicate_button") and duplicate_to:
            _handle_duplicate(repo, selected_name, duplicate_to)
    with col_delete:
        confirm = st.checkbox("Confirm delete", key="oscill8_ss_delete_confirm")
        if st.button("Delete", key="oscill8_ss_delete_button", disabled=not confirm):
            _handle_delete(repo, selected_name)


def _handle_save(
    repo: StrategySetRepository,
    selected_name: str | None,
    new_name: str,
    entries: list[StrategySetEntry],
    description: str,
) -> None:
    if not entries:
        st.error("Add at least one strategy before saving.")
        return

    if selected_name is None:
        name = new_name.strip()
        if not name:
            st.error("Enter a name for the new Strategy Set.")
            return
        if repo.exists(name):
            st.error(f"A Strategy Set named '{name}' already exists.")
            return
    else:
        name = selected_name

    try:
        strategy_set = StrategySet(name=name, entries=tuple(entries), description=description)
    except ValueError as exc:
        st.error(str(exc))
        return

    repo.save(strategy_set)
    ss_state.load_draft(name, list(strategy_set.entries), strategy_set.description)
    ss_state.set_message("success", f"Saved '{name}'.")
    ss_state.set_pending_selection(name)
    st.rerun()


def _handle_rename(repo: StrategySetRepository, selected_name: str, rename_to: str) -> None:
    try:
        renamed = repo.rename(selected_name, rename_to.strip())
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        st.error(str(exc))
        return
    ss_state.load_draft(renamed.name, list(renamed.entries), renamed.description)
    ss_state.set_message("success", f"Renamed to '{renamed.name}'.")
    ss_state.set_pending_selection(renamed.name)
    st.rerun()


def _handle_duplicate(repo: StrategySetRepository, selected_name: str, duplicate_to: str) -> None:
    try:
        copy = repo.duplicate(selected_name, duplicate_to.strip())
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        st.error(str(exc))
        return
    ss_state.load_draft(copy.name, list(copy.entries), copy.description)
    ss_state.set_message("success", f"Duplicated as '{copy.name}'.")
    ss_state.set_pending_selection(copy.name)
    st.rerun()


def _handle_delete(repo: StrategySetRepository, selected_name: str) -> None:
    repo.delete(selected_name)
    ss_state.start_new_draft()

    # Select a sensible remaining set (first remaining name, alphabetically
    # -- StrategySetRepository.list_names() is already sorted) rather than
    # always dropping back to "+ New Strategy Set" when other sets exist.
    remaining = repo.list_names()
    next_selection = remaining[0] if remaining else _NEW_SET_OPTION

    ss_state.set_message("info", f"Deleted '{selected_name}'.")
    ss_state.set_pending_selection(next_selection)
    st.rerun()
