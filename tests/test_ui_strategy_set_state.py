"""
tests/test_ui_strategy_set_state.py

Tests for the Strategy Set selector's session-state (ui/
strategy_set_state.py): which saved set (if any) is currently loaded,
the pending-selection indirection its widget-lifecycle fix depends on,
and the one-shot status message.

There is no "draft" state anymore (Module 7B simplification: a
Strategy Set is a saved version of the ONE Strategy Templates grid --
the grid widget itself is the draft, not a separate list this module
tracks). There is also no "did the selection just change" tracker
anymore -- an earlier version of this module had one, used to decide
whether to override the scan bar's Market/Interval selectors when a
set loaded; the multi-market fix (each grid row now carries its own
Market/Interval, see ui.formatting.MARKET_COLUMN/INTERVAL_COLUMN) made
that override unnecessary, so the tracker was removed along with it.

Like ui.state (Module 6A/6B's own session-state module, which has no
dedicated test file), this exercises real streamlit session_state
directly -- it already works outside a running Streamlit script.
Because that session_state is a process-wide singleton, every test
starts from an explicit reset rather than relying on import-time
defaults, so no state leaks in from another test.
"""

from __future__ import annotations

import ui.strategy_set_state as ss_state


def setup_function() -> None:
    ss_state.init_state()
    ss_state.set_selected_name(None)
    ss_state.pop_message()  # drain any leftover message from a prior test
    ss_state.pop_pending_selection()


# ---------------------------------------------------------------------
# Selected name
# ---------------------------------------------------------------------

def test_selected_name_defaults_to_none():
    assert ss_state.get_selected_name() is None


def test_set_selected_name_round_trips():
    ss_state.set_selected_name("6M Strategies")
    assert ss_state.get_selected_name() == "6M Strategies"


def test_set_selected_name_back_to_none_means_new_set():
    ss_state.set_selected_name("Churning")
    ss_state.set_selected_name(None)
    assert ss_state.get_selected_name() is None


# ---------------------------------------------------------------------
# Status message: set once, read once
# ---------------------------------------------------------------------

def test_message_is_returned_once_then_cleared():
    ss_state.set_message("success", "Saved 'Churning'.")

    assert ss_state.pop_message() == ("success", "Saved 'Churning'.")
    assert ss_state.pop_message() is None


def test_no_message_by_default():
    assert ss_state.pop_message() is None


# ---------------------------------------------------------------------
# Pending selection: applied once by the selector's next render
# ---------------------------------------------------------------------

def test_pending_selection_is_returned_once_then_cleared():
    ss_state.set_pending_selection("6M Strategies")

    assert ss_state.pop_pending_selection() == "6M Strategies"
    assert ss_state.pop_pending_selection() is None


def test_no_pending_selection_by_default():
    assert ss_state.pop_pending_selection() is None


def test_consume_selection_change_no_longer_exists():
    # Locks in the multi-market fix's removal of the "did the selection
    # just change" tracker -- per-row Market/Interval made overriding
    # the scan bar's selectors on load unnecessary (see the module
    # docstring), so this helper was deleted rather than left unused.
    assert not hasattr(ss_state, "consume_selection_change")
