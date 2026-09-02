"""
strategy_set_view.py

Module 7B (simplified): the Strategy Set selector and Save/"+ New"/
Delete controls that live at the top of ui.controls' Strategy Templates
section -- NOT a
separate application section, NOT a second grid, NOT a second Run
button. "Strategy Templates is the working strategy grid; a Strategy
Set is simply a saved named version of that grid" (see the design
review this simplification follows). Persistence, serialization, and
validation all delegate to strategy_sets/ui.formatting unmodified --
this module only renders the selector/save widgets and translates their
values into calls on those existing public functions.

Multi-market/multi-interval sets (e.g. "Intermarket Churning": SOFR +
SONIA + CORRA entries) round-trip losslessly: the grid carries its own
per-row Market/Interval columns (see ui.controls' column_config and
ui.formatting.build_definitions_from_grid, which resolves a row's own
Market/Interval before falling back to any grid-wide default), so
loading and re-saving a set never normalizes an entry to the "wrong"
market -- see ui.strategy_set_formatting's module docstring for the
full rationale. This module no longer overrides the scan bar's Market/
Interval selectors when a set loads, and there is no more "mixed
markets can't be represented" warning -- both were needed only while
the grid was bound to a single global market/interval.

Intermarket entries (Module 9 visibility slice): a saved set may also
carry `intermarket_entries` (legs spanning different markets), which the
single-market grid has no representation for. render_intermarket_
entries() shows them READ-ONLY beneath the grid whenever the loaded set
has any -- st.dataframe, no editor, no lifecycle control, no scan
button; hand-editing the set's JSON remains the only way to author one.
A save carries them through untouched (see process_save/_save's
`loaded_set` argument and ui.strategy_set_formatting.build_strategy_set_
from_grid's `intermarket_entries`), so load -> save -> reload of a mixed
set is lossless rather than silently dropping the half the grid can't
show. A set with no intermarket entries renders exactly as before --
the panel is not rendered at all.

Scanner integration: unchanged from before this simplification and
unchanged by it. A loaded Strategy Set becomes ordinary grid rows;
ui.scan_view.handle_run_scan() builds StrategyDefinition[] from
ui.controls.ScanSetup.grid_rows exactly as it always has, via
ui.formatting.build_definitions_from_grid() -- it has no idea, and does
not need to know, whether a row was typed manually or loaded from a
saved set. There is no Strategy-Set-specific execution path anymore
(template_scanner.scanner.run_scan_on_instances(), added for the
previous, richer per-entry Strategy Set design, is unused by this
module now -- run_scan() alone is the only path a Strategy Set's rows
ever take, exactly like the manual grid).

Selector widget lifecycle: Streamlit forbids writing to a widget's own
session-state key once that widget has been instantiated in the
current script run. Save runs from further down the same script pass
(inside the Strategy Templates section, below the selector), so it
never touches the selector's key directly -- it calls ui.strategy_set_
state.set_pending_selection(name) and st.rerun(). On the fresh rerun
that follows, render_selector() applies that pending value to the
selector's key before st.selectbox() (re)creates the widget -- the one
point in the script where writing to it is legal. This is the same fix
verified in the Strategy Set selector lifecycle bug fix; preserved
unchanged here.

Lifecycle controls row (UI/UX redesign pass): render_selector(),
render_save_button(), render_new_button(), and render_delete_control()
are each a single column's worth of widgets -- ui.controls renders all
five columns (selector, Save, "+ New", Delete, Positions) from ONE flat
st.columns() call so every control shares the same label-row/control-
row baseline (see ui.controls._render_strategy_templates()'s own
comment on why columns-inside-a-column previously left the buttons
floating above the dropdown). "+ New" and Delete act immediately, since
neither needs the just-edited grid's content: "+ New" reuses the exact
same pending-selection indirection Save already uses to switch the
selector to NEW_SET_OPTION (no second "new set" implementation), and
Delete only ever removes an already-SAVED file via the existing,
unmodified StrategySetRepository.delete() -- it never touches the
in-progress grid. Save is different: overwriting an existing set (or
creating a new one) needs that rerun's just-edited grid_rows, which
ui.controls only has AFTER the grid itself renders further down the
same script pass. So render_save_button() only captures whether the
Save button was clicked (a plain bool, returned to the caller) --
process_save() performs the actual save once grid_rows is available,
exactly mirroring the split ui.controls' render order already needs
for _peek_current_interval().

Delete confirmation: clicking Delete never deletes immediately -- it
only sets a session-state flag that opens an `@st.dialog` confirmation
naming the exact set to be removed, with Cancel/Delete actions (same
open/close pattern as the existing "+ New Strategy Set" name-prompt
dialog below). Only the dialog's own Delete button calls repo.delete().
After a confirmed delete, a "sensible remaining Strategy Set" is
selected automatically: the alphabetically-first name still on disk
(repo.list_names() is already sorted), or NEW_SET_OPTION (a blank
Strategy Workspace) if none remain -- applied via the same pending-
selection indirection, so the grid resets to whatever that selection's
own resolve_grid_seed() produces. No scan is ever triggered by delete.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core.config import BarInterval

from strategy_sets.model import StrategySet
from strategy_sets.repository import StrategySetRepository

from ui import strategy_set_state as ss_state
from ui.formatting import INTERVAL_COLUMN, LABEL_COLUMN, MARKET_COLUMN
from ui.intermarket_formatting import (
    LEG_COLUMNS,
    LEG_OFFSET_HELP,
    SECTION_CAPTION,
    entry_displays,
    entry_status_label,
    entry_summary_line,
    intermarket_notice,
    panel_title,
)
from ui.strategy_set_formatting import build_strategy_set_from_grid, grid_rows_from_strategy_set

NEW_SET_OPTION = "+ New Strategy Set"
_SELECTOR_KEY = "oscill8_ss_selector"


def render_selector(repo: StrategySetRepository) -> str | None:
    """The Strategy Set selectbox. Returns the currently selected saved
    name, or None for "+ New Strategy Set". Also updates ui.
    strategy_set_state's SELECTED_NAME to match, so later widgets in
    the same script pass (and the next rerun's pending-selection clamp)
    agree on what's loaded without re-deriving it from the widget.
    """
    names = repo.list_names()
    options = [NEW_SET_OPTION] + names

    pending = ss_state.pop_pending_selection()
    if pending is not None:
        st.session_state[_SELECTOR_KEY] = pending if pending in options else NEW_SET_OPTION
    elif st.session_state.get(_SELECTOR_KEY) not in options:
        selected = ss_state.get_selected_name()
        st.session_state[_SELECTOR_KEY] = selected if selected in options else options[0]

    choice = st.selectbox("Strategy Set", options, key=_SELECTOR_KEY)
    selected_name = None if choice == NEW_SET_OPTION else choice
    ss_state.set_selected_name(selected_name)
    return selected_name


def blank_grid_row(
    position_columns: tuple[str, ...], default_market_key: str, default_interval: BarInterval
) -> pd.DataFrame:
    """A single, genuinely-empty row -- what "+ New Strategy Set" shows
    (see the module docstring's design principle: a brand-new set
    starts from a blank grid, not a pre-filled example). Its Market/
    Interval cells default to whatever the scan bar currently has
    selected -- a starting point the user can freely change per row,
    not a constraint on the row."""
    row = {LABEL_COLUMN: "", MARKET_COLUMN: default_market_key, INTERVAL_COLUMN: default_interval.value}
    row.update({col: "" for col in position_columns})
    return pd.DataFrame([row])


def load_selected_set(repo: StrategySetRepository, selected_name: str | None) -> StrategySet | None:
    """The currently selected saved StrategySet, or None for "+ New
    Strategy Set". Exists so one script pass reads a set's JSON file
    exactly ONCE and shares that object across everything that needs it
    (the grid seed, the read-only intermarket panel, and the save path's
    intermarket preservation) rather than each re-loading it."""
    if selected_name is None:
        return None
    return repo.load(selected_name)


def resolve_grid_seed(
    selected_name: str | None,
    repo: StrategySetRepository,
    position_columns: tuple[str, ...],
    default_market_key: str,
    default_interval: BarInterval,
    strategy_set: StrategySet | None = None,
) -> pd.DataFrame:
    """The grid rows to show for the currently selected Strategy Set --
    each carrying its OWN Market/Interval, so a set mixing markets (see
    the module docstring) loads exactly as saved, with no need to
    override the scan bar's Market/Interval selectors and no "mixed
    markets" warning. `default_market_key`/`default_interval` seed only
    a genuinely blank "+ New Strategy Set" row's Market/Interval cells.

    `strategy_set` is the already-loaded set for `selected_name` when
    the caller has one (see load_selected_set) -- purely an I/O
    optimization; it is loaded from `repo` when omitted, exactly as
    before. Only the set's single-market `entries` seed the grid; its
    `intermarket_entries` have no grid representation and are shown
    read-only elsewhere (see render_intermarket_entries).
    """
    if selected_name is None:
        return blank_grid_row(position_columns, default_market_key, default_interval)

    if strategy_set is None:
        strategy_set = repo.load(selected_name)
    rows = grid_rows_from_strategy_set(strategy_set, position_columns)
    if not rows:
        return blank_grid_row(position_columns, default_market_key, default_interval)
    return pd.DataFrame(rows, columns=[LABEL_COLUMN, MARKET_COLUMN, INTERVAL_COLUMN, *position_columns])


def render_intermarket_entries(strategy_set: StrategySet | None) -> None:
    """The read-only Intermarket Strategies panel (Module 9 visibility
    slice) -- rendered ONLY when the loaded set actually carries
    intermarket entries, so a set with none (the ordinary case) looks
    and behaves exactly as it did before this panel existed.

    Read-only in the strongest available sense: st.dataframe (not
    st.data_editor), no widget writes back into the set, and no
    lifecycle control of any kind. Creating/editing/deleting an
    intermarket entry still means hand-editing the set's JSON file --
    this panel exists so a trader can SEE that those entries exist and
    what they contain, rather than inferring it from their silent
    absence in the grid above.

    Every value shown here comes from ui.intermarket_formatting's pure
    translation; the composite market label it produces is cosmetic
    only and never reaches provider resolution, a cache key, or a bp
    conversion (Module 9's display-only rule).
    """
    displays = entry_displays(strategy_set)
    if not displays:
        return

    with st.container(border=True):
        st.caption(SECTION_CAPTION)
        st.markdown(f"**{panel_title(displays)}**")
        notice = intermarket_notice(strategy_set)
        if notice is not None:
            st.info(notice)

        for display in displays:
            st.markdown(f"**{display.name}** · {entry_status_label(display)}")
            st.caption(entry_summary_line(display))
            st.dataframe(
                pd.DataFrame(display.leg_rows, columns=list(LEG_COLUMNS)),
                hide_index=True,
                key=f"oscill8_ss_intermarket_legs_{display.name}",
            )
        st.caption(LEG_OFFSET_HELP)


_SHOW_DIALOG_KEY = "oscill8_ss_show_save_dialog"
_SHOW_DELETE_DIALOG_KEY = "oscill8_ss_show_delete_dialog"


def render_save_button() -> bool:
    """The Save button only -- caller (ui.controls) places this in its
    own column of the Strategy Set control row (see the module
    docstring's "Lifecycle controls row" note for why Save is only
    captured here, not processed: it needs that rerun's just-edited
    grid_rows, which only exist after the grid itself renders further
    down the same script pass). Returns whether it was clicked this
    rerun; the caller must still call process_save()."""
    return st.button("Save Strategy Set", key="oscill8_ss_save_button", width="stretch")


def render_new_button() -> None:
    """The "+ New" button -- acts immediately, since starting a new
    (blank) Strategy Workspace needs no grid content. Reuses the exact
    "+ New Strategy Set" sentinel/pending-selection path Save already
    uses to switch the selector -- no second "start a new set"
    implementation."""
    if st.button("+ New", key="oscill8_ss_new_button", width="stretch"):
        ss_state.set_pending_selection(NEW_SET_OPTION)
        st.rerun()


def render_delete_control(repo: StrategySetRepository, selected_name: str | None) -> None:
    """The Delete button (disabled with nothing saved-and-selected) plus
    its confirmation dialog trigger -- also acts immediately, since
    deleting an already-SAVED file needs no grid content either."""
    if st.button(
        "Delete", key="oscill8_ss_delete_button", width="stretch", disabled=selected_name is None,
    ):
        st.session_state[_SHOW_DELETE_DIALOG_KEY] = True

    if selected_name is not None and st.session_state.get(_SHOW_DELETE_DIALOG_KEY):
        _delete_confirm_dialog(repo, selected_name)


@st.dialog("Delete Strategy Set")
def _delete_confirm_dialog(repo: StrategySetRepository, name: str) -> None:
    """Explicit, two-step delete: this dialog only ever opens from a
    Delete button click and never removes anything by itself -- only
    its own "Delete" button (below) actually calls repo.delete()."""
    st.warning(f"Delete Strategy Set **'{name}'**? This cannot be undone.")
    col_cancel, col_delete = st.columns(2)
    with col_cancel:
        if st.button("Cancel", key="oscill8_ss_delete_cancel", width="stretch"):
            st.session_state[_SHOW_DELETE_DIALOG_KEY] = False
            st.rerun()
    with col_delete:
        if st.button("Delete", key="oscill8_ss_delete_confirm", type="primary", width="stretch"):
            repo.delete(name)
            st.session_state[_SHOW_DELETE_DIALOG_KEY] = False
            # A sensible remaining set (alphabetically first, matching
            # repo.list_names()'s own sort) if any is left, else a
            # blank/new Strategy Workspace -- never a scan, never a
            # second deletion mechanism.
            remaining = repo.list_names()
            next_selection = remaining[0] if remaining else NEW_SET_OPTION
            ss_state.set_message("success", f"Deleted '{name}'.")
            ss_state.set_pending_selection(next_selection)
            st.rerun()


def process_save(
    repo: StrategySetRepository,
    selected_name: str | None,
    save_clicked: bool,
    grid_rows: list[dict],
    position_columns: tuple[str, ...],
    market_key: str,
    interval: BarInterval,
    loaded_set: StrategySet | None = None,
) -> None:
    """Acts on the Save click captured by render_controls_row(), now
    that the grid's current rows are known. Overwrites in place when a
    saved set is loaded; opens a small name prompt when "+ New Strategy
    Set" is active -- identical behavior to the original single-button
    Save control, just split across the grid's render point.

    `loaded_set` is the currently selected set as it was read at the top
    of this same script pass (see load_selected_set) -- its
    `intermarket_entries` are the ones a save must carry through
    untouched, since the grid cannot represent them (see
    render_intermarket_entries). Omitted/None means "nothing loaded",
    which is also correct for the "+ New Strategy Set" path: a
    brand-new set has no intermarket entries to preserve.
    """
    if selected_name is not None:
        if save_clicked:
            _save(
                repo, selected_name, grid_rows, position_columns, market_key, interval,
                new_name=None, loaded_set=loaded_set,
            )
        return

    if save_clicked:
        st.session_state[_SHOW_DIALOG_KEY] = True

    if st.session_state.get(_SHOW_DIALOG_KEY):
        _save_new_dialog(repo, grid_rows, position_columns, market_key, interval)


@st.dialog("Save Strategy Set")
def _save_new_dialog(
    repo: StrategySetRepository,
    grid_rows: list[dict],
    position_columns: tuple[str, ...],
    market_key: str,
    interval: BarInterval,
) -> None:
    name = st.text_input("Strategy Set Name")
    col_cancel, col_save = st.columns(2)
    with col_cancel:
        if st.button("Cancel", width="stretch"):
            st.session_state[_SHOW_DIALOG_KEY] = False
            st.rerun()
    with col_save:
        if st.button("Save", type="primary", width="stretch"):
            _save(repo, None, grid_rows, position_columns, market_key, interval, new_name=name)


def _save(
    repo: StrategySetRepository,
    selected_name: str | None,
    grid_rows: list[dict],
    position_columns: tuple[str, ...],
    market_key: str,
    interval: BarInterval,
    new_name: str | None,
    loaded_set: StrategySet | None = None,
) -> None:
    """Shared save logic for both the overwrite-existing and create-new
    paths. On success, clears the dialog-open flag (a no-op for the
    overwrite path, which never sets it), records the pending selection,
    and reruns -- st.rerun() never returns, so a validation error is the
    only way this function returns normally (leaving the dialog, if
    any, open with the error visible for the user to correct).

    `loaded_set`'s intermarket entries (if any) are passed straight
    through to build_strategy_set_from_grid(), which writes them back
    unchanged -- this is what makes load -> save -> reload of a mixed
    set lossless despite the grid being single-market only. It is NOT a
    second save path: the same, single repo.save() below persists both
    kinds of entry together, exactly as strategy_sets.serialization
    already writes them.
    """
    if selected_name is None:
        name = (new_name or "").strip()
        if not name:
            st.error("Enter a name for the new Strategy Set.")
            return
        if repo.exists(name):
            st.error(f"A Strategy Set named '{name}' already exists.")
            return
    else:
        name = selected_name

    intermarket_entries = () if loaded_set is None else loaded_set.intermarket_entries
    try:
        strategy_set: StrategySet = build_strategy_set_from_grid(
            name, grid_rows, position_columns, market_key, interval,
            intermarket_entries=intermarket_entries,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    repo.save(strategy_set)
    st.session_state[_SHOW_DIALOG_KEY] = False
    ss_state.set_message("success", f"Saved '{name}'.")
    ss_state.set_pending_selection(name)
    st.rerun()
