"""
tests/test_ui_strategy_set_multimarket_roundtrip.py

Regression tests for the multi-market Strategy Set fix: an earlier
version of the Strategy Templates grid bound EVERY row to one
scan-bar-selected market/interval, so loading a set whose entries span
different markets (e.g. "Intermarket Churning": SOFR + SONIA + CORRA +
Fed Funds) and saving it again would silently normalize every row to a
single market/interval -- corrupting the saved file. Fixed by giving
the grid its own per-row Market/Interval columns (see
ui.formatting.MARKET_COLUMN/INTERVAL_COLUMN, ui.controls'
column_config, and ui.strategy_set_formatting's module docstring for
the full rationale).

test_ui_strategy_set_formatting.py already covers the pure translation
functions in isolation (dense_row_from_definition/grid_rows_from_
strategy_set/build_strategy_set_from_grid). This file instead exercises
the FULL round trip through the real StrategySetRepository (isolated
tmp_path, never the real data/strategy_sets/) end to end -- create ->
save -> load -> verify -> modify one unrelated weight -> save -> reload
-> verify every entry's market/interval/name/weight/offset is exactly
what it was, for both a multi-market set (the case that was breaking)
and a single-market set (the common case, confirmed unaffected) -- plus
one AppTest-level check that the actual Streamlit Save button preserves
a multi-market set with zero edits.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from core import config
from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository

from ui.formatting import LABEL_COLUMN, position_column
from ui.strategy_set_formatting import build_strategy_set_from_grid, grid_rows_from_strategy_set

_POS6 = tuple(position_column(i) for i in range(1, 7))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
    directory = tmp_path / "strategy_sets"
    monkeypatch.setattr(config, "STRATEGY_SETS_DIR", str(directory))
    return StrategySetRepository(base_dir=str(directory))


def _entry(name, market_key, interval, weights=(1.0, -2.0, 1.0)) -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key=market_key, offsets=tuple(range(len(weights))), weights=weights, interval=interval,
    )
    return StrategySetEntry(name=name, definition=definition)


def _load_edit_save_reload(repo, name, grid_rows, edit_fn, scan_bar_market_key, scan_bar_interval):
    """Simulate: the grid already shows `grid_rows` (loaded from a
    saved set) -> the user edits it (`edit_fn`) -> clicks Save Strategy
    Set, with the scan bar currently showing scan_bar_market_key/
    scan_bar_interval -> the set is reloaded from disk."""
    edited_rows = edit_fn(grid_rows)
    updated = build_strategy_set_from_grid(name, edited_rows, _POS6, scan_bar_market_key, scan_bar_interval)
    repo.save(updated)
    return repo.load(name)


# ---------------------------------------------------------------------
# create -> save -> load -> verify -> modify -> save -> reload -> verify
# ---------------------------------------------------------------------

def test_multi_market_set_round_trips_without_normalization(repo):
    original = StrategySet(
        name="Intermarket Churning",
        entries=(
            _entry("SOFR Fly", "SOFR", BarInterval.DAILY),
            _entry("SONIA Fly", "SONIA", BarInterval.DAILY),
            _entry("CORRA Fly", "CORRA", BarInterval.HOURLY),
            _entry("ZQ Fly", "FED_FUNDS", BarInterval.DAILY),
        ),
    )
    repo.save(original)

    # create -> save -> load -> verify
    loaded = repo.load("Intermarket Churning")
    assert {e.name: (e.definition.market_key, e.definition.interval) for e in loaded.entries} == {
        "SOFR Fly": ("SOFR", BarInterval.DAILY),
        "SONIA Fly": ("SONIA", BarInterval.DAILY),
        "CORRA Fly": ("CORRA", BarInterval.HOURLY),
        "ZQ Fly": ("FED_FUNDS", BarInterval.DAILY),
    }

    grid_rows = grid_rows_from_strategy_set(loaded, _POS6)

    # Modify ONE unrelated weight (SOFR Fly's middle leg only) --
    # exactly what a user editing one row in the grid would do. The
    # scan bar is simulated showing a market/interval that matches NONE
    # of the set's entries, to prove it can never leak into a row that
    # already has its own.
    def _edit(rows):
        edited = []
        for row in rows:
            row = dict(row)
            if row[LABEL_COLUMN] == "SOFR Fly":
                row[_POS6[1]] = "-99"
            edited.append(row)
        return edited

    reloaded = _load_edit_save_reload(
        repo, "Intermarket Churning", grid_rows, _edit,
        scan_bar_market_key="CORRA", scan_bar_interval=BarInterval.FOUR_HOUR,
    )
    by_name = {e.name: e for e in reloaded.entries}

    # save -> reload -> verify: the edited entry changed only its weight.
    assert by_name["SOFR Fly"].definition.weights == (1.0, -99.0, 1.0)
    assert by_name["SOFR Fly"].definition.market_key == "SOFR"
    assert by_name["SOFR Fly"].definition.interval == BarInterval.DAILY
    assert by_name["SOFR Fly"].definition.offsets == (0, 1, 2)

    # Every OTHER entry: market, interval, weights, offsets, and name
    # all identical to the original -- no silent normalization.
    for name, market_key, interval in [
        ("SONIA Fly", "SONIA", BarInterval.DAILY),
        ("CORRA Fly", "CORRA", BarInterval.HOURLY),
        ("ZQ Fly", "FED_FUNDS", BarInterval.DAILY),
    ]:
        original_entry = next(e for e in original.entries if e.name == name)
        reloaded_entry = by_name[name]
        assert reloaded_entry.definition.market_key == market_key
        assert reloaded_entry.definition.interval == interval
        assert reloaded_entry.definition.weights == original_entry.definition.weights
        assert reloaded_entry.definition.offsets == original_entry.definition.offsets


def test_single_market_set_round_trips_unchanged(repo):
    # The common case -- confirmed unaffected by the multi-market fix.
    original = StrategySet(
        name="6M Strategies",
        entries=(
            _entry("SOFR Fly", "SOFR", BarInterval.DAILY, weights=(1.0, -2.0, 1.0)),
            _entry("SOFR Curve", "SOFR", BarInterval.DAILY, weights=(1.0, -1.0, -1.0, 1.0)),
        ),
    )
    repo.save(original)

    loaded = repo.load("6M Strategies")
    grid_rows = grid_rows_from_strategy_set(loaded, _POS6)

    reloaded = _load_edit_save_reload(
        repo, "6M Strategies", grid_rows, lambda rows: rows,  # no edits at all
        scan_bar_market_key="SOFR", scan_bar_interval=BarInterval.DAILY,
    )

    by_name = {e.name: e for e in reloaded.entries}
    for entry in original.entries:
        assert by_name[entry.name].definition == entry.definition


def test_scan_bar_selection_never_leaks_into_a_multi_market_sets_own_rows(repo):
    # Directly targets the original bug report: the scan bar's
    # currently-selected Market/Interval must never overwrite a row
    # that already carries its own -- even when the scan bar's
    # selection matches NEITHER entry in the set.
    original = StrategySet(
        name="Two Markets",
        entries=(_entry("A", "SOFR", BarInterval.DAILY), _entry("B", "SONIA", BarInterval.HOURLY)),
    )
    repo.save(original)
    grid_rows = grid_rows_from_strategy_set(repo.load("Two Markets"), _POS6)

    rebuilt = build_strategy_set_from_grid(
        "Two Markets", grid_rows, _POS6, market_key="CORRA", interval=BarInterval.FOUR_HOUR,
    )

    by_name = {e.name: e for e in rebuilt.entries}
    assert by_name["A"].definition.market_key == "SOFR"
    assert by_name["A"].definition.interval == BarInterval.DAILY
    assert by_name["B"].definition.market_key == "SONIA"
    assert by_name["B"].definition.interval == BarInterval.HOURLY


# ---------------------------------------------------------------------
# Same round trip, but through the actual Streamlit Save button
# ---------------------------------------------------------------------

def test_save_button_preserves_a_multi_market_set_with_zero_edits(repo):
    from pathlib import Path

    original = StrategySet(
        name="Intermarket Churning",
        entries=(
            _entry("SOFR Fly", "SOFR", BarInterval.DAILY),
            _entry("SONIA Fly", "SONIA", BarInterval.DAILY),
            _entry("CORRA Fly", "CORRA", BarInterval.HOURLY),
        ),
    )
    repo.save(original)

    app_path = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
    at = AppTest.from_file(app_path, default_timeout=60)
    at.run()

    selector = [s for s in at.selectbox if s.label == "Strategy Set"][0]
    selector.select("Intermarket Churning").run()
    assert not list(at.exception)

    save_button = [b for b in at.button if b.label == "Save Strategy Set"][0]
    save_button.click().run()
    assert not list(at.exception)

    reloaded = repo.load("Intermarket Churning")
    by_name = {e.name: e for e in reloaded.entries}
    for entry in original.entries:
        assert by_name[entry.name].definition == entry.definition
