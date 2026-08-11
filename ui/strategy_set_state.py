"""
strategy_set_state.py

Session-state keys and accessor helpers for the Strategy Set selector
integrated into ui.controls' Strategy Templates section: which saved
set (if any) is currently loaded into the grid, the pending-selection
indirection the selector's widget-lifecycle fix depends on, and the
panel's own one-shot status message.

There is no separate "draft" state here (see the Module 7B
simplification: a Strategy Set is just a saved version of the ONE
Strategy Templates grid) -- the grid widget itself (owned by
ui.controls/Streamlit's own data_editor session state) IS the draft.
This module only tracks which saved name that grid currently reflects,
not its contents. It also does NOT need to track "did the selection
just change" anymore: since the grid carries its own per-row Market/
Interval (see ui.controls/ui.strategy_set_formatting), loading a set
never needs to override the scan bar's Market/Interval selectors, which
was the only reason an earlier version of this module tracked that.
"""

from __future__ import annotations

import streamlit as st

SELECTED_NAME = "oscill8_ss_selected_name"  # name of the loaded saved set, or None = new/unsaved
MESSAGE = "oscill8_ss_message"  # (level, text) | None -- "success"/"error"/"info"

# The Strategy Set name (or ui.strategy_set_view's "+ New Strategy Set"
# sentinel) that a lifecycle action (save) wants to become selected.
# NOT the selector widget's own session-state key -- Streamlit forbids
# writing to a widget's key after that widget has already been
# instantiated in the current script run, which is exactly what a Save
# action needs to do from further down the same script pass. This key
# is never bound to a widget, so it can be set freely from a callback;
# ui.strategy_set_view's selector applies it to the widget's key on the
# NEXT rerun, before that widget is (re)created -- the one point where
# doing so is legal.
PENDING_SELECTION = "oscill8_ss_pending_selection"


def init_state() -> None:
    st.session_state.setdefault(SELECTED_NAME, None)
    st.session_state.setdefault(MESSAGE, None)
    st.session_state.setdefault(PENDING_SELECTION, None)


def get_selected_name() -> str | None:
    return st.session_state.get(SELECTED_NAME)


def set_selected_name(name: str | None) -> None:
    st.session_state[SELECTED_NAME] = name


def set_message(level: str, text: str) -> None:
    st.session_state[MESSAGE] = (level, text)


def pop_message() -> tuple[str, str] | None:
    """Read and clear the pending status message -- shown at most once,
    on the rerun immediately after the action that set it."""
    message = st.session_state.get(MESSAGE)
    st.session_state[MESSAGE] = None
    return message


def set_pending_selection(value: str) -> None:
    """Record the name (or the "+ New Strategy Set" sentinel) that
    should become the selector's value on the next rerun -- see
    PENDING_SELECTION above for why this indirection exists."""
    st.session_state[PENDING_SELECTION] = value


def pop_pending_selection() -> str | None:
    """Read and clear the pending selection -- applied at most once, by
    the very next render of the selector."""
    value = st.session_state.get(PENDING_SELECTION)
    st.session_state[PENDING_SELECTION] = None
    return value
