"""
tests/test_ui_composite_scan.py

Phase 4 live-UI integration, exercised through the REAL Streamlit script
(streamlit.testing.v1.AppTest), same convention as tests/
test_ui_strategy_set_selector_lifecycle.py and tests/
test_ui_strategy_set_intermarket_visibility.py.

What Phase 4 changed at this layer: Run Scan no longer builds candidates
from the grid alone. It assembles ONE transient, never-saved StrategySet
from the grid's rows plus the loaded set's `intermarket_entries` (Module
9) and `groups` (composite Group A x Group B), and executes it through
strategy_sets.execution.run_strategy_set(). So all three kinds of
content now actually scan, and they scan through the same single
downstream path into template_scanner.scanner.run_scan_on_instances().

The only mocked boundary is the leg-batch fetch
(strategy_engine.pricing.get_history_batch) -- everything above it is
the real app: real repository, real expansion, real composition, real
structural-zero filtering, real analytics, real results grid.
"""

from __future__ import annotations

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

_APP_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
_PRICE_DATES = pd.date_range("2026-02-02", periods=40, freq="B")


@pytest.fixture(autouse=True)
def _mock_leg_fetch(mocker):
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


@pytest.fixture
def repo(tmp_path, monkeypatch) -> StrategySetRepository:
    directory = tmp_path / "strategy_sets"
    monkeypatch.setattr(config, "STRATEGY_SETS_DIR", str(directory))
    repository = StrategySetRepository(base_dir=str(directory))
    repository.save(
        StrategySet(
            name="STIR Flys",
            entries=(
                StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR")),
                StrategySetEntry(name="SON Fly", definition=_fly("SONIA")),
            ),
        )
    )
    repository.save(
        StrategySet(
            name="Other Flys",
            entries=(
                StrategySetEntry(name="SR3 Fly", definition=_fly("SOFR")),
                StrategySetEntry(name="CRA Fly", definition=_fly("CORRA")),
            ),
        )
    )
    return repository


def _fly(market_key: str, interval=BarInterval.DAILY, price_field="Close") -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0),
        interval=interval, price_field=price_field,
    )


def _basis_entry(name="SOFR vs CORRA") -> IntermarketStrategySetEntry:
    return IntermarketStrategySetEntry(
        name=name,
        definition=IntermarketDefinition(
            legs=(LegSpec("SOFR", 0, 1.0), LegSpec("CORRA", 0, -1.0)),
            interval=BarInterval.DAILY,
        ),
    )


def _groups(a=("SR3 Fly", "SON Fly"), b=("SR3 Fly", "CRA Fly"),
            a_source="STIR Flys", b_source="Other Flys") -> StrategyGroupPair:
    return StrategyGroupPair(
        group_a=StrategyGroup(source_set_name=a_source, selected_entry_names=a),
        group_b=StrategyGroup(source_set_name=b_source, selected_entry_names=b),
    )


def _app() -> AppTest:
    return AppTest.from_file(_APP_PATH, default_timeout=90)


def _selector(at: AppTest):
    return [s for s in at.selectbox if s.label == "Strategy Set"][0]


def _button(at: AppTest, label: str):
    return [b for b in at.button if b.label == label][0]


def _assert_no_exception(at: AppTest) -> None:
    assert not list(at.exception), [e.value for e in at.exception]


def _errors(at: AppTest) -> list[str]:
    return [e.value for e in at.error]


def _scan(at: AppTest) -> AppTest:
    return _button(at, "▶ Run Scan").click().run()


def _results_grid(at: AppTest):
    """The Range-Bound Opportunities table (the one carrying a Rank
    column), or None when no candidates were analyzed."""
    grids = [df.value for df in at.dataframe if "Rank" in getattr(df.value, "columns", [])]
    return grids[0] if grids else None


def _markets_shown(at: AppTest) -> set[str]:
    """Market keys represented in the current results -- surfaced as the
    Market filter's options (ui.formatting.available_markets), which is
    derived fresh from this scan's own results. `market_key` is not a
    column in the results grid itself (see ui.formatting.DISPLAY_COLUMNS)."""
    filters = [m for m in at.multiselect if m.label == "Market"]
    return set(filters[0].options) if filters else set()


