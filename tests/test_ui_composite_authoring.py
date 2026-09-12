"""
tests/test_ui_composite_authoring.py

The composite ("Group A x Group B") AUTHORING UI -- the last phase of
the composite feature, and the first time a composite Strategy Set can
be created without hand-editing JSON.

Two layers, the same split every other UI module in this project is
tested at:

  * ui.composite_formatting -- pure, Streamlit-free translation between
    widget values and strategy_sets.model's StrategyGroup/
    StrategyGroupPair. Tested directly against plain data.
  * ui.composite_view, through the REAL Streamlit script
    (streamlit.testing.v1.AppTest), same convention as tests/
    test_ui_strategy_set_selector_lifecycle.py, tests/
    test_ui_strategy_set_groups_preservation.py and tests/
    test_ui_composite_scan.py -- a real repository backed by tmp_path
    (never data/strategy_sets/, which is live user data), the real
    selector, the real Save button, the real scan.

What is deliberately NOT re-tested here: pairing, composition,
structural-zero filtering, expansion, and execution. Those are
strategy_sets.composite's and strategy_sets.execution's, already
covered by tests/test_strategy_sets_composite*.py, tests/
test_composite_execution.py and tests/test_ui_composite_scan.py. The
authoring UI's job is to produce a CONFIGURATION and feed it into that
existing machinery -- these tests check exactly that boundary, plus the
two rules the UI itself is responsible for (no nested composites can be
selected, and a saved selection is never silently rewritten).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
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

from ui.composite_formatting import (
    GROUP_A,
    GROUP_B,
    GROUP_B_WITHOUT_A_ERROR,
    NO_SOURCE_LABEL,
    build_group,
    build_group_pair,
    composite_summary,
    eligible_source_names,
    group_summary,
    is_eligible_source,
    resolve_source_state,
    reuse_unchanged,
    selectable_entry_names,
    selection_options,
    source_options,
    stale_selection,
    stored_group,
)

_APP_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
_PRICE_DATES = pd.date_range("2026-02-02", periods=40, freq="B")


def _fly(market_key: str, interval=BarInterval.DAILY, price_field="Close") -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0),
        interval=interval, price_field=price_field,
    )


def _entry(name: str, market_key: str = "SOFR", enabled: bool = True) -> StrategySetEntry:
    return StrategySetEntry(name=name, definition=_fly(market_key), enabled=enabled)


def _basis_entry(name: str = "SOFR vs CORRA") -> IntermarketStrategySetEntry:
    return IntermarketStrategySetEntry(
        name=name,
        definition=IntermarketDefinition(
            legs=(LegSpec("SOFR", 0, 1.0), LegSpec("CORRA", 0, -1.0)),
            interval=BarInterval.DAILY,
        ),
    )


# ---------------------------------------------------------------------
# ui.composite_formatting -- pure
# ---------------------------------------------------------------------

def test_selectable_entry_names_lists_entries_then_intermarket_entries_in_set_order():
    strategy_set = StrategySet(
        name="Mixed",
        entries=(_entry("SR3 Fly"), _entry("SON Fly", "SONIA")),
        intermarket_entries=(_basis_entry(),),
    )
    assert selectable_entry_names(strategy_set) == ("SR3 Fly", "SON Fly", "SOFR vs CORRA")


def test_selectable_entry_names_includes_a_disabled_entry():
    # resolve_group_entries() deliberately ignores `enabled` too -- the
    # panel must not hide a strategy a scan would happily include.
    strategy_set = StrategySet(name="Set", entries=(_entry("On"), _entry("Off", enabled=False)))
    assert selectable_entry_names(strategy_set) == ("On", "Off")


def test_is_eligible_source_rejects_a_composite_set():
    composite = StrategySet(
        name="Combos",
        entries=(_entry("SR3 Fly"),),
        groups=StrategyGroupPair(group_a=StrategyGroup(source_set_name="STIR Flys")),
    )
    assert is_eligible_source(composite) is False


def test_is_eligible_source_rejects_a_set_with_nothing_selectable():
    groups_only = StrategySet(
        name="Empty",
        entries=(),
        groups=StrategyGroupPair(group_a=StrategyGroup(source_set_name="STIR Flys")),
    )
    assert is_eligible_source(groups_only) is False


def test_is_eligible_source_accepts_an_ordinary_set():
    assert is_eligible_source(StrategySet(name="Set", entries=(_entry("SR3 Fly"),))) is True


def test_eligible_source_names_excludes_composites_and_the_set_being_edited():
    sets = {
        "Alpha": StrategySet(name="Alpha", entries=(_entry("A"),)),
        "Beta": StrategySet(name="Beta", entries=(_entry("B"),)),
        "Combos": StrategySet(
            name="Combos",
            entries=(_entry("C"),),
            groups=StrategyGroupPair(group_a=StrategyGroup(source_set_name="Alpha")),
        ),
    }
    assert eligible_source_names(sets, exclude_name="Beta") == ["Alpha"]


def test_eligible_source_names_preserves_the_callers_order():
    sets = {
        "Zulu": StrategySet(name="Zulu", entries=(_entry("A"),)),
        "Alpha": StrategySet(name="Alpha", entries=(_entry("B"),)),
    }
    assert eligible_source_names(sets) == ["Zulu", "Alpha"]


def test_source_options_puts_the_no_source_sentinel_first():
    assert source_options(["Alpha", "Beta"], None) == [NO_SOURCE_LABEL, "Alpha", "Beta"]


def test_source_options_keeps_a_stored_source_that_is_no_longer_eligible():
    options = source_options(["Alpha"], "Deleted Set")
    assert options == [NO_SOURCE_LABEL, "Alpha", "Deleted Set"]


def test_source_options_does_not_duplicate_a_stored_source_that_is_eligible():
    assert source_options(["Alpha"], "Alpha") == [NO_SOURCE_LABEL, "Alpha"]


def test_resolve_source_state_with_no_source_is_resolved_and_empty():
    state = resolve_source_state(None, {})
    assert state.resolved is True
    assert state.available == ()
    assert state.problem is None


def test_resolve_source_state_reports_a_missing_source_without_guessing_its_entries():
    state = resolve_source_state("Gone", {})
    assert state.resolved is False
    assert state.available is None
    assert "Gone" in state.problem


def test_resolve_source_state_rejects_a_composite_source():
    composite = StrategySet(
        name="Combos",
        entries=(_entry("C"),),
        groups=StrategyGroupPair(group_a=StrategyGroup(source_set_name="Alpha")),
    )
    state = resolve_source_state("Combos", {"Combos": composite})
    assert state.resolved is False
    assert "composite" in state.problem.lower()


def test_resolve_source_state_rejects_the_set_being_edited_as_its_own_source():
    alpha = StrategySet(name="Alpha", entries=(_entry("A"),))
    state = resolve_source_state("Alpha", {"Alpha": alpha}, own_name="Alpha")
    assert state.resolved is False
    assert "itself" in state.problem


def test_resolve_source_state_resolves_an_ordinary_source():
    alpha = StrategySet(name="Alpha", entries=(_entry("A"), _entry("B")))
    state = resolve_source_state("Alpha", {"Alpha": alpha})
    assert state.resolved is True
    assert state.available == ("A", "B")


def test_selection_options_lists_available_names_in_source_order():
    assert selection_options(["A", "B", "C"], ["C"]) == ["A", "B", "C"]


def test_selection_options_appends_a_stale_selected_name_so_it_is_never_dropped():
    assert selection_options(["A", "B"], ["B", "Deleted"]) == ["A", "B", "Deleted"]


def test_stale_selection_reports_names_the_source_no_longer_offers_in_selection_order():
    assert stale_selection(["X", "A", "Y"], ["A"]) == ["X", "Y"]


def test_stale_selection_is_empty_when_everything_resolves():
    assert stale_selection(["A", "B"], ["B", "A"]) == []


def test_build_group_returns_none_without_a_source():
    assert build_group(None, ["A"]) is None


def test_build_group_preserves_selection_order_exactly():
    group = build_group("Alpha", ["C", "A", "B"])
    assert group.selected_entry_names == ("C", "A", "B")


def test_build_group_keeps_a_source_with_an_empty_selection():
    group = build_group("Alpha", [])
    assert group.source_set_name == "Alpha"
    assert group.selected_entry_names == ()


def test_build_group_pair_with_neither_group_is_not_a_composite():
    pair, error = build_group_pair(None, None)
    assert pair is None
    assert error is None


def test_build_group_pair_with_group_a_only_is_valid():
    pair, error = build_group_pair(build_group("Alpha", ["A"]), None)
    assert error is None
    assert pair.group_b is None
    assert pair.group_a.source_set_name == "Alpha"


def test_build_group_pair_rejects_group_b_without_group_a():
    pair, error = build_group_pair(None, build_group("Beta", ["B"]))
    assert pair is None
    assert error == GROUP_B_WITHOUT_A_ERROR


def test_build_group_pair_never_reorders_the_two_sides():
    # Group A stays Group A even when Group B would sort first.
    pair, _ = build_group_pair(build_group("Zulu", ["Z"]), build_group("Alpha", ["A"]))
    assert pair.group_a.source_set_name == "Zulu"
    assert pair.group_b.source_set_name == "Alpha"


def test_stored_group_reads_each_side_of_a_saved_pair():
    pair = StrategyGroupPair(
        group_a=StrategyGroup(source_set_name="Alpha", selected_entry_names=("A",)),
        group_b=StrategyGroup(source_set_name="Beta", selected_entry_names=("B",)),
    )
    assert stored_group(pair, GROUP_A).source_set_name == "Alpha"
    assert stored_group(pair, GROUP_B).source_set_name == "Beta"
    assert stored_group(None, GROUP_A) is None


def test_reuse_unchanged_keeps_the_stored_object_identity_when_nothing_changed():
    stored = StrategyGroupPair(
        group_a=StrategyGroup(source_set_name="Alpha", selected_entry_names=("A",))
    )
    authored = StrategyGroupPair(
        group_a=StrategyGroup(source_set_name="Alpha", selected_entry_names=("A",))
    )
    assert reuse_unchanged(authored, stored) is stored


def test_reuse_unchanged_returns_the_new_configuration_when_it_differs():
    stored = StrategyGroupPair(
        group_a=StrategyGroup(source_set_name="Alpha", selected_entry_names=("A",))
    )
    authored = StrategyGroupPair(
        group_a=StrategyGroup(source_set_name="Alpha", selected_entry_names=("A", "B"))
    )
    assert reuse_unchanged(authored, stored) is authored


def test_reuse_unchanged_propagates_an_explicit_clear():
    stored = StrategyGroupPair(group_a=StrategyGroup(source_set_name="Alpha"))
    assert reuse_unchanged(None, stored) is None


def test_group_summary_and_composite_summary_describe_the_current_state():
    assert "No source" in group_summary(None)
    assert "no strategies selected" in group_summary(build_group("Alpha", []))
    assert "2 strategies" in group_summary(build_group("Alpha", ["A", "B"]))
    assert "Not a composite" in composite_summary(None)
    assert "Group A only" in composite_summary(build_group_pair(build_group("A", ["x"]), None)[0])
    pair, _ = build_group_pair(build_group("A", ["x"]), build_group("B", ["y"]))
    assert "×" in composite_summary(pair)


# ---------------------------------------------------------------------
# AppTest -- the real panel
# ---------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
    directory = tmp_path / "strategy_sets"
    monkeypatch.setattr(config, "STRATEGY_SETS_DIR", str(directory))
    repository = StrategySetRepository(base_dir=str(directory))
    repository.save(
        StrategySet(
            name="STIR Flys",
            entries=(_entry("SR3 Fly", "SOFR"), _entry("SON Fly", "SONIA")),
        )
    )
    repository.save(
        StrategySet(
            name="Other Flys",
            entries=(_entry("CRA Fly", "CORRA"), _entry("FF Fly", "FED_FUNDS")),
        )
    )
    return repository


@pytest.fixture
def mock_leg_fetch(mocker):
    """Only used by the scan test below -- every other test here never
    touches market data."""

    def _batch(rics, interval, start, end):
        n = len(_PRICE_DATES)
        return {
            ric: pd.DataFrame(
                {
                    "Date": _PRICE_DATES,
                    "Open": [96.0 + (i % 7) * 0.01 for i in range(n)],
                    "High": [96.0 + (i % 7) * 0.01 for i in range(n)],
                    "Low": [96.0 + (i % 7) * 0.01 for i in range(n)],
                    "Close": [96.0 + (i % 7) * 0.01 + hash(ric) % 3 * 0.05 for i in range(n)],
                    "Volume": [1000.0] * n,
                }
            )
            for ric in rics
        }

    return mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch)


def _app() -> AppTest:
    return AppTest.from_file(_APP_PATH, default_timeout=90)


def _assert_no_exception(at: AppTest) -> None:
    assert not list(at.exception), [e.value for e in at.exception]


def _selector(at: AppTest):
    return [s for s in at.selectbox if s.label == "Strategy Set"][0]


def _source(at: AppTest, slot: str):
    return [s for s in at.selectbox if s.label == f"Group {slot} source Strategy Set"][0]


def _strategies(at: AppTest, slot: str):
    return [m for m in at.multiselect if m.label == f"Group {slot} strategies"][0]


def _key_button(at: AppTest, fragment: str):
    return [b for b in at.button if b.key and fragment in b.key][0]


def _select_set(at: AppTest, name: str) -> AppTest:
    return _selector(at).select(name).run()


def _save(at: AppTest) -> AppTest:
    return [b for b in at.button if b.label == "Save Strategy Set"][0].click().run()


def _save_as(at: AppTest, name: str) -> AppTest:
    at = _save(at)
    at = [t for t in at.text_input if t.label == "Strategy Set Name"][0].set_value(name).run()
    return [b for b in at.button if b.label == "Save"][0].click().run()


def _saved_groups(repo: StrategySetRepository, name: str) -> dict | None:
    return json.loads((Path(repo.base_dir) / f"{name}.json").read_text()).get("groups")


def _started(repo: StrategySetRepository) -> AppTest:
    at = _app()
    at.run()
    _assert_no_exception(at)
    return at


# --- source options ---------------------------------------------------

def test_panel_offers_every_ordinary_saved_set_as_a_group_source(repo):
    at = _started(repo)
    assert set(_source(at, GROUP_A).options) == {NO_SOURCE_LABEL, "STIR Flys", "Other Flys"}
    assert set(_source(at, GROUP_B).options) == {NO_SOURCE_LABEL, "STIR Flys", "Other Flys"}


def test_a_composite_strategy_set_is_never_offered_as_a_source(repo):
    # No nested composites: the UI-side half of resolve_group_entries()'
    # own rule -- a composite cannot even be picked.
    repo.save(
        StrategySet(
            name="Combos",
            entries=(),
            groups=StrategyGroupPair(
                group_a=StrategyGroup(source_set_name="STIR Flys", selected_entry_names=("SR3 Fly",)),
                group_b=StrategyGroup(source_set_name="Other Flys", selected_entry_names=("CRA Fly",)),
            ),
        )
    )
    at = _started(repo)
    assert "Combos" not in _source(at, GROUP_A).options


def test_the_set_being_edited_is_not_offered_as_its_own_source(repo):
    at = _started(repo)
    at = _select_set(at, "STIR Flys")
    _assert_no_exception(at)
    assert "STIR Flys" not in _source(at, GROUP_A).options
    assert "Other Flys" in _source(at, GROUP_A).options


# --- authoring and persistence ---------------------------------------

def test_authoring_both_groups_saves_the_selection_in_click_order(repo):
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _strategies(at, GROUP_A).select("SON Fly").run()
    at = _strategies(at, GROUP_A).select("SR3 Fly").run()
    at = _source(at, GROUP_B).select("Other Flys").run()
    at = _strategies(at, GROUP_B).select("FF Fly").run()
    at = _save_as(at, "Combos")
    _assert_no_exception(at)

    groups = _saved_groups(repo, "Combos")
    # Click order, NOT the source set's own order and never alphabetical.
    assert groups["group_a"] == {
        "source_set_name": "STIR Flys",
        "selected_entry_names": ["SON Fly", "SR3 Fly"],
    }
    assert groups["group_b"] == {
        "source_set_name": "Other Flys",
        "selected_entry_names": ["FF Fly"],
    }


def test_saved_composite_stores_only_the_configuration_never_the_combinations(repo):
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _key_button(at, "composite_a_select_all").click().run()
    at = _source(at, GROUP_B).select("Other Flys").run()
    at = _key_button(at, "composite_b_select_all").click().run()
    at = _save_as(at, "Combos")
    _assert_no_exception(at)

    raw = json.loads((Path(repo.base_dir) / "Combos.json").read_text())
    # 2 x 2 pairs exist at scan time; nothing about them is persisted.
    assert set(raw["groups"]) == {"group_a", "group_b"}
    assert set(raw["groups"]["group_a"]) == {"source_set_name", "selected_entry_names"}
    assert raw["entries"] == []


def test_select_all_selects_every_strategy_in_source_order(repo):
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _key_button(at, "composite_a_select_all").click().run()
    _assert_no_exception(at)
    assert _strategies(at, GROUP_A).value == ["SR3 Fly", "SON Fly"]


def test_clear_all_empties_the_selection(repo):
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _key_button(at, "composite_a_select_all").click().run()
    at = _key_button(at, "composite_a_clear_all").click().run()
    _assert_no_exception(at)
    assert _strategies(at, GROUP_A).value == []


def test_group_b_is_optional_and_saves_a_group_a_only_configuration(repo):
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _key_button(at, "composite_a_select_all").click().run()
    at = _save_as(at, "Just A")
    _assert_no_exception(at)

    groups = _saved_groups(repo, "Just A")
    assert groups["group_a"]["selected_entry_names"] == ["SR3 Fly", "SON Fly"]
    assert groups.get("group_b") is None


def test_group_b_without_group_a_is_rejected_and_nothing_is_authored(repo):
    at = _started(repo)
    at = _source(at, GROUP_B).select("Other Flys").run()
    _assert_no_exception(at)
    assert GROUP_B_WITHOUT_A_ERROR in [e.value for e in at.error]


def test_loading_a_composite_set_shows_its_saved_groups(repo):
    repo.save(
        StrategySet(
            name="Combos",
            entries=(),
            groups=StrategyGroupPair(
                group_a=StrategyGroup(
                    source_set_name="STIR Flys", selected_entry_names=("SON Fly",)
                ),
                group_b=StrategyGroup(
                    source_set_name="Other Flys", selected_entry_names=("FF Fly", "CRA Fly")
                ),
            ),
        )
    )
    at = _started(repo)
    at = _select_set(at, "Combos")
    _assert_no_exception(at)

    assert _source(at, GROUP_A).value == "STIR Flys"
    assert _strategies(at, GROUP_A).value == ["SON Fly"]
    assert _source(at, GROUP_B).value == "Other Flys"
    assert _strategies(at, GROUP_B).value == ["FF Fly", "CRA Fly"]


def test_editing_a_loaded_composites_selection_is_persisted(repo):
    repo.save(
        StrategySet(
            name="Combos",
            entries=(),
            groups=StrategyGroupPair(
                group_a=StrategyGroup(
                    source_set_name="STIR Flys", selected_entry_names=("SON Fly",)
                ),
                group_b=StrategyGroup(
                    source_set_name="Other Flys", selected_entry_names=("FF Fly",)
                ),
            ),
        )
    )
    at = _started(repo)
    at = _select_set(at, "Combos")
    at = _strategies(at, GROUP_A).select("SR3 Fly").run()
    at = _save(at)
    _assert_no_exception(at)

    assert _saved_groups(repo, "Combos")["group_a"]["selected_entry_names"] == [
        "SON Fly", "SR3 Fly"
    ]


def test_clearing_group_a_source_turns_a_composite_back_into_an_ordinary_set(repo):
    repo.save(
        StrategySet(
            name="Combos",
            entries=(_entry("SR3 Fly"),),
            groups=StrategyGroupPair(
                group_a=StrategyGroup(
                    source_set_name="STIR Flys", selected_entry_names=("SR3 Fly",)
                ),
                group_b=StrategyGroup(
                    source_set_name="Other Flys", selected_entry_names=("CRA Fly",)
                ),
            ),
        )
    )
    at = _started(repo)
    at = _select_set(at, "Combos")
    at = _source(at, GROUP_A).select(NO_SOURCE_LABEL).run()
    at = _source(at, GROUP_B).select(NO_SOURCE_LABEL).run()
    at = _save(at)
    _assert_no_exception(at)

    assert repo.load("Combos").groups is None
    assert "groups" not in json.loads((Path(repo.base_dir) / "Combos.json").read_text())


def test_switching_strategy_set_reseeds_the_panel(repo):
    repo.save(
        StrategySet(
            name="Combos",
            entries=(),
            groups=StrategyGroupPair(
                group_a=StrategyGroup(
                    source_set_name="STIR Flys", selected_entry_names=("SR3 Fly",)
                )
            ),
        )
    )
    at = _started(repo)
    at = _select_set(at, "Combos")
    assert _source(at, GROUP_A).value == "STIR Flys"

    at = _select_set(at, "Other Flys")
    _assert_no_exception(at)
    assert _source(at, GROUP_A).value == NO_SOURCE_LABEL


def test_switching_a_groups_source_starts_a_fresh_selection(repo):
    """A group's selection belongs to its source: changing the source
    must not carry the previous set's strategy names over -- they would
    resolve against nothing, and would be reported as a stale selection
    the trader never made."""
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _key_button(at, "composite_a_select_all").click().run()
    assert _strategies(at, GROUP_A).value == ["SR3 Fly", "SON Fly"]

    at = _source(at, GROUP_A).select("Other Flys").run()
    _assert_no_exception(at)
    assert _strategies(at, GROUP_A).value == []
    assert _strategies(at, GROUP_A).options == ["CRA Fly", "FF Fly"]
    assert not [w for w in at.warning if "no longer in" in w.value]


# --- preservation of what the panel cannot resolve --------------------

def test_a_missing_source_sets_selection_is_shown_read_only_and_saved_unchanged(repo):
    """The source Strategy Set no longer exists. Its selection cannot be
    validated, so it is displayed but never edited or rewritten -- a
    save must not quietly empty it."""
    repo.save(
        StrategySet(
            name="Combos",
            entries=(_entry("SR3 Fly"),),
            groups=StrategyGroupPair(
                group_a=StrategyGroup(
                    source_set_name="Deleted Set", selected_entry_names=("Gone Fly", "Also Gone")
                )
            ),
        )
    )
    at = _started(repo)
    at = _select_set(at, "Combos")
    _assert_no_exception(at)

    warnings = " ".join(w.value for w in at.warning)
    assert "Deleted Set" in warnings
    # No editable selection widget is offered for an unresolvable group.
    assert not [m for m in at.multiselect if m.label == f"Group {GROUP_A} strategies"]

    at = _save(at)
    _assert_no_exception(at)
    assert _saved_groups(repo, "Combos")["group_a"] == {
        "source_set_name": "Deleted Set",
        "selected_entry_names": ["Gone Fly", "Also Gone"],
    }


def test_a_stale_selected_strategy_is_flagged_and_kept_not_silently_dropped(repo):
    """The source set still exists but no longer contains one selected
    strategy. Dropping it would silently change a saved configuration
    AND would turn the scan's actionable "not found" error into a
    quietly smaller scan -- so it stays selected and stays offered."""
    repo.save(
        StrategySet(
            name="Combos",
            entries=(),
            groups=StrategyGroupPair(
                group_a=StrategyGroup(
                    source_set_name="STIR Flys", selected_entry_names=("SR3 Fly", "Deleted Fly")
                ),
                group_b=StrategyGroup(
                    source_set_name="Other Flys", selected_entry_names=("CRA Fly",)
                ),
            ),
        )
    )
    at = _started(repo)
    at = _select_set(at, "Combos")
    _assert_no_exception(at)

    assert "Deleted Fly" in " ".join(w.value for w in at.warning)
    assert _strategies(at, GROUP_A).value == ["SR3 Fly", "Deleted Fly"]

    at = _save(at)
    _assert_no_exception(at)
    assert _saved_groups(repo, "Combos")["group_a"]["selected_entry_names"] == [
        "SR3 Fly", "Deleted Fly"
    ]


# --- preview and execution -------------------------------------------

def test_preview_reports_how_many_combinations_the_selection_produces(repo):
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _key_button(at, "composite_a_select_all").click().run()
    at = _source(at, GROUP_B).select("Other Flys").run()
    at = _key_button(at, "composite_b_select_all").click().run()
    _assert_no_exception(at)

    # 2 x 2, none structurally zero (all four pairs are distinct).
    assert any("4 combinations" in s.value for s in at.success)


def test_preview_drops_a_structurally_zero_pair_via_the_existing_backend(repo):
    """"SR3 Fly - SR3 Fly" cancels completely; the count the panel shows
    is resolve_composite_combinations()' own, already-filtered one --
    the UI never re-implements that rule."""
    repo.save(
        StrategySet(name="Same Flys", entries=(_entry("SR3 Fly", "SOFR"),))
    )
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _strategies(at, GROUP_A).select("SR3 Fly").run()
    at = _source(at, GROUP_B).select("Same Flys").run()
    at = _strategies(at, GROUP_B).select("SR3 Fly").run()
    _assert_no_exception(at)

    assert any("No combinations" in i.value for i in at.info)


def test_run_scan_uses_the_panels_current_selection_without_saving_first(repo, mock_leg_fetch):
    """An unsaved composite scans exactly what the panel shows -- the
    same rule an unsaved grid row already follows. Nothing is written to
    the repository by the scan."""
    at = _started(repo)
    at = _source(at, GROUP_A).select("STIR Flys").run()
    at = _key_button(at, "composite_a_select_all").click().run()
    at = _source(at, GROUP_B).select("Other Flys").run()
    at = _key_button(at, "composite_b_select_all").click().run()
    at = [b for b in at.button if b.label == "▶ Run Scan"][0].click().run()
    _assert_no_exception(at)

    assert [e.value for e in at.error] == []
    grids = [df.value for df in at.dataframe if "Rank" in getattr(df.value, "columns", [])]
    assert grids and not grids[0].empty
    assert sorted(repo.list_names()) == ["Other Flys", "STIR Flys"]
