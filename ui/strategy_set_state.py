"""
strategy_set_state.py

Session-state keys and accessor helpers for Module 7B's Strategy Set
panel: which saved set is currently loaded, the in-progress editor
draft (entries being added/removed/enabled-toggled before Save
persists them via strategy_sets.repository), and the panel's own
status/error banner.

Kept separate from ui.state (Module 6A/6B's scan-result state) -- this
module is about the Strategy Set *editor*, not the scan report/
selection/chart-history state ui.state already owns. Running a
selected set still writes into ui.state's existing SCAN_REQUEST/
SCAN_REPORT keys unchanged (see ui.strategy_set_view.handle_run_
strategy_set) -- no new scan-result state is introduced here.

A "draft" is a plain list[StrategySetEntry] (not yet a StrategySet),
since a StrategySet requires >=1 entry and unique entry names -- an
in-progress edit (e.g. a brand new set with zero entries so far, or a
transient duplicate name before the user renames it) must be able to
violate those invariants without raising. StrategySet's own validation
only runs at Save time, when the draft is turned into a real
StrategySet.
"""

from __future__ import annotations

import streamlit as st

from strategy_sets.model import StrategySetEntry

SELECTED_NAME = "oscill8_ss_selected_name"  # name of the loaded saved set, or None = new/unsaved
DRAFT_ENTRIES = "oscill8_ss_draft_entries"  # list[StrategySetEntry] currently being viewed/edited
DRAFT_DESCRIPTION = "oscill8_ss_draft_description"
MESSAGE = "oscill8_ss_message"  # (level, text) | None -- "success"/"error"/"info"


def init_state() -> None:
    st.session_state.setdefault(SELECTED_NAME, None)
    st.session_state.setdefault(DRAFT_ENTRIES, None)
    st.session_state.setdefault(DRAFT_DESCRIPTION, "")
    st.session_state.setdefault(MESSAGE, None)


def load_draft(name: str, entries: list[StrategySetEntry], description: str) -> None:
    """Load a saved set's entries into the draft for viewing/editing,
    replacing whatever draft was previously in progress."""
    st.session_state[SELECTED_NAME] = name
    st.session_state[DRAFT_ENTRIES] = list(entries)
    st.session_state[DRAFT_DESCRIPTION] = description


def start_new_draft() -> None:
    """Enter "create a new Strategy Set" mode: an empty, unsaved draft
    with no loaded name."""
    st.session_state[SELECTED_NAME] = None
    st.session_state[DRAFT_ENTRIES] = []
    st.session_state[DRAFT_DESCRIPTION] = ""


def get_selected_name() -> str | None:
    return st.session_state.get(SELECTED_NAME)


def get_draft_entries() -> list[StrategySetEntry] | None:
    return st.session_state.get(DRAFT_ENTRIES)


def set_draft_entries(entries: list[StrategySetEntry]) -> None:
    st.session_state[DRAFT_ENTRIES] = list(entries)


def get_draft_description() -> str:
    return st.session_state.get(DRAFT_DESCRIPTION, "")


def set_draft_description(description: str) -> None:
    st.session_state[DRAFT_DESCRIPTION] = description


def set_message(level: str, text: str) -> None:
    st.session_state[MESSAGE] = (level, text)


def pop_message() -> tuple[str, str] | None:
    """Read and clear the pending status message -- shown at most once,
    on the rerun immediately after the action that set it."""
    message = st.session_state.get(MESSAGE)
    st.session_state[MESSAGE] = None
    return message
