"""
tests/test_ui_strategy_set_state.py

Tests for Module 7B's Strategy Set panel state (ui/strategy_set_state.py):
loading a saved set into the draft (what "Selecting a Strategy Set" and
"Displaying its entries" mean at the state layer, one level below the
actual Streamlit widget rendering ui.strategy_set_view does over this
state -- see that module's own tests for the run/save/rename/duplicate/
delete behavior built on top of it), starting a new/unsaved draft, and
the one-shot status message.

Like ui.state (Module 6A/6B's own session-state module, which has no
dedicated test file), this exercises real streamlit session_state
directly -- it already works outside a running Streamlit script.
Because that session_state is a process-wide singleton, every test
starts from an explicit start_new_draft() rather than relying on
import-time defaults, so no state leaks in from another test.
"""

from __future__ import annotations

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import StrategySetEntry

import ui.strategy_set_state as ss_state


def _entry(name="SOFR Fly") -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0), interval=BarInterval.DAILY,
    )
    return StrategySetEntry(name=name, definition=definition)


def setup_function() -> None:
    ss_state.init_state()
    ss_state.start_new_draft()
    ss_state.pop_message()  # drain any leftover message from a prior test


# ---------------------------------------------------------------------
# start_new_draft / load_draft -- "no selection yet" vs. "selecting a set"
# ---------------------------------------------------------------------

def test_start_new_draft_has_no_selected_name_and_empty_entries():
    ss_state.start_new_draft()
    assert ss_state.get_selected_name() is None
    assert ss_state.get_draft_entries() == []
    assert ss_state.get_draft_description() == ""


def test_load_draft_records_the_selected_name_and_entries():
    entries = [_entry("SOFR Fly"), _entry("SOFR Spread")]
    ss_state.load_draft("6M Strategies", entries, "quarterly rolls")

    assert ss_state.get_selected_name() == "6M Strategies"
    assert ss_state.get_draft_entries() == entries
    assert ss_state.get_draft_description() == "quarterly rolls"


def test_load_draft_copies_the_entries_list_at_load_time():
    original = [_entry("SOFR Fly")]
    ss_state.load_draft("Churning", original, "")

    original.append(_entry("SOFR Spread"))

    # mutating the caller's own list after load_draft() must not
    # retroactively change the stored draft.
    assert [e.name for e in ss_state.get_draft_entries()] == ["SOFR Fly"]


def test_switching_from_a_loaded_set_to_new_draft_clears_selection():
    ss_state.load_draft("6M Strategies", [_entry()], "")
    ss_state.start_new_draft()

    assert ss_state.get_selected_name() is None
    assert ss_state.get_draft_entries() == []


def test_loading_a_different_set_replaces_the_previous_draft():
    ss_state.load_draft("6M Strategies", [_entry("SOFR Fly")], "")
    ss_state.load_draft("Churning", [_entry("SOFR Spread")], "desc")

    assert ss_state.get_selected_name() == "Churning"
    assert [e.name for e in ss_state.get_draft_entries()] == ["SOFR Spread"]


# ---------------------------------------------------------------------
# set_draft_entries / set_draft_description
# ---------------------------------------------------------------------

def test_set_draft_entries_updates_in_place_without_changing_selected_name():
    ss_state.load_draft("Churning", [_entry("A")], "")
    ss_state.set_draft_entries([_entry("A"), _entry("B")])

    assert ss_state.get_selected_name() == "Churning"
    assert [e.name for e in ss_state.get_draft_entries()] == ["A", "B"]


def test_set_draft_description_updates_independently_of_entries():
    ss_state.load_draft("Churning", [_entry()], "old")
    ss_state.set_draft_description("new description")

    assert ss_state.get_draft_description() == "new description"
    assert len(ss_state.get_draft_entries()) == 1


# ---------------------------------------------------------------------
# Status message: set once, read once
# ---------------------------------------------------------------------

def test_message_is_returned_once_then_cleared():
    ss_state.set_message("success", "Saved 'Churning'.")

    assert ss_state.pop_message() == ("success", "Saved 'Churning'.")
    assert ss_state.pop_message() is None


def test_no_message_by_default():
    assert ss_state.pop_message() is None
