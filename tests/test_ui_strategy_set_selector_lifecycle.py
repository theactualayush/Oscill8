"""
tests/test_ui_strategy_set_selector_lifecycle.py

Regression + integration coverage for the Module 7B simplification:
Strategy Sets became saved versions of the ONE Strategy Templates grid
(no separate Strategy Set section/table/Run button/Manage panel), the
Universe became fully automatic (no manual date inputs), and History
now defaults to the last six months.

These tests use the REAL Streamlit script via streamlit.testing.v1.
AppTest rather than a mocked `st`: AppTest enforces the actual widget-
instantiation rule the selector's pending-selection fix depends on
(see ui.strategy_set_state.PENDING_SELECTION / ui.strategy_set_view.
render_selector), which a mocked `st` cannot observe -- this is the
same rationale the original selector-lifecycle bug-fix tests used, and
is preserved unchanged by this rewrite.

st.data_editor's canvas grid isn't drivable via AppTest (no
`data_editor` accessor exists in this Streamlit version's testing
API), so tests that need REAL grid content either (a) load an existing
saved Strategy Set (which seeds the grid with real rows without typing
into it) or (b) call the pure translation functions directly (see
tests/test_ui_strategy_set_formatting.py for that layer's own direct
coverage). Only the "no content" paths (blank "+ New Strategy Set")
are exercised for save-validation here.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import config
from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository
from template_scanner.scanner import ScanReport
import ui.scan_view as scan_view

_APP_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
_TODAY = date.today()


def _definition(weights=(1.0, -2.0, 1.0), market_key="SOFR", interval=BarInterval.DAILY) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=tuple(range(len(weights))), weights=weights, interval=interval,
    )


def _entry(name="SOFR Fly", weights=(1.0, -2.0, 1.0), market_key="SOFR", interval=BarInterval.DAILY) -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_definition(weights, market_key, interval))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
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
# 1/3/4/5/20: fresh load -- blank grid, no second panel, single Run button
# ---------------------------------------------------------------------

def test_fresh_load_has_blank_grid_and_no_second_strategy_set_panel(repo):
    at = _app()
    at.run()
    _assert_no_exception(at)

    assert _selector(at).value == "+ New Strategy Set"

    # Exactly one Run button, and it's the manual scan's -- no
    # "Run Strategy Set" of any kind exists anymore.
    run_labels = [b.label for b in at.button if "Run" in b.label]
    assert run_labels == ["▶ Run Scan"]
    assert "Run Strategy Set" not in {b.label for b in at.button}

    expander_titles = [getattr(exp, "label", None) for exp in at.expander]
    assert "Manage Strategy Set" not in expander_titles

    # Exactly one grid on the page (the Strategy Templates grid) --
    # no second "Strategies in Set" table.
    assert len(at.dataframe) <= 1


def test_no_manage_strategy_set_panel_or_lifecycle_buttons_exist(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))
    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()
    _assert_no_exception(at)

    labels = {b.label for b in at.button}
    for removed in ("Run Strategy Set", "Rename", "Duplicate", "Delete", "Add to Set", "Remove"):
        assert removed not in labels

    expander_titles = [getattr(exp, "label", None) for exp in at.expander]
    assert "Manage Strategy Set" not in expander_titles


# ---------------------------------------------------------------------
# 2/8/9: existing Strategy Set loads into the SAME grid; Run Scan uses it
# ---------------------------------------------------------------------

def test_selecting_an_existing_set_loads_its_own_market_and_interval_into_the_grid_row(repo):
    # The grid carries its OWN per-row Market/Interval (the multi-market
    # fix) -- loading a set never needs to, and no longer does, touch
    # the scan bar's top-level Market/Interval selectors. What matters
    # for correctness is the loaded ROW's own cell values.
    repo.save(
        StrategySet(
            name="6M Strategies",
            entries=(_entry(name="SONIA Fly", market_key="SONIA", interval=BarInterval.HOURLY),),
        )
    )

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()
    _assert_no_exception(at)

    grid = [df.value for df in at.dataframe if "Label" in df.value.columns][0]
    row = grid.set_index("Label").loc["SONIA Fly"]
    assert row["Market"] == "SONIA"
    assert row["Interval"] == "HOURLY"


def test_run_scan_uses_the_loaded_sets_exact_weights_via_the_same_run_scan_path(repo, mocker):
    repo.save(
        StrategySet(
            name="6M Strategies",
            entries=(_entry(name="SOFR Fly", weights=(1.0, -2.0, 1.0)),),
        )
    )

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    mock_run = mocker.patch.object(scan_view, "run_scan", return_value=ScanReport(results=()))
    _button(at, "▶ Run Scan").click().run()

    _assert_no_exception(at)
    mock_run.assert_called_once()
    request = mock_run.call_args[0][0]
    assert len(request.definitions) == 1
    assert request.definitions[0].weights == (1.0, -2.0, 1.0)
    assert request.definitions[0].market_key == "SOFR"


# ---------------------------------------------------------------------
# 6: Save an existing (loaded, unmodified) set overwrites it
# ---------------------------------------------------------------------

def test_save_on_an_existing_selected_set_overwrites_it(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(weights=(1.0, -2.0, 1.0)),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    _button(at, "Save Strategy Set").click().run()

    _assert_no_exception(at)
    reloaded = repo.load("6M Strategies")
    assert reloaded.entries[0].definition.weights == (1.0, -2.0, 1.0)
    assert _selector(at).value == "6M Strategies"


# ---------------------------------------------------------------------
# 7: Save on "+ New Strategy Set" prompts for a name (a real Streamlit dialog)
# ---------------------------------------------------------------------

def test_save_on_new_strategy_set_opens_a_name_prompt(repo):
    at = _app()
    at.run()

    _button(at, "Save Strategy Set").click().run()

    _assert_no_exception(at)
    assert any(t.label == "Strategy Set Name" for t in at.text_input)
    assert any(b.label == "Cancel" for b in at.button)
    assert any(b.label == "Save" for b in at.button)


def test_save_on_new_strategy_set_with_a_blank_grid_errors_without_creating_a_file(repo):
    at = _app()
    at.run()

    _button(at, "Save Strategy Set").click().run()
    _text_input(at, "Strategy Set Name").set_value("My New Set").run()
    _button(at, "Save").click().run()

    _assert_no_exception(at)
    assert not repo.exists("My New Set")
    assert any(e.value for e in at.error)


def test_cancel_closes_the_name_prompt_without_creating_a_file(repo):
    """Regression-investigated (see the investigation note below):
    Cancel's production handler (ui.strategy_set_view._save_new_dialog)
    sets st.session_state["oscill8_ss_show_save_dialog"] = False and
    calls st.rerun() -- confirmed directly against real session_state,
    not inferred -- which is the exact flag render_save_controls()
    checks before ever calling _save_new_dialog() (the only place
    st.text_input("Strategy Set Name") is created) again. That flag is
    therefore the authoritative, production-code-driven signal that the
    prompt is closed, and is asserted directly here.

    `assert not any(t.label == "Strategy Set Name" for t in
    at.text_input)` was tried first and does NOT hold: Streamlit
    testing.v1 AppTest's ForwardMsgQueue (streamlit/testing/v1/
    local_script_runner.py) is never cleared between .run() calls, and
    element_tree.parse_tree_from_messages() rebuilds the whole element
    tree from that queue's full cumulative history on every .run() --
    so a widget whose containing code path stops executing (the closed
    dialog, here) leaves its last-rendered delta sitting in the queue
    forever, and AppTest's at.text_input keeps listing it. Confirmed
    this is stale metadata, not a live widget: reading that stale
    node's own .value raises KeyError (its session_state entry was
    already pruned by Streamlit's normal end-of-run widget cleanup) --
    proving the real widget is gone even though AppTest's tree still
    references it. This is a testing-harness-only artifact: a live
    browser session has no analogous "replay the full message history"
    step, so this can never affect a real user. Do not restore the
    at.text_input assertion, and do not call .run() again on `at` past
    this point in this test -- AppTest's own Block.run() re-evaluates
    every node's value (including stale ones) to snapshot widget
    state, which raises the same KeyError against the pruned entry.
    """
    at = _app()
    at.run()

    _button(at, "Save Strategy Set").click().run()
    _text_input(at, "Strategy Set Name").set_value("Abandoned Set").run()
    _button(at, "Cancel").click().run()

    _assert_no_exception(at)
    assert not repo.exists("Abandoned Set")
    assert at.session_state["oscill8_ss_show_save_dialog"] is False


# ---------------------------------------------------------------------
# 14/15/16: Universe is fully automatic -- no manual date inputs
# ---------------------------------------------------------------------

def test_no_universe_date_input_widgets_exist(repo):
    at = _app()
    at.run()
    _assert_no_exception(at)

    labels = {d.label for d in at.date_input}
    assert "Contract Start" not in labels
    assert "Contract End" not in labels
    # Only the two History date inputs remain.
    assert labels == {"Price History Start", "Price History End"}


def test_universe_indicator_is_shown_instead_of_date_inputs(repo):
    at = _app()
    at.run()
    _assert_no_exception(at)

    all_text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    infos = " ".join(i.value for i in at.info) if hasattr(at, "info") else ""
    assert "Automatic" in infos or "Automatic" in all_text


def test_active_contract_universe_starts_from_today(repo, mocker):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))
    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    mock_run = mocker.patch.object(scan_view, "run_scan", return_value=ScanReport(results=()))
    _button(at, "▶ Run Scan").click().run()

    request = mock_run.call_args[0][0]
    assert request.contract_start == _TODAY


# ---------------------------------------------------------------------
# 17/18/19: History defaults to six months, still user-editable
# ---------------------------------------------------------------------

def test_default_history_end_is_today(repo):
    at = _app()
    at.run()
    price_end = [d for d in at.date_input if d.label == "Price History End"][0]
    assert price_end.value == _TODAY


def test_default_history_start_is_about_six_months_before_today(repo):
    at = _app()
    at.run()
    price_start = [d for d in at.date_input if d.label == "Price History Start"][0]
    delta_days = (_TODAY - price_start.value).days
    assert 175 <= delta_days <= 190


def test_history_dates_remain_user_editable(repo, mocker):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))
    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    custom_start = _TODAY - timedelta(days=30)
    [d for d in at.date_input if d.label == "Price History Start"][0].set_value(custom_start).run()
    _assert_no_exception(at)

    mock_run = mocker.patch.object(scan_view, "run_scan", return_value=ScanReport(results=()))
    _button(at, "▶ Run Scan").click().run()

    request = mock_run.call_args[0][0]
    assert request.price_start == custom_start


# ---------------------------------------------------------------------
# 21/22: manual scan workflow (no Strategy Set touched) still works
# ---------------------------------------------------------------------

def test_manual_scan_workflow_is_unaffected_when_no_strategy_set_is_selected(repo, mocker):
    at = _app()
    at.run()
    _assert_no_exception(at)
    assert _selector(at).value == "+ New Strategy Set"

    # Manual Run Scan with nothing in the grid should error cleanly
    # (no strategy rows), exactly as before this whole change.
    mock_run = mocker.patch.object(scan_view, "run_scan", return_value=ScanReport(results=()))
    _button(at, "▶ Run Scan").click().run()
    _assert_no_exception(at)
    mock_run.assert_not_called()
    assert any(e.value for e in at.error)


def test_switching_strategy_set_selection_does_not_raise(repo):
    """The original selector-lifecycle bug: writing to the selector's
    own widget key after it was already instantiated in the same
    script pass. Save (the only lifecycle action left) still exercises
    the fix -- see ui.strategy_set_state.set_pending_selection."""
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))
    repo.save(StrategySet(name="Churning", entries=(_entry(name="Other"),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()
    _assert_no_exception(at)
    _selector(at).select("Churning").run()
    _assert_no_exception(at)
    _selector(at).select("+ New Strategy Set").run()
    _assert_no_exception(at)

    _button(at, "Save Strategy Set").click().run()
    _text_input(at, "Strategy Set Name").set_value("Fresh Set").run()
    _button(at, "Save").click().run()
    _assert_no_exception(at)
    # Empty grid -- expected validation error, but critically no
    # StreamlitAPIException from the selector's widget lifecycle.
    assert not repo.exists("Fresh Set")