def _labels_shown(at: AppTest) -> set[str]:
    """Strategy Label values in the results grid. That column is not in
    DEFAULT_VISIBLE_COLUMNS, so it is enabled first."""
    grid = _results_grid(at)
    if grid is None or "Strategy Label" not in grid.columns:
        return set()
    return set(grid["Strategy Label"].dropna())


def _show_label_column(at: AppTest) -> AppTest:
    """Enable the optional Strategy Label column in the Columns popover."""
    multiselect = [m for m in at.multiselect if m.label == "Visible columns"][0]
    return multiselect.set_value(list(multiselect.value) + ["Strategy Label"]).run()


def _select(at: AppTest, name: str) -> AppTest:
    return _selector(at).select(name).run()


# ---------------------------------------------------------------------
# 1-2. Existing paths still work
# ---------------------------------------------------------------------

def test_ordinary_strategy_set_still_scans(repo):
    at = _app()
    at.run()
    at = _select(at, "STIR Flys")
    at = _scan(at)

    _assert_no_exception(at)
    assert _errors(at) == []
    assert _results_grid(at) is not None


def test_manual_grid_workflow_is_unaffected_when_no_set_is_selected(repo):
    """The no-set-loaded path still reaches the scan unchanged.

    st.data_editor's canvas grid is not drivable via AppTest in this
    Streamlit version (see the same note in tests/
    test_ui_strategy_set_selector_lifecycle.py), so a manually TYPED row
    cannot be exercised here -- the observable contract for this path is
    that Run Scan reaches validation with a transient set built from the
    grid alone, carrying no intermarket entries and no groups. Typed-row
    equivalence is covered at the execution layer instead (see
    tests/test_composite_execution.py's ordinary-only tests)."""
    at = _app()
    at.run()
    assert _selector(at).value == "+ New Strategy Set"
    at = _scan(at)

    _assert_no_exception(at)
    assert any("at least one strategy row" in e for e in _errors(at))


def test_blank_grid_on_a_loaded_ordinary_set_scans_its_rows(repo):
    """Loading a saved set seeds the grid with real rows -- the
    documented way to get populated grid content under AppTest."""
    at = _app()
    at.run()
    at = _select(at, "Other Flys")
    at = _scan(at)

    _assert_no_exception(at)
    assert _errors(at) == []
    assert _results_grid(at) is not None


# ---------------------------------------------------------------------
# 3-4. The two paths Phase 4 unlocks
# ---------------------------------------------------------------------

def test_hand_authored_intermarket_entry_now_scans(repo):
    """Previously displayed read-only but never scannable."""
    repo.save(
        StrategySet(name="Basis Only", entries=(), intermarket_entries=(_basis_entry(),))
    )
    at = _app()
    at.run()
    at = _select(at, "Basis Only")
    at = _scan(at)

    _assert_no_exception(at)
    assert _errors(at) == []
    assert _results_grid(at) is not None
    assert _markets_shown(at) == {"SOFR/CORRA"}


def test_composite_strategy_set_now_scans(repo):
    repo.save(StrategySet(name="Combos", entries=(), groups=_groups()))
    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)

    _assert_no_exception(at)
    assert _errors(at) == []
    assert _results_grid(at) is not None


# ---------------------------------------------------------------------
# 5-6. Labels, orientation, structural zero
# ---------------------------------------------------------------------

def test_composite_labels_show_group_a_then_group_b_orientation(repo):
    repo.save(
        StrategySet(name="Combos", entries=(),
                    groups=_groups(a=("SR3 Fly",), b=("CRA Fly",)))
    )
    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)
    _assert_no_exception(at)
    at = _show_label_column(at)

    assert _labels_shown(at) == {"SR3 Fly - CRA Fly"}


def test_structural_zero_combinations_do_not_appear_in_results(repo):
    repo.save(StrategySet(name="Combos", entries=(), groups=_groups()))
    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)
    _assert_no_exception(at)
    at = _show_label_column(at)

    labels = _labels_shown(at)
    assert labels == {"SR3 Fly - CRA Fly", "SON Fly - SR3 Fly", "SON Fly - CRA Fly"}
    assert "SR3 Fly - SR3 Fly" not in labels


