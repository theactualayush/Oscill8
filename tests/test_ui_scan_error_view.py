"""
tests/test_ui_scan_error_view.py

Integration coverage (real Streamlit script via streamlit.testing.v1.
AppTest, same rationale as tests/test_ui_strategy_set_selector_lifecycle.
py) for the friendly scan-error presentation: a failed Run Scan must
show a short, trader-facing headline/message as the PRIMARY error --
never the raw exception type, message, or traceback -- with the full
technical detail confined to a collapsed "Technical details" expander.

Does not touch run_scan()/the scanner itself -- template_scanner.
scanner.run_scan is mocked to raise, exactly as the existing
"test_run_scan_uses_the_loaded_sets_exact_weights..." style tests mock
it to return a canned ScanReport. Only ui.scan_view's exception handling
and ui.error_formatting's classification are under test here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import config
from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository
import ui.scan_view as scan_view

_APP_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
    directory = tmp_path / "strategy_sets"
    monkeypatch.setattr(config, "STRATEGY_SETS_DIR", str(directory))
    return StrategySetRepository(base_dir=str(directory))


def _entry(name="SOFR Fly", weights=(1.0, -2.0, 1.0)) -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=tuple(range(len(weights))), weights=weights, interval=BarInterval.DAILY,
    )
    return StrategySetEntry(name=name, definition=definition)


def _app() -> AppTest:
    return AppTest.from_file(_APP_PATH, default_timeout=60)


def _selector(at: AppTest):
    return [s for s in at.selectbox if s.label == "Strategy Set"][0]


def _button(at: AppTest, label: str):
    return [b for b in at.button if b.label == label][0]


def _assert_no_exception(at: AppTest) -> None:
    exceptions = list(at.exception)
    assert not exceptions, f"Unexpected exception(s): {[e.value for e in exceptions]}"


def _run_scan_and_fail(repo, mocker, exc: Exception) -> AppTest:
    repo.save(StrategySet(name="6M Strategies", entries=(_entry(),)))
    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()

    mocker.patch.object(scan_view, "run_scan", side_effect=exc)
    _button(at, "▶ Run Scan").click().run()
    _assert_no_exception(at)
    return at


def test_permission_error_shows_friendly_data_access_message(repo, mocker):
    at = _run_scan_and_fail(
        repo, mocker, RuntimeError("TS.Interday.UserNotPermission.70112: User does not have permission")
    )

    errors = " ".join(e.value for e in at.error)
    assert "Unable to fetch market data" in errors
    assert "not available with the current data access" in errors
    # The raw vendor error code/exception text must never appear in the
    # primary error -- only inside "Technical details".
    assert "70112" not in errors
    assert "UserNotPermission" not in errors


def test_connection_error_shows_friendly_connection_message(repo, mocker):
    at = _run_scan_and_fail(repo, mocker, ConnectionError("Session could not be established"))

    errors = " ".join(e.value for e in at.error)
    assert "Unable to connect to market data" in errors
    assert "Session could not be established" not in errors


def test_unclassified_error_shows_generic_message(repo, mocker):
    at = _run_scan_and_fail(repo, mocker, ValueError("something totally unrelated broke"))

    errors = " ".join(e.value for e in at.error)
    assert "The scan could not be completed" in errors
    assert "something totally unrelated broke" not in errors


def test_technical_details_expander_still_contains_the_full_exception(repo, mocker):
    at = _run_scan_and_fail(repo, mocker, RuntimeError("permission denied for this universe"))

    expander_labels = [getattr(exp, "label", None) for exp in at.expander]
    assert "Technical details" in expander_labels

    code_blocks = " ".join(c.value for c in at.code)
    assert "permission denied for this universe" in code_blocks
    assert "RuntimeError" in code_blocks


def test_no_traceback_or_exception_type_leaks_outside_the_expander(repo, mocker):
    at = _run_scan_and_fail(repo, mocker, RuntimeError("no successful response from the data provider"))

    errors = " ".join(e.value for e in at.error)
    assert "RuntimeError" not in errors
    assert "Traceback" not in errors
