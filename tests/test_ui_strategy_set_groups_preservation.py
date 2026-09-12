"""
tests/test_ui_strategy_set_groups_preservation.py

Coverage for the composite-groups PRESERVATION slice: a Strategy Set
carrying `groups` (the Group A / Group B selection -- strategy_sets/
model.py) survives a save from the single-market-only Strategy
Templates grid instead of being silently dropped.

Exactly the same preservation-not-authoring story as tests/
test_ui_strategy_set_intermarket_visibility.py covers for
`intermarket_entries`, and structured the same way:
  * pure level -- ui.strategy_set_formatting.build_strategy_set_from_grid()
    against plain data.
  * round-trip level -- a REAL StrategySetRepository backed by tmp_path
    (never data/strategy_sets/, which is live user data).
  * AppTest level -- the real Streamlit script's own Save button.

Nothing here creates or edits a group through the UI -- there is
deliberately no such path yet (hand-editing the set's JSON remains the
only authoring route), and nothing here expands a Group A x Group B
combination, which is a later phase entirely.
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
    StrategyGroup,
    StrategyGroupPair,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository

from ui.formatting import INTERVAL_COLUMN, LABEL_COLUMN, MARKET_COLUMN, position_column
from ui.strategy_set_formatting import build_strategy_set_from_grid

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


def _intermarket_entry(name="SOFR vs CORRA") -> IntermarketStrategySetEntry:
    definition = IntermarketDefinition(
        legs=(
            LegSpec(market_key="SOFR", offset=0, weight=1.0),
            LegSpec(market_key="CORRA", offset=0, weight=-1.0),
        ),
        interval=BarInterval.DAILY,
    )
    return IntermarketStrategySetEntry(name=name, definition=definition)


def _groups() -> StrategyGroupPair:
    return StrategyGroupPair(
        group_a=StrategyGroup(
            source_set_name="STIR Flys", selected_entry_names=("SR3 Fly", "SON Fly")
        ),
        group_b=StrategyGroup(
            source_set_name="Other Flys", selected_entry_names=("SR3 Fly", "CRA Fly")
        ),
    )


def _grid_row(label, *weights, market_key="SOFR", interval=BarInterval.DAILY):
    row = {LABEL_COLUMN: label, MARKET_COLUMN: market_key, INTERVAL_COLUMN: interval.value}
    row.update({col: "" for col in _POS6})
    for i, w in enumerate(weights):
        row[_POS6[i]] = str(w)
    return row


def _raw_groups(repo: StrategySetRepository, name: str) -> str:
    """The `groups` object of the saved JSON file, as JSON text -- the
    exact thing that must not change across a load -> save -> reload
    (key order included, since it is re-dumped from the parsed file in
    the order the file itself carries)."""
    text = (Path(repo.base_dir) / f"{name}.json").read_text()
    return json.dumps(json.loads(text).get("groups"), indent=2)


# ---------------------------------------------------------------------
# build_strategy_set_from_grid -- preservation, not authoring
# ---------------------------------------------------------------------

def test_build_strategy_set_from_grid_defaults_to_no_groups():
    strategy_set = build_strategy_set_from_grid(
        "Set", [_grid_row("SOFR Fly", 1, -2, 1)], _POS6, "SOFR", BarInterval.DAILY
    )
    assert strategy_set.groups is None


def test_build_strategy_set_from_grid_explicit_none_groups_stays_none():
    strategy_set = build_strategy_set_from_grid(
        "Set", [_grid_row("SOFR Fly", 1, -2, 1)], _POS6, "SOFR", BarInterval.DAILY,
        groups=None,
    )
    assert strategy_set.groups is None


def test_build_strategy_set_from_grid_carries_groups_through_untouched():
    groups = _groups()
    strategy_set = build_strategy_set_from_grid(
        "Set", [_grid_row("SOFR Fly", 1, -2, 1)], _POS6, "SOFR", BarInterval.DAILY,
        groups=groups,
    )
    assert strategy_set.groups == groups


def test_build_strategy_set_from_grid_passes_the_same_groups_object_by_reference():
    # Preserved, never reconstructed: identity, not just equality, so a
    # future field added to StrategyGroupPair cannot be silently lost by
    # a partial rebuild here.
    groups = _groups()
    strategy_set = build_strategy_set_from_grid(
        "Set", [_grid_row("SOFR Fly", 1, -2, 1)], _POS6, "SOFR", BarInterval.DAILY,
        groups=groups,
    )
    assert strategy_set.groups is groups
    assert strategy_set.groups.group_a is groups.group_a
    assert strategy_set.groups.group_b is groups.group_b


def test_build_strategy_set_from_grid_preserves_groups_alongside_entries_and_intermarket():
    groups = _groups()
    preserved = (_intermarket_entry(),)
    strategy_set = build_strategy_set_from_grid(
        "Set",
        [_grid_row("SOFR Fly", 1, -2, 1), _grid_row("SONIA Spread", 1, -1, market_key="SONIA")],
        _POS6, "SOFR", BarInterval.DAILY,
        intermarket_entries=preserved,
        groups=groups,
    )
    assert [e.name for e in strategy_set.entries] == ["SOFR Fly", "SONIA Spread"]
    assert strategy_set.entries[1].definition.market_key == "SONIA"
    assert strategy_set.intermarket_entries == preserved
    assert strategy_set.intermarket_entries[0] is preserved[0]
    assert strategy_set.groups is groups


def test_build_strategy_set_from_grid_group_a_only_survives():
    groups = StrategyGroupPair(group_a=StrategyGroup(source_set_name="STIR Flys"))
    strategy_set = build_strategy_set_from_grid(
        "Set", [_grid_row("SOFR Fly", 1, -2, 1)], _POS6, "SOFR", BarInterval.DAILY,
        groups=groups,
    )
    assert strategy_set.groups is groups
    assert strategy_set.groups.group_b is None


def test_build_strategy_set_from_grid_allows_an_empty_grid_when_groups_survive():
    # A groups-only composite set has nothing the grid can show, so
    # saving it back must not be rejected as "empty".
    groups = _groups()
    strategy_set = build_strategy_set_from_grid(
        "Combos", [_grid_row("")], _POS6, "SOFR", BarInterval.DAILY, groups=groups,
    )
    assert strategy_set.entries == ()
    assert strategy_set.intermarket_entries == ()
    assert strategy_set.groups is groups


def test_build_strategy_set_from_grid_still_rejects_a_wholly_empty_set():
    with pytest.raises(ValueError, match="at least one strategy row"):
        build_strategy_set_from_grid(
            "Empty", [_grid_row("")], _POS6, "SOFR", BarInterval.DAILY,
        )


# ---------------------------------------------------------------------
# Repository round trip
# ---------------------------------------------------------------------

def _composite_set(name="Combos") -> StrategySet:
    return StrategySet(
        name=name,
        entries=(_entry("SOFR Fly"), _entry("SONIA Fly", market_key="SONIA")),
        intermarket_entries=(_intermarket_entry(),),
        groups=_groups(),
    )


def test_composite_set_survives_load_save_reload_with_groups_byte_identical(repo):
    original = _composite_set()
    repo.save(original)
    before = _raw_groups(repo, "Combos")

    loaded = repo.load("Combos")
    repo.save(
        build_strategy_set_from_grid(
            "Combos",
            [_grid_row("SOFR Fly", 1, -2, 1), _grid_row("SONIA Fly", 1, -2, 1, market_key="SONIA")],
            _POS6, "SOFR", BarInterval.DAILY,
            intermarket_entries=loaded.intermarket_entries,
            groups=loaded.groups,
        )
    )

    assert _raw_groups(repo, "Combos") == before
    reloaded = repo.load("Combos")
    assert reloaded.groups == original.groups
    assert reloaded.intermarket_entries == original.intermarket_entries
    assert {e.name: e.definition for e in reloaded.entries} == {
        e.name: e.definition for e in original.entries
    }


def test_saving_a_non_composite_set_still_writes_no_groups_key(repo):
    repo.save(
        build_strategy_set_from_grid(
            "Plain", [_grid_row("SOFR Fly", 1, -2, 1)], _POS6, "SOFR", BarInterval.DAILY
        )
    )
    raw = json.loads((Path(repo.base_dir) / "Plain.json").read_text())
    assert "groups" not in raw


# ---------------------------------------------------------------------
# AppTest -- the real Streamlit script's Save button
# ---------------------------------------------------------------------

def _app() -> AppTest:
    return AppTest.from_file(_APP_PATH, default_timeout=60)


def _selector(at: AppTest):
    return [s for s in at.selectbox if s.label == "Strategy Set"][0]


def test_save_button_preserves_a_composite_sets_groups(repo):
    original = _composite_set()
    repo.save(original)
    before = _raw_groups(repo, "Combos")

    at = _app()
    at.run()
    _selector(at).select("Combos").run()
    assert not list(at.exception)

    [b for b in at.button if b.label == "Save Strategy Set"][0].click().run()
    assert not list(at.exception)

    assert _raw_groups(repo, "Combos") == before
    reloaded = repo.load("Combos")
    assert reloaded.groups == original.groups
    assert reloaded.intermarket_entries == original.intermarket_entries
    assert {e.name: e.definition for e in reloaded.entries} == {
        e.name: e.definition for e in original.entries
    }


def test_save_button_leaves_a_non_composite_set_without_groups(repo):
    repo.save(StrategySet(name="6M Strategies", entries=(_entry("SOFR Fly"),)))

    at = _app()
    at.run()
    _selector(at).select("6M Strategies").run()
    assert not list(at.exception)

    [b for b in at.button if b.label == "Save Strategy Set"][0].click().run()
    assert not list(at.exception)

    assert repo.load("6M Strategies").groups is None
    raw = json.loads((Path(repo.base_dir) / "6M Strategies.json").read_text())
    assert "groups" not in raw
