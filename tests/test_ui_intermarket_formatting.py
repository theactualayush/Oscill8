"""
tests/test_ui_intermarket_formatting.py

Tests for ui/intermarket_formatting.py -- the pure translation from a
StrategySet's Module 9 `intermarket_entries` into the read-only display
rows the Strategy Set panel shows for them.

Plain functions over plain data and the real, unmodified
strategy_engine/strategy_sets objects (no mocks, no Streamlit
rendering), matching tests/test_ui_formatting.py and
tests/test_ui_strategy_set_formatting.py's own convention.

The three shapes named in the task are covered explicitly: a set with
ONLY intermarket entries, a MIXED set, and a set with NONE.
"""

from __future__ import annotations

import inspect

import pytest

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec

from strategy_sets.model import (
    IntermarketStrategySetEntry,
    StrategySet,
    StrategySetEntry,
)

from ui import intermarket_formatting as imf
from ui.intermarket_formatting import (
    LEG_COLUMN,
    LEG_COLUMNS,
    LEG_MARKET_COLUMN,
    LEG_OFFSET_COLUMN,
    LEG_WEIGHT_COLUMN,
    entry_display,
    entry_displays,
    entry_status_label,
    entry_summary_line,
    has_intermarket_entries,
    intermarket_notice,
    leg_display_rows,
    panel_title,
)


def _single_market_entry(name="SOFR Fly") -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0), interval=BarInterval.DAILY,
    )
    return StrategySetEntry(name=name, definition=definition)


def _intermarket_definition(
    legs=(("SOFR", 0, 1.0), ("CORRA", 0, -1.0)),
    interval=BarInterval.DAILY,
    price_field="Close",
    bp_per_point=None,
) -> IntermarketDefinition:
    return IntermarketDefinition(
        legs=tuple(LegSpec(market_key=m, offset=o, weight=w) for m, o, w in legs),
        interval=interval,
        price_field=price_field,
        bp_per_point=bp_per_point,
    )


def _intermarket_entry(name="SOFR vs CORRA", enabled=True, **kwargs) -> IntermarketStrategySetEntry:
    return IntermarketStrategySetEntry(
        name=name, definition=_intermarket_definition(**kwargs), enabled=enabled,
    )


# ---------------------------------------------------------------------
# Design principle: pure, no Streamlit
# ---------------------------------------------------------------------

def test_module_imports_no_streamlit():
    """The same convention ui.formatting/ui.strategy_set_formatting
    already follow (Module 6A): this layer is plain-data translation and
    must stay unit-testable without a Streamlit script context."""
    source = inspect.getsource(imf)
    assert "import streamlit" not in source
    assert "from streamlit" not in source


def test_module_exposes_no_way_to_build_or_edit_an_intermarket_entry():
    """Visibility, not editability: there is deliberately no inverse of
    these functions anywhere in ui/ -- hand-editing the Strategy Set's
    JSON remains the only authoring route."""
    exported = set(imf.__all__)
    for forbidden in ("entry_from_display", "build_intermarket_entry", "display_to_entry"):
        assert forbidden not in exported
    assert not any(name.startswith("build_") for name in exported)


# ---------------------------------------------------------------------
# leg_display_rows
# ---------------------------------------------------------------------

def test_leg_display_rows_reports_each_legs_own_market_offset_and_weight():
    definition = _intermarket_definition(legs=(("SOFR", 0, 1.0), ("CORRA", 2, -1.5)))
    legs = leg_display_rows(definition)

    assert [leg.leg_number for leg in legs] == [1, 2]
    assert [leg.market_key for leg in legs] == ["SOFR", "CORRA"]
    assert [leg.offset for leg in legs] == [0, 2]
    assert [leg.weight for leg in legs] == ["1", "-1.5"]


def test_leg_display_rows_preserves_leg_order_and_repeated_offsets():
    # Two legs at offset 0 in different markets is the ordinary
    # intermarket-spread case -- never deduplicated or re-sorted.
    definition = _intermarket_definition(
        legs=(("CORRA", 0, -1.0), ("SOFR", 0, 1.0), ("SONIA", 1, 1.0)),
    )
    legs = leg_display_rows(definition)
    assert [leg.market_key for leg in legs] == ["CORRA", "SOFR", "SONIA"]
    assert [leg.offset for leg in legs] == [0, 0, 1]


def test_leg_rows_are_plain_dicts_keyed_by_the_declared_leg_columns():
    display = entry_display(_intermarket_entry())
    rows = display.leg_rows

    assert [set(row) for row in rows] == [set(LEG_COLUMNS), set(LEG_COLUMNS)]
    assert rows[0] == {
        LEG_COLUMN: 1,
        LEG_MARKET_COLUMN: "SOFR",
        LEG_OFFSET_COLUMN: 0,
        LEG_WEIGHT_COLUMN: "1",
    }
    assert rows[1][LEG_MARKET_COLUMN] == "CORRA"
    assert rows[1][LEG_WEIGHT_COLUMN] == "-1"


# ---------------------------------------------------------------------
# entry_display
# ---------------------------------------------------------------------

def test_entry_display_carries_name_interval_and_price_field_verbatim():
    entry = _intermarket_entry(name="SOFR vs CORRA basis", interval=BarInterval.HOURLY, price_field="Open")
    display = entry_display(entry)

    assert display.name == "SOFR vs CORRA basis"
    assert display.interval == "HOURLY"
    assert display.price_field == "Open"
    assert display.enabled is True


def test_entry_display_market_label_is_the_composite_display_label():
    """Module 9's resolve_display_market_key() -- cosmetic only. The
    real, authoritative per-leg market keys are still reported per leg,
    which is what anything non-cosmetic would have to use."""
    display = entry_display(_intermarket_entry())
    assert display.market_label == "SOFR/CORRA"
    assert [leg.market_key for leg in display.legs] == ["SOFR", "CORRA"]


