"""
tests/test_ui_strategy_set_selector_lifecycle.py

Regression coverage for a Streamlit widget-lifecycle bug in the
Strategy Set panel: create/save, rename, duplicate, and delete used to
write directly to the selector's own widget-owned session-state key
(ui.strategy_set_view._SELECTOR_KEY) from inside a button callback that
runs AFTER that widget has already been instantiated earlier in the
same script pass (the selector renders near the top of
render_strategy_set_panel(); Save/Rename/Duplicate/Delete live inside
the "Manage Strategy Set" expander, rendered further down the SAME
pass). Streamlit forbids that outright:

    StreamlitAPIException: st.session_state.oscill8_ss_selector cannot
    be modified after the widget with key oscill8_ss_selector is
    instantiated.

The fix routes every lifecycle action through a separate, non-widget
key (ui.strategy_set_state.PENDING_SELECTION via set_pending_selection/
pop_pending_selection) instead of the selector's own key, followed by
st.rerun(). _render_selector applies the pending value to the selector's
key on the FRESH rerun that follows, before st.selectbox() (re)creates
the widget -- the one point in the script where writing to that key is
legal.

These tests use the REAL Streamlit script via streamlit.testing.v1.
AppTest rather than a mocked `st` (see tests/test_ui_strategy_set_run.py
for the mocked-`st` style, which covers the run/propagation logic but
cannot observe widget-instantiation ordering at all): AppTest enforces
the actual widget-lifecycle rule, which is exactly why a fully-mocked
`st` never caught this bug in the first place -- mocking `st` away
hides the very constraint that broke.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

_APP_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")

from core import config
from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository


def _definition(weights=(1.0, -2.0, 1.0)) -> StrategyDefinition:
    return StrategyDefinition(
        market_key="SOFR", offsets=tuple(range(len(weights))), weights=weights, interval=BarInterval.DAILY,
    )


def _entry(name="SOFR Fly", enabled=True, weights=(1.0, -2.0, 1.0)) -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_definition(weights), enabled=enabled)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
    """Redirects the panel's own `StrategySetRepository()` (constructed
    with no explicit base_dir inside ui.strategy_set_view) to an
    isolated tmp_path directory -- config.STRATEGY_SETS_DIR is read
    fresh by StrategySetRepository.__init__ on every call, so patching
    the already-imported core.config module object here is visible to
    the AppTest-run script, which imports the SAME cached module.
    """
    directory = tmp_path / "strategy_sets"
    monkeypatch.setattr(config, "STRATEGY_SETS_DIR", str(directory))
    return StrategySetRepository(base_dir=str(directory))


def _app() -> AppTest:
    return AppTest.from_file(_APP_PATH, default_timeout=60)


def _selector(at: AppTest):
    return [s for s in at.selectbox if s.label == "Strategy Set"][0]


def _button(at: AppTest, label: str):
    return [b for b in at.button if b.label == label][0]


def _text_input(at: AppTest, label: str):
    return [t for t in at.text_input if t.label == label][0]


def _assert_no_exception(at: AppTest) -> None:
    exceptions = list(at.exception)
    assert not exceptions, f"Unexpected exception(s): {[e.value for e in exceptions]}"


# ---------------------------------------------------------------------
# A. Existing selection -- no regression
# ---------------------------------------------------------------------

def test_selecting_an_existing_set_loads_its_entries_and_preserves_enabled_state(repo):
    repo.save(
        StrategySet(
            name="6M Strategies",
            entries=(_entry("SOFR 6M Fly", enabled=True), _entry("SONIA Fly", enabled=False)),
        )
    )

    at = _app()
    at.run()
    _assert_no_exception(at)

    _selector(at).select("6M Strategies").run()
    _assert_no_exception(at)
    assert _selector(at).value == "6M Strategies"

    tables = [df.value for df in at.dataframe if "Enabled" in df.value.columns]
    assert len(tables) == 1
    table = tables[0].set_index("Name")
    assert bool(table.loc["SOFR 6M Fly", "Enabled"]) is True
    assert bool(table.loc["SONIA Fly", "Enabled"]) is False


# ---------------------------------------------------------------------
# B. Create + Save -> new set becomes selected
# ---------------------------------------------------------------------

def test_create_new_set_and_save_selects_it_with_no_exception(repo):
    at = _app()
    # Bypass the Add-Strategy grid (a st.data_editor, not drivable via
    # AppTest -- see tests/test_ui_strategy_set_formatting.py for that
    # translation logic's own direct unit coverage) by seeding the draft
    # the same way clicking "Add to Set" would have left it.
    at.session_state["oscill8_ss_selected_name"] = None
    at.session_state["oscill8_ss_draft_entries"] = [_entry("SOFR Fly")]
    at.run()
    _assert_no_exception(at)

    _text_input(at, "New Strategy Set name").set_value("My New Set").run()
    _button(at, "Save").click().run()

    _assert_no_exception(at)
    assert repo.exists("My New Set")
    assert _selector(at).value == "My New Set"
    assert "My New Set" in _selector(at).options


def test_saving_an_existing_selected_set_keeps_it_selected_with_no_exception(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    _button(at, "Save").click().run()

    _assert_no_exception(at)
    assert _selector(at).value == "6M Strategies"


# ---------------------------------------------------------------------
# D. Rename
# ---------------------------------------------------------------------

def test_rename_selects_the_renamed_set_and_drops_the_old_name(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    _text_input(at, "Rename to").set_value("6M Churning").run()
    _button(at, "Rename").click().run()

    _assert_no_exception(at)
    assert _selector(at).value == "6M Churning"
    assert "6M Strategies" not in _selector(at).options
    assert not repo.exists("6M Strategies")
    assert repo.exists("6M Churning")


# ---------------------------------------------------------------------
# E. Duplicate
# ---------------------------------------------------------------------

def test_duplicate_selects_the_copy_and_leaves_the_original_unchanged(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    _text_input(at, "Duplicate as").set_value("6M Strategies Test").run()
    _button(at, "Duplicate").click().run()

    _assert_no_exception(at)
    assert _selector(at).value == "6M Strategies Test"
    assert repo.exists("6M Strategies")
    assert repo.exists("6M Strategies Test")
    assert repo.load("6M Strategies").entries == repo.load("6M Strategies Test").entries


# ---------------------------------------------------------------------
# F. Delete
# ---------------------------------------------------------------------

def test_delete_selected_set_falls_back_to_a_remaining_set(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))
    repo.save(StrategySet(name="Churning", entries=(_entry(),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    [c for c in at.checkbox if c.label == "Confirm delete"][0].set_value(True).run()
    _button(at, "Delete").click().run()

    _assert_no_exception(at)
    assert not repo.exists("6M Strategies")
    assert _selector(at).value == "Churning"


def test_delete_the_last_remaining_set_falls_back_to_new_set_option(repo):
    repo.save(StrategySet(name="Only Set", entries=(_entry(),)))

    at = _app()
    at.run()
    _selector(at).select("Only Set").run()

    [c for c in at.checkbox if c.label == "Confirm delete"][0].set_value(True).run()
    _button(at, "Delete").click().run()

    _assert_no_exception(at)
    assert not repo.exists("Only Set")
    assert _selector(at).value == "+ New Strategy Set"


# ---------------------------------------------------------------------
# G. In-memory (unsaved) draft is what Run actually uses
# ---------------------------------------------------------------------

def test_run_uses_the_unsaved_in_memory_draft_not_the_saved_file(repo, mocker):
    # On disk, both entries are enabled.
    saved = StrategySet(
        name="6M Strategies",
        entries=(_entry("SOFR Fly", enabled=True), _entry("SONIA Fly", enabled=True)),
    )
    repo.save(saved)

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    # Simulate unticking "SONIA Fly" in the entries table (a data_editor
    # checkbox edit, not drivable via AppTest) by writing the same
    # disabled entry directly into the draft, exactly what
    # apply_enabled_edits() would have produced -- see
    # tests/test_ui_strategy_set_formatting.py for that function's own
    # direct unit coverage. "SOFR Fly" stays enabled so the Run button
    # itself stays enabled (it disables only when NOTHING is enabled).
    at.session_state["oscill8_ss_draft_entries"] = [
        _entry("SOFR Fly", enabled=True),
        _entry("SONIA Fly", enabled=False),
    ]

    import ui.strategy_set_view as strategy_set_view

    mock_expand = mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[])
    mock_run = mocker.patch.object(strategy_set_view, "run_scan_on_instances")

    _button(at, "▶ Run '6M Strategies'").click().run()

    _assert_no_exception(at)
    mock_expand.assert_called_once()
    (strategy_set_arg, *_rest), _kwargs = mock_expand.call_args
    entries_by_name = {e.name: e.enabled for e in strategy_set_arg.entries}
    # The in-memory (unsaved) toggle -- not the saved file's still-fully-
    # enabled version -- is what reached expand_strategy_set().
    assert entries_by_name == {"SOFR Fly": True, "SONIA Fly": False}

    # The on-disk file is untouched -- the toggle was never saved.
    on_disk = {e.name: e.enabled for e in repo.load("6M Strategies").entries}
    assert on_disk == {"SOFR Fly": True, "SONIA Fly": True}


# ---------------------------------------------------------------------
# Manual scanner regression -- untouched by any of the above
# ---------------------------------------------------------------------

def test_manual_scan_bar_is_unaffected_by_strategy_set_lifecycle_actions(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()
    _text_input(at, "Rename to").set_value("6M Churning").run()
    _button(at, "Rename").click().run()

    _assert_no_exception(at)
    # The manual scan bar's own controls (market/interval/run scan
    # button) are still present and unaffected.
    assert any(s.label == "Market" for s in at.selectbox)
    assert any(b.label == "▶ Run Scan" for b in at.button)
