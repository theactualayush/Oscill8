"""
tests/test_ui_strategy_set_intermarket_visibility.py

Coverage for the Module 9 visibility slice: a Strategy Set carrying
`intermarket_entries` (legs spanning different markets) is now SHOWN,
read-only, in the Strategy Set panel, and a save from the
single-market-only grid preserves those entries instead of silently
dropping them.

Two layers, matching this repository's existing Strategy Set test
convention:
  * pure/round-trip level -- ui.strategy_set_formatting +
    ui.intermarket_formatting against a REAL StrategySetRepository
    backed by tmp_path (never data/strategy_sets/, which is live user
    data -- see tests/test_ui_strategy_set_multimarket_roundtrip.py's
    own fixture, copied here for the same reason).
  * AppTest level -- the real Streamlit script, verifying the read-only
    panel appears for a mixed set, does NOT appear for a single-market
    set (the ordinary case must look exactly as it did before), and
    that the real Save button preserves the intermarket entries.

Nothing here creates or edits an intermarket entry through the UI --
there is deliberately no such path (hand-editing the set's JSON remains
the only authoring route).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import config
from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec

from strategy_sets.model import (
    IntermarketStrategySetEntry,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository

from ui.formatting import INTERVAL_COLUMN, LABEL_COLUMN, MARKET_COLUMN, position_column
from ui.intermarket_formatting import LEG_COLUMN, LEG_MARKET_COLUMN, LEG_OFFSET_COLUMN, LEG_WEIGHT_COLUMN
from ui.strategy_set_formatting import build_strategy_set_from_grid, grid_rows_from_strategy_set

_POS6 = tuple(position_column(i) for i in range(1, 7))
_APP_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
    directory = tmp_path / "strategy_sets"
    monkeypatch.setattr(config, "STRATEGY_SETS_DIR", str(directory))
    return StrategySetRepository(base_dir=str(directory))


def _entry(name, market_key="SOFR", interval=BarInterval.DAILY, weights=(1.0, -2.0, 1.0)) -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key=market_key, offsets=tuple(range(len(weights))), weights=weights, interval=interval,
    )
    return StrategySetEntry(name=name, definition=definition)


def _intermarket_entry(
    name="SOFR vs CORRA",
    legs=(("SOFR", 0, 1.0), ("CORRA", 0, -1.0)),
    interval=BarInterval.DAILY,
    bp_per_point=None,
    enabled=True,
) -> IntermarketStrategySetEntry:
    definition = IntermarketDefinition(
        legs=tuple(LegSpec(market_key=m, offset=o, weight=w) for m, o, w in legs),
        interval=interval,
        bp_per_point=bp_per_point,
    )
    return IntermarketStrategySetEntry(name=name, definition=definition, enabled=enabled)


def _mixed_set(name="Mixed Set") -> StrategySet:
    return StrategySet(
        name=name,
        entries=(_entry("SOFR Fly"), _entry("SONIA Fly", market_key="SONIA")),
        intermarket_entries=(
            _intermarket_entry(name="SOFR vs CORRA"),
            _intermarket_entry(
                name="SOFR vs SONIA fwd",
                legs=(("SOFR", 0, 1.0), ("SONIA", 1, -1.0)),
                bp_per_point=100.0,
            ),
        ),
    )


def _raw_intermarket_entries(repo: StrategySetRepository, name: str) -> str:
    """The intermarket entries of the saved JSON file, as JSON text --
    the exact thing the acceptance criterion says must not change across
    a load -> save -> reload (key order included, since it is re-dumped
    from the parsed file in the order the file itself carries)."""
    text = (Path(repo.base_dir) / f"{name}.json").read_text()
    raw_entries = json.loads(text)["entries"]
    return json.dumps([e for e in raw_entries if "legs" in e], indent=2)


# ---------------------------------------------------------------------
# build_strategy_set_from_grid -- preservation, not authoring
# ---------------------------------------------------------------------

def _grid_row(label, *weights, market_key="SOFR", interval=BarInterval.DAILY):
    row = {LABEL_COLUMN: label, MARKET_COLUMN: market_key, INTERVAL_COLUMN: interval.value}
    row.update({col: "" for col in _POS6})
    for i, w in enumerate(weights):
        row[_POS6[i]] = str(w)
    return row


def test_build_strategy_set_from_grid_defaults_to_no_intermarket_entries():
    """Every pre-existing caller is unaffected -- a set built purely
    from grid rows has none."""
    strategy_set = build_strategy_set_from_grid(
        "Plain Set", [_grid_row("A", 1, -1)], _POS6, "SOFR", BarInterval.DAILY
    )
    assert strategy_set.intermarket_entries == ()


def test_build_strategy_set_from_grid_carries_intermarket_entries_through_untouched():
    preserved = (_intermarket_entry(name="SOFR vs CORRA"),)
    strategy_set = build_strategy_set_from_grid(
        "Mixed Set", [_grid_row("A", 1, -1)], _POS6, "SOFR", BarInterval.DAILY,
        intermarket_entries=preserved,
    )
    assert strategy_set.intermarket_entries == preserved
    # By reference -- ui/ never rebuilds, normalizes or re-validates one.
    assert strategy_set.intermarket_entries[0] is preserved[0]
    assert [e.name for e in strategy_set.entries] == ["A"]


def test_build_strategy_set_from_grid_allows_an_empty_grid_when_intermarket_entries_survive():
    """An intermarket-ONLY set loads a blank grid (it has no
    single-market rows) -- saving it must not be rejected as "empty",
    and must not wipe the entries that do exist."""
    preserved = (_intermarket_entry(name="SOFR vs CORRA"),)
    strategy_set = build_strategy_set_from_grid(
        "Cross Market", [_grid_row("")], _POS6, "SOFR", BarInterval.DAILY,
        intermarket_entries=preserved,
    )
    assert strategy_set.entries == ()
    assert strategy_set.intermarket_entries == preserved


def test_build_strategy_set_from_grid_still_rejects_a_wholly_empty_set():
    with pytest.raises(ValueError):
        build_strategy_set_from_grid("Empty Set", [_grid_row("")], _POS6, "SOFR", BarInterval.DAILY)


def test_build_strategy_set_from_grid_rejects_a_grid_label_colliding_with_an_intermarket_entry():
    """StrategySet's own one-shared-namespace uniqueness rule, surfaced
    unchanged -- never silently resolved by dropping one of them."""
    with pytest.raises(ValueError):
        build_strategy_set_from_grid(
            "Mixed Set", [_grid_row("SOFR vs CORRA", 1, -1)], _POS6, "SOFR", BarInterval.DAILY,
            intermarket_entries=(_intermarket_entry(name="SOFR vs CORRA"),),
        )


# ---------------------------------------------------------------------
# load -> save -> reload round trip through the real repository
# ---------------------------------------------------------------------

def test_mixed_set_survives_load_save_reload_with_intermarket_entries_byte_identical(repo):
    original = _mixed_set()
    repo.save(original)
    before = _raw_intermarket_entries(repo, "Mixed Set")

    loaded = repo.load("Mixed Set")
    grid_rows = grid_rows_from_strategy_set(loaded, _POS6)  # what the grid would show
    rebuilt = build_strategy_set_from_grid(
        "Mixed Set", grid_rows, _POS6, "SOFR", BarInterval.DAILY,
        intermarket_entries=loaded.intermarket_entries,
    )
    repo.save(rebuilt)

    assert _raw_intermarket_entries(repo, "Mixed Set") == before

    reloaded = repo.load("Mixed Set")
    assert reloaded.intermarket_entries == original.intermarket_entries
    # ...and the single-market half is unaffected by any of this.
    assert {e.name: e.definition for e in reloaded.entries} == {
        e.name: e.definition for e in original.entries
    }


def test_editing_a_single_market_row_does_not_disturb_the_intermarket_entries(repo):
    original = _mixed_set()
    repo.save(original)
    before = _raw_intermarket_entries(repo, "Mixed Set")

    loaded = repo.load("Mixed Set")
    rows = []
    for row in grid_rows_from_strategy_set(loaded, _POS6):
        row = dict(row)
        if row[LABEL_COLUMN] == "SOFR Fly":
            row[_POS6[1]] = "-99"
        rows.append(row)

    repo.save(
        build_strategy_set_from_grid(
            "Mixed Set", rows, _POS6, "SOFR", BarInterval.DAILY,
            intermarket_entries=loaded.intermarket_entries,
        )
    )

    reloaded = repo.load("Mixed Set")
    assert _raw_intermarket_entries(repo, "Mixed Set") == before
    assert {e.name: e for e in reloaded.entries}["SOFR Fly"].definition.weights == (1.0, -99.0, 1.0)


def test_intermarket_only_set_loads_a_blank_grid_and_still_round_trips(repo):
    original = StrategySet(
        name="Cross Market", entries=(), intermarket_entries=(_intermarket_entry(name="SOFR vs CORRA"),),
    )
    repo.save(original)
    before = _raw_intermarket_entries(repo, "Cross Market")

    loaded = repo.load("Cross Market")
    assert grid_rows_from_strategy_set(loaded, _POS6) == []  # nothing the grid can show

    repo.save(
        build_strategy_set_from_grid(
            "Cross Market", [_grid_row("")], _POS6, "SOFR", BarInterval.DAILY,
            intermarket_entries=loaded.intermarket_entries,
        )
    )
    assert _raw_intermarket_entries(repo, "Cross Market") == before
    assert repo.load("Cross Market").intermarket_entries == original.intermarket_entries


# ---------------------------------------------------------------------
# AppTest -- the real Streamlit script
# ---------------------------------------------------------------------

def _app() -> AppTest:
    return AppTest.from_file(_APP_PATH, default_timeout=60)


def _selector(at: AppTest):
    return [s for s in at.selectbox if s.label == "Strategy Set"][0]


def _page_text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown) + " " + " ".join(c.value for c in at.caption)


def _leg_tables(at: AppTest):
    return [df.value for df in at.dataframe if LEG_COLUMN in df.value.columns]


def test_single_market_only_set_renders_no_intermarket_panel(repo):
    """The ordinary case is untouched: no panel, no leg table, no
    read-only caption anywhere."""
    repo.save(StrategySet(name="6M Strategies", entries=(_entry("SOFR Fly"),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()
    assert not list(at.exception)

    assert _leg_tables(at) == []
    assert "INTERMARKET STRATEGIES" not in _page_text(at)


def test_mixed_set_shows_its_intermarket_entries_read_only(repo):
    repo.save(_mixed_set())

    at = _app()
    at.run()
    _selector(at).select("Mixed Set").run()
    assert not list(at.exception)

    text = _page_text(at)
    assert "INTERMARKET STRATEGIES (READ-ONLY)" in text
    assert "SOFR vs CORRA" in text
    assert "SOFR vs SONIA fwd" in text

    tables = _leg_tables(at)
    assert len(tables) == 2

    first = tables[0]
    assert list(first[LEG_MARKET_COLUMN]) == ["SOFR", "CORRA"]
    assert list(first[LEG_OFFSET_COLUMN]) == [0, 0]
    assert list(first[LEG_WEIGHT_COLUMN]) == ["1", "-1"]

    second = tables[1]
    assert list(second[LEG_MARKET_COLUMN]) == ["SOFR", "SONIA"]
    assert list(second[LEG_OFFSET_COLUMN]) == [0, 1]


def test_mixed_set_still_shows_its_single_market_entries_in_the_editable_grid(repo):
    repo.save(_mixed_set())

    at = _app()
    at.run()
    _selector(at).select("Mixed Set").run()
    assert not list(at.exception)

    grid = [df.value for df in at.dataframe if LABEL_COLUMN in df.value.columns][0]
    assert sorted(grid[LABEL_COLUMN]) == ["SOFR Fly", "SONIA Fly"]
    # The intermarket entries are NOT smuggled into the editable grid.
    assert "SOFR vs CORRA" not in list(grid[LABEL_COLUMN])


def test_save_button_preserves_a_mixed_sets_intermarket_entries(repo):
    original = _mixed_set()
    repo.save(original)
    before = _raw_intermarket_entries(repo, "Mixed Set")

    at = _app()
    at.run()
    _selector(at).select("Mixed Set").run()
    assert not list(at.exception)

    [b for b in at.button if b.label == "Save Strategy Set"][0].click().run()
    assert not list(at.exception)

    assert _raw_intermarket_entries(repo, "Mixed Set") == before
    reloaded = repo.load("Mixed Set")
    assert reloaded.intermarket_entries == original.intermarket_entries
    assert {e.name: e.definition for e in reloaded.entries} == {
        e.name: e.definition for e in original.entries
    }