def test_entry_display_reports_an_unset_bp_per_point_as_none():
    assert entry_display(_intermarket_entry()).bp_per_point is None


def test_entry_display_reports_an_explicit_bp_per_point_override():
    assert entry_display(_intermarket_entry(bp_per_point=100.0)).bp_per_point == 100.0


def test_entry_display_preserves_a_disabled_entrys_flag():
    display = entry_display(_intermarket_entry(enabled=False))
    assert display.enabled is False
    assert entry_status_label(display) == "Disabled"


def test_entry_status_label_for_an_enabled_entry():
    assert entry_status_label(entry_display(_intermarket_entry())) == "Enabled"


# ---------------------------------------------------------------------
# entry_displays / has_intermarket_entries -- only / mixed / none
# ---------------------------------------------------------------------

def test_intermarket_only_set_yields_one_display_per_entry():
    strategy_set = StrategySet(
        name="Cross Market",
        entries=(),
        intermarket_entries=(
            _intermarket_entry(name="A"),
            _intermarket_entry(name="B", legs=(("SOFR", 0, 1.0), ("SONIA", 1, -1.0))),
        ),
    )
    displays = entry_displays(strategy_set)

    assert [d.name for d in displays] == ["A", "B"]
    assert displays[1].market_label == "SOFR/SONIA"
    assert has_intermarket_entries(strategy_set) is True


def test_mixed_set_yields_displays_for_the_intermarket_entries_only():
    strategy_set = StrategySet(
        name="Mixed Set",
        entries=(_single_market_entry("SOFR Fly"), _single_market_entry("SOFR Curve")),
        intermarket_entries=(_intermarket_entry(name="SOFR vs CORRA"),),
    )
    displays = entry_displays(strategy_set)

    assert [d.name for d in displays] == ["SOFR vs CORRA"]
    assert has_intermarket_entries(strategy_set) is True


def test_single_market_only_set_yields_nothing_at_all():
    """The ordinary case: no panel, no notice, nothing changes."""
    strategy_set = StrategySet(name="6M Strategies", entries=(_single_market_entry(),))

    assert entry_displays(strategy_set) == []
    assert has_intermarket_entries(strategy_set) is False
    assert intermarket_notice(strategy_set) is None


def test_no_loaded_set_at_all_yields_nothing():
    assert entry_displays(None) == []
    assert has_intermarket_entries(None) is False
    assert intermarket_notice(None) is None


def test_displays_are_in_saved_order_including_disabled_entries():
    """Unlike the grid (which omits disabled entries because re-saving
    it legitimately drops them), a read-only panel that hid them would
    recreate the exact invisibility problem this module exists to fix."""
    strategy_set = StrategySet(
        name="Cross Market",
        entries=(),
        intermarket_entries=(
            _intermarket_entry(name="First", enabled=True),
            _intermarket_entry(name="Second", enabled=False),
            _intermarket_entry(name="Third", enabled=True),
        ),
    )
    displays = entry_displays(strategy_set)
    assert [(d.name, d.enabled) for d in displays] == [
        ("First", True), ("Second", False), ("Third", True),
    ]


# ---------------------------------------------------------------------
# entry_summary_line / panel_title / intermarket_notice
# ---------------------------------------------------------------------

def test_entry_summary_line_reports_markets_leg_count_interval_and_price_field():
    display = entry_display(_intermarket_entry(interval=BarInterval.FOUR_HOUR))
    line = entry_summary_line(display)

    assert "SOFR/CORRA" in line
    assert "2 legs" in line
    assert "4H" in line or BarInterval.FOUR_HOUR.value in line
    assert "Close" in line


def test_entry_summary_line_says_bp_per_point_is_not_set_when_it_is_not():
    line = entry_summary_line(entry_display(_intermarket_entry()))
    assert "bp/point not set" in line


def test_entry_summary_line_reports_an_explicit_bp_per_point():
    line = entry_summary_line(entry_display(_intermarket_entry(bp_per_point=100.0)))
    assert "bp/point 100" in line


def test_entry_summary_line_uses_singular_leg_wording_for_one_leg():
    display = entry_display(_intermarket_entry(legs=(("SOFR", 0, 1.0),)))
    assert "1 leg" in entry_summary_line(display)
    assert "1 legs" not in entry_summary_line(display)


@pytest.mark.parametrize(
    "count, expected_fragment", [(1, "1 intermarket strategy"), (3, "3 intermarket strategies")]
)
def test_panel_title_counts_and_pluralizes(count, expected_fragment):
    displays = [entry_display(_intermarket_entry(name=f"Entry {i}")) for i in range(count)]
    title = panel_title(displays)
    assert expected_fragment in title
    assert "read-only" in title


def test_intermarket_notice_explains_why_they_are_not_in_the_grid():
    strategy_set = StrategySet(
        name="Mixed Set",
        entries=(_single_market_entry(),),
        intermarket_entries=(_intermarket_entry(name="X"), _intermarket_entry(name="Y")),
    )
    notice = intermarket_notice(strategy_set)

    assert notice is not None
    assert "2 intermarket strategies" in notice
    assert "read-only" in notice
    # It must explain BOTH why they're missing from the grid and that
    # saving does not lose them -- the whole point of the panel.
    assert "single-market" in notice
    assert "preserves" in notice


def test_intermarket_notice_is_singular_for_one_entry():
    strategy_set = StrategySet(
        name="Mixed Set",
        entries=(_single_market_entry(),),
        intermarket_entries=(_intermarket_entry(name="X"),),
    )
    notice = intermarket_notice(strategy_set)
    assert "1 intermarket strategy " in notice
