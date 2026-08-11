"""
tests/test_ui_strategy_set_run.py

Tests for Module 7B's run/lifecycle integration in ui/strategy_set_view.py:

  - handle_run_strategy_set() correctly propagates the shared Universe
    window (ScanSetup.contract_start/contract_end) into strategy_sets.
    expansion.expand_strategy_set(), and the shared History/Lookback/
    Percentile window into the new template_scanner.scanner.
    run_scan_on_instances() -- then stores the result in ui.state
    exactly where ui.scan_view.handle_run_scan stores a manual scan's
    result, so ui.results_view/ui.chart_view need no Strategy-Set-aware
    branch.
  - _handle_save/_handle_rename/_handle_duplicate/_handle_delete drive
    the real, unmodified StrategySetRepository (an isolated tmp_path
    directory, never the real data/strategy_sets/).
  - The manual scan workflow (ui.scan_view/ui.controls) has no
    dependency on this module -- it is unaffected by anything here.

Only the module-level `st` alias inside ui.strategy_set_view is
mocked -- st.error/st.success/st.spinner/st.rerun are UI-only calls
with no logic of their own to verify. ui.state's real session-state
helpers are used throughout (they already work outside a running
Streamlit script -- see ui.state's own lack of a dedicated test file).
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

import ui.strategy_set_view as strategy_set_view
from core.config import BarInterval
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository
from template_scanner.scanner import ScanReport
from ui import state
from ui.controls import ScanSetup


def _setup(**overrides) -> ScanSetup:
    defaults = dict(
        market_key="SOFR",
        interval=BarInterval.DAILY,
        contract_start=date(2026, 1, 1),
        contract_end=date(2026, 12, 31),
        price_start=date(2020, 1, 1),
        price_end=date(2020, 6, 30),
        lookbacks=(20, 40),
        display_lookback=20,
        lower_percentile=5.0,
        upper_percentile=95.0,
        grid_rows=[],
        position_columns=(),
        run_clicked=False,
    )
    defaults.update(overrides)
    return ScanSetup(**defaults)


def _definition() -> StrategyDefinition:
    return StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0), interval=BarInterval.DAILY,
    )


def _entry(name="SOFR Fly", enabled=True) -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_definition(), enabled=enabled)


def _strategy_set(*entries) -> StrategySet:
    return StrategySet(name="Test Set", entries=tuple(entries))


_STATE_KEYS = (
    state.SCAN_REQUEST,
    state.SCAN_REPORT,
    state.DISPLAY_LOOKBACK,
    state.SELECTED_CANDIDATE,
    state.SELECTED_RANK,
    state.SELECTED_HISTORY,
    state.SCAN_ERROR,
)


def _reset_scan_state() -> None:
    # ui.state.init_state() only setdefault()s -- real streamlit
    # session_state is a process-wide singleton that otherwise leaks a
    # previous test's ScanReport/error across test functions, so each
    # key is force-cleared here before re-seeding fresh defaults.
    for key in _STATE_KEYS:
        state.st.session_state.pop(key, None)
    state.init_state()


@pytest.fixture(autouse=True)
def _clean_scan_state():
    _reset_scan_state()
    yield
    _reset_scan_state()


# ---------------------------------------------------------------------
# handle_run_strategy_set: contract-window / price-window propagation
# ---------------------------------------------------------------------

def test_handle_run_strategy_set_propagates_contract_window_into_expansion(mocker):
    mocker.patch.object(strategy_set_view, "st")
    fake_instance = StrategyInstance(definition=_definition(), rics=("SRAH26", "SRAM26", "SRAU26"))
    mock_expand = mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[fake_instance])
    mocker.patch.object(strategy_set_view, "run_scan_on_instances", return_value=ScanReport(results=()))

    setup = _setup(contract_start=date(2026, 3, 1), contract_end=date(2027, 3, 1))
    strategy_set = _strategy_set(_entry())

    strategy_set_view.handle_run_strategy_set(setup, strategy_set)

    args, _ = mock_expand.call_args
    assert args[0] is strategy_set
    assert args[1] == date(2026, 3, 1)
    assert args[2] == date(2027, 3, 1)


def test_handle_run_strategy_set_propagates_price_window_and_lookbacks(mocker):
    mocker.patch.object(strategy_set_view, "st")
    fake_instance = StrategyInstance(definition=_definition(), rics=("SRAH26", "SRAM26", "SRAU26"))
    mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[fake_instance])
    mock_run = mocker.patch.object(strategy_set_view, "run_scan_on_instances", return_value=ScanReport(results=()))

    setup = _setup(price_start=date(2019, 1, 1), price_end=date(2019, 12, 31), lookbacks=(20, 60))
    strategy_set_view.handle_run_strategy_set(setup, _strategy_set(_entry()))

    args, kwargs = mock_run.call_args
    assert args[0] == [fake_instance]
    assert args[1] == date(2019, 1, 1)
    assert args[2] == date(2019, 12, 31)
    assert kwargs["lookbacks"] == (20, 60)


def test_handle_run_strategy_set_propagates_percentile_range(mocker):
    mocker.patch.object(strategy_set_view, "st")
    fake_instance = StrategyInstance(definition=_definition(), rics=("SRAH26", "SRAM26", "SRAU26"))
    mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[fake_instance])
    mock_run = mocker.patch.object(strategy_set_view, "run_scan_on_instances", return_value=ScanReport(results=()))

    setup = _setup(lower_percentile=25.0, upper_percentile=75.0)
    strategy_set_view.handle_run_strategy_set(setup, _strategy_set(_entry()))

    _, kwargs = mock_run.call_args
    assert kwargs["lower_percentile"] == 25.0
    assert kwargs["upper_percentile"] == 75.0


def test_handle_run_strategy_set_stores_result_like_a_manual_scan(mocker):
    mocker.patch.object(strategy_set_view, "st")
    fake_instance = StrategyInstance(definition=_definition(), rics=("SRAH26", "SRAM26", "SRAU26"))
    mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[fake_instance])
    report = ScanReport(results=())
    mocker.patch.object(strategy_set_view, "run_scan_on_instances", return_value=report)

    setup = _setup(display_lookback=40)
    strategy_set_view.handle_run_strategy_set(setup, _strategy_set(_entry()))

    assert state.get_scan_report() is report
    assert state.get_display_lookback() == 40
    request = state.get_scan_request()
    assert request is not None
    assert request.contract_start == setup.contract_start
    assert request.price_start == setup.price_start


def test_handle_run_strategy_set_only_carries_enabled_definitions_into_request_metadata(mocker):
    mocker.patch.object(strategy_set_view, "st")
    fake_instance = StrategyInstance(definition=_definition(), rics=("SRAH26", "SRAM26", "SRAU26"))
    mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[fake_instance])
    mocker.patch.object(strategy_set_view, "run_scan_on_instances", return_value=ScanReport(results=()))

    enabled = _entry(name="Enabled Fly", enabled=True)
    disabled = _entry(name="Disabled Fly", enabled=False)
    strategy_set_view.handle_run_strategy_set(_setup(), _strategy_set(enabled, disabled))

    request = state.get_scan_request()
    assert len(request.definitions) == 1


def test_handle_run_strategy_set_errors_when_nothing_is_enabled(mocker):
    mock_st = mocker.patch.object(strategy_set_view, "st")
    mock_expand = mocker.patch.object(strategy_set_view, "expand_strategy_set")

    strategy_set_view.handle_run_strategy_set(_setup(), _strategy_set(_entry(enabled=False)))

    mock_st.error.assert_called_once()
    mock_expand.assert_not_called()
    assert state.get_scan_report() is None


def test_handle_run_strategy_set_errors_when_no_lookback_selected(mocker):
    mock_st = mocker.patch.object(strategy_set_view, "st")
    mock_expand = mocker.patch.object(strategy_set_view, "expand_strategy_set")

    strategy_set_view.handle_run_strategy_set(_setup(display_lookback=None), _strategy_set(_entry()))

    mock_st.error.assert_called_once()
    mock_expand.assert_not_called()


def test_handle_run_strategy_set_errors_when_expansion_yields_no_candidates(mocker):
    mock_st = mocker.patch.object(strategy_set_view, "st")
    mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[])
    mock_run = mocker.patch.object(strategy_set_view, "run_scan_on_instances")

    strategy_set_view.handle_run_strategy_set(_setup(), _strategy_set(_entry()))

    mock_st.error.assert_called_once()
    mock_run.assert_not_called()
    assert state.get_scan_report() is None


def test_handle_run_strategy_set_stores_scan_error_on_unexpected_exception(mocker):
    mocker.patch.object(strategy_set_view, "st")
    fake_instance = StrategyInstance(definition=_definition(), rics=("SRAH26",))
    mocker.patch.object(strategy_set_view, "expand_strategy_set", return_value=[fake_instance])
    mocker.patch.object(
        strategy_set_view, "run_scan_on_instances", side_effect=RuntimeError("simulated LSEG failure")
    )

    strategy_set_view.handle_run_strategy_set(_setup(), _strategy_set(_entry()))

    assert state.get_scan_report() is None
    stored_error = state.st.session_state.get(state.SCAN_ERROR)
    assert stored_error is not None and "simulated LSEG failure" in stored_error


# ---------------------------------------------------------------------
# Save / Rename / Duplicate / Delete lifecycle
# ---------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path) -> StrategySetRepository:
    return StrategySetRepository(base_dir=str(tmp_path / "strategy_sets"))


def test_handle_save_creates_a_new_set(mocker, repo):
    mocker.patch.object(strategy_set_view, "st")
    strategy_set_view._handle_save(repo, None, "My New Set", [_entry()], "")

    assert repo.exists("My New Set")
    assert repo.load("My New Set").entries[0].name == "SOFR Fly"


def test_handle_save_rejects_collision_when_creating_new(mocker, repo):
    mock_st = mocker.patch.object(strategy_set_view, "st")
    repo.save(_strategy_set(_entry()))

    strategy_set_view._handle_save(repo, None, "Test Set", [_entry()], "")

    mock_st.error.assert_called_once()


def test_handle_save_requires_at_least_one_entry(mocker, repo):
    mock_st = mocker.patch.object(strategy_set_view, "st")
    strategy_set_view._handle_save(repo, None, "Empty Set", [], "")

    mock_st.error.assert_called_once()
    assert not repo.exists("Empty Set")


def test_handle_save_overwrites_the_currently_loaded_set(mocker, repo):
    mocker.patch.object(strategy_set_view, "st")
    repo.save(_strategy_set(_entry(name="A", enabled=True)))

    strategy_set_view._handle_save(repo, "Test Set", "Test Set", [_entry(name="A", enabled=False)], "")

    assert repo.load("Test Set").entries[0].enabled is False


def test_handle_rename_moves_the_saved_file(mocker, repo):
    mocker.patch.object(strategy_set_view, "st")
    repo.save(_strategy_set(_entry()))

    strategy_set_view._handle_rename(repo, "Test Set", "Renamed Set")

    assert not repo.exists("Test Set")
    assert repo.exists("Renamed Set")


def test_handle_duplicate_keeps_the_original(mocker, repo):
    mocker.patch.object(strategy_set_view, "st")
    repo.save(_strategy_set(_entry()))

    strategy_set_view._handle_duplicate(repo, "Test Set", "Test Set Copy")

    assert repo.exists("Test Set")
    assert repo.exists("Test Set Copy")


def test_handle_delete_removes_the_saved_file(mocker, repo):
    mocker.patch.object(strategy_set_view, "st")
    repo.save(_strategy_set(_entry()))

    strategy_set_view._handle_delete(repo, "Test Set")

    assert not repo.exists("Test Set")


# ---------------------------------------------------------------------
# Existing scanner workflow is unaffected
# ---------------------------------------------------------------------

def test_manual_scan_workflow_has_no_dependency_on_strategy_sets():
    """The manual grid -> Run Scan path (ui.scan_view/ui.controls) must
    remain completely unaffected by Module 7B: it never imports
    strategy_sets or ui.strategy_set_view, so a user who never touches
    the Strategy Set panel gets byte-identical behavior to before this
    module existed (also verified end to end by the untouched, still-
    passing tests/test_ui_formatting.py, tests/test_ui_controls.py, and
    the run_scan() tests in tests/test_template_scanner_scanner.py)."""
    import ui.controls as controls
    import ui.scan_view as scan_view

    for module in (scan_view, controls):
        source = inspect.getsource(module)
        assert "strategy_set" not in source.lower()