def test_all_zero_composite_shows_the_normal_no_candidates_behaviour(repo):
    repo.save(
        StrategySet(name="Combos", entries=(),
                    groups=_groups(a=("SR3 Fly",), b=("SR3 Fly",)))
    )
    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)

    _assert_no_exception(at)
    assert _errors(at) == []          # not an error state
    assert _results_grid(at) is None
    page = " ".join(m.value for m in at.markdown) + " ".join(i.value for i in at.info)
    assert "No candidates" in page


# ---------------------------------------------------------------------
# 7. Mixed set in one result set
# ---------------------------------------------------------------------

def test_mixed_ordinary_intermarket_and_composite_produce_one_result_set(repo):
    repo.save(
        StrategySet(
            name="Everything",
            entries=(StrategySetEntry(name="SOFR Fly", definition=_fly("SOFR")),),
            intermarket_entries=(_basis_entry(),),
            groups=_groups(a=("SR3 Fly",), b=("CRA Fly",)),
        )
    )
    at = _app()
    at.run()
    at = _select(at, "Everything")
    at = _scan(at)
    _assert_no_exception(at)
    at = _show_label_column(at)

    assert _results_grid(at) is not None
    markets = _markets_shown(at)
    assert "SOFR" in markets                                  # ordinary entry
    assert "SOFR/CORRA" in markets                            # hand-authored intermarket
    assert "SOFR/SOFR/SOFR/CORRA/CORRA/CORRA" in markets      # composite
    assert _labels_shown(at) == {"SOFR Fly", "SOFR vs CORRA", "SR3 Fly - CRA Fly"}


# ---------------------------------------------------------------------
# 8-11. CompositeResolutionError reaches the trader readably
# ---------------------------------------------------------------------

def test_missing_source_set_produces_a_readable_error(repo):
    repo.save(
        StrategySet(name="Combos", entries=(), groups=_groups(b_source="Gone Missing"))
    )
    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)

    _assert_no_exception(at)
    errors = " ".join(_errors(at))
    assert "Gone Missing" in errors and "Group B" in errors
    # Never the generic vendor/system headline.
    assert "could not be completed" not in errors


def test_missing_selected_strategy_produces_a_readable_error(repo):
    repo.save(
        StrategySet(name="Combos", entries=(), groups=_groups(a=("SR3 Fly", "Deleted Fly")))
    )
    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)

    _assert_no_exception(at)
    errors = " ".join(_errors(at))
    assert "Deleted Fly" in errors and "STIR Flys" in errors and "Group A" in errors
    assert "could not be completed" not in errors


def test_nested_composite_source_produces_a_readable_error(repo):
    repo.save(StrategySet(name="Inner Combo", entries=(), groups=_groups()))
    repo.save(
        StrategySet(name="Outer", entries=(),
                    groups=_groups(a=("SR3 Fly",), a_source="Inner Combo"))
    )
    at = _app()
    at.run()
    at = _select(at, "Outer")
    at = _scan(at)

    _assert_no_exception(at)
    errors = " ".join(_errors(at))
    assert "Inner Combo" in errors and "ested composite" in errors
    assert "could not be completed" not in errors


def test_incompatible_price_field_produces_a_readable_error(repo):
    repo.save(
        StrategySet(
            name="High Field",
            entries=(
                StrategySetEntry(
                    name="SR3 Fly High", definition=_fly("SOFR", price_field="High")
                ),
            ),
        )
    )
    repo.save(
        StrategySet(
            name="Combos", entries=(),
            groups=_groups(a=("SR3 Fly",), b=("SR3 Fly High",), b_source="High Field"),
        )
    )
    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)

    _assert_no_exception(at)
    errors = " ".join(_errors(at))
    assert "one price field" in errors
    assert "could not be completed" not in errors


# ---------------------------------------------------------------------
# Source files stay untouched through a UI scan
# ---------------------------------------------------------------------

def test_a_ui_scan_never_writes_to_a_source_strategy_set(repo):
    repo.save(StrategySet(name="Combos", entries=(), groups=_groups()))
    before = {
        name: (Path(repo.base_dir) / f"{name}.json").read_text()
        for name in repo.list_names()
    }

    at = _app()
    at.run()
    at = _select(at, "Combos")
    at = _scan(at)
    _assert_no_exception(at)

    assert sorted(repo.list_names()) == sorted(before)
    for name, text in before.items():
        assert (Path(repo.base_dir) / f"{name}.json").read_text() == text
