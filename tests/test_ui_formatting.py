"""
test_ui_formatting.py

Tests for Module 6A's pure UI helper logic (ui/formatting.py): ratio
parsing, template-row translation, filter/sort-key construction, and
display formatting. No Streamlit rendering is exercised here -- these
are plain functions operating on plain data, backed by the real,
unmodified strategy_engine/template_scanner objects.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import BarInterval

from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition

from range_analytics.multi_lookback import analyze_multi_lookback

from strategy_engine.pricing import StrategyHistory

from template_scanner.scan_results import ScanCandidateResult, results_to_dataframe

from ui.formatting import (
    ALL_FILTER_SPECS,
    FILTER_SPECS,
    NO_SECONDARY_RANK,
    STABILITY_FILTER_SPEC,
    build_definitions,
    build_filter_criteria,
    build_sort_keys,
    fmt_number,
    fmt_percent,
    parse_dense_weights,
    to_display_dataframe,
)


# ---------------------------------------------------------------------
# parse_dense_weights
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 | -2 | 1", [1.0, -2.0, 1.0]),
        ("1,-2,1", [1.0, -2.0, 1.0]),
        ("1 -2 1", [1.0, -2.0, 1.0]),
        ("2, -3, 0, 1", [2.0, -3.0, 0.0, 1.0]),
        (" 1  |  -1 ", [1.0, -1.0]),
    ],
)
def test_parse_dense_weights_accepts_common_separators(text, expected):
    assert parse_dense_weights(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "|||"])
def test_parse_dense_weights_rejects_empty(text):
    with pytest.raises(ValueError, match="empty"):
        parse_dense_weights(text)


def test_parse_dense_weights_rejects_non_numeric_token():
    with pytest.raises(ValueError, match="non-numeric"):
        parse_dense_weights("1 | abc | 1")


# ---------------------------------------------------------------------
# build_definitions
# ---------------------------------------------------------------------

def test_build_definitions_translates_valid_rows():
    results = build_definitions(["1 | -2 | 1", "2, -3, 0, 1"], "SOFR", BarInterval.DAILY)

    assert len(results) == 2
    assert all(r.error is None for r in results)

    fly = results[0].definition
    assert fly.offsets == (0, 1, 2)
    assert fly.weights == (1.0, -2.0, 1.0)

    # (2, -3, 0, 1) -- matches the CLAUDE.md-documented live-tested case.
    gapped = results[1].definition
    assert gapped.offsets == (0, 1, 3)
    assert gapped.weights == (2.0, -3.0, 1.0)


def test_build_definitions_skips_blank_rows():
    results = build_definitions(["1 | -2 | 1", "", "   ", None], "SOFR", BarInterval.DAILY)
    assert len(results) == 1
    assert results[0].definition is not None


def test_build_definitions_reports_invalid_rows_without_blocking_others():
    results = build_definitions(["1 | -2 | 1", "abc"], "SOFR", BarInterval.DAILY)

    assert len(results) == 2
    valid = [r for r in results if r.error is None]
    invalid = [r for r in results if r.error is not None]
    assert len(valid) == 1
    assert len(invalid) == 1
    assert invalid[0].ratio_text == "abc"
    assert invalid[0].definition is None


def test_build_definitions_reports_all_zero_ratio_as_error():
    results = build_definitions(["0 | 0"], "SOFR", BarInterval.DAILY)
    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].definition is None


# ---------------------------------------------------------------------
# build_filter_criteria
# ---------------------------------------------------------------------

def test_build_filter_criteria_empty_when_nothing_enabled():
    filter_state = {spec.key: {"enabled": False, "value": None} for spec in ALL_FILTER_SPECS}
    assert build_filter_criteria(filter_state, display_lookback=20) == []


def test_build_filter_criteria_only_includes_enabled_filters():
    filter_state = {spec.key: {"enabled": False, "value": None} for spec in ALL_FILTER_SPECS}
    filter_state["efficiency_ratio_max"] = {"enabled": True, "value": 0.5}
    filter_state["ar1_r_squared_min"] = {"enabled": True, "value": 0.2}

    criteria = build_filter_criteria(filter_state, display_lookback=20)

    assert len(criteria) == 2
    by_name = {c.name: c for c in criteria}
    assert by_name["Efficiency Ratio (max)"].max_value == 0.5
    assert by_name["Efficiency Ratio (max)"].min_value is None
    assert by_name["AR(1) R² (min)"].min_value == 0.2
    assert by_name["AR(1) R² (min)"].max_value is None


def test_build_filter_criteria_enabled_without_value_is_skipped():
    filter_state = {spec.key: {"enabled": False, "value": None} for spec in ALL_FILTER_SPECS}
    filter_state["half_life_max"] = {"enabled": True, "value": None}
    assert build_filter_criteria(filter_state, display_lookback=20) == []


def test_build_filter_criteria_includes_stability_filter_when_enabled():
    filter_state = {spec.key: {"enabled": False, "value": None} for spec in ALL_FILTER_SPECS}
    filter_state[STABILITY_FILTER_SPEC.key] = {"enabled": True, "value": 0.1}

    criteria = build_filter_criteria(filter_state, display_lookback=20)

    assert len(criteria) == 1
    assert criteria[0].name == STABILITY_FILTER_SPEC.label
    assert criteria[0].max_value == 0.1


def test_every_filter_spec_accessor_resolves_against_a_real_candidate(_scan_candidate):
    filter_state = {
        spec.key: {"enabled": True, "value": 10_000.0} for spec in FILTER_SPECS
    }
    criteria = build_filter_criteria(filter_state, display_lookback=20)
    assert len(criteria) == len(FILTER_SPECS)
    for criterion in criteria:
        # Should not raise -- proves the accessor resolves a real field/
        # derived metric via template_scanner's own canonical resolver.
        criterion.passes(_scan_candidate)


# ---------------------------------------------------------------------
# build_sort_keys
# ---------------------------------------------------------------------

def test_build_sort_keys_primary_only():
    keys = build_sort_keys("efficiency_ratio", True, NO_SECONDARY_RANK, True, display_lookback=20)
    assert len(keys) == 1
    assert keys[0].ascending is True


def test_build_sort_keys_with_secondary():
    keys = build_sort_keys(
        "efficiency_ratio", True, "ar1_beta", False, display_lookback=20
    )
    assert len(keys) == 2
    assert keys[0].ascending is True
    assert keys[1].ascending is False


def test_build_sort_keys_secondary_none_field_omitted():
    keys = build_sort_keys("efficiency_ratio", True, None, True, display_lookback=20)
    assert len(keys) == 1


# ---------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------

def test_fmt_number_renders_nan_as_dash():
    assert fmt_number(float("nan")) == "—"
    assert fmt_number(None) == "—"


def test_fmt_number_formats_float():
    assert fmt_number(1.23456, decimals=2) == "1.23"


def test_fmt_percent_renders_nan_as_dash():
    assert fmt_percent(float("nan")) == "—"


def test_fmt_percent_formats_fraction():
    assert fmt_percent(0.4567, decimals=1) == "45.7%"


def test_to_display_dataframe_empty_results():
    empty = results_to_dataframe([], display_lookback=20)
    display = to_display_dataframe(empty)
    assert display.empty
    assert list(display.columns)


def test_to_display_dataframe_preserves_row_order_and_formats_values(_scan_candidate):
    results_df = results_to_dataframe([_scan_candidate], display_lookback=20)
    display = to_display_dataframe(results_df)

    assert len(display) == 1
    assert display.iloc[0]["Strategy (RICs)"] == " / ".join(_scan_candidate.rics)
    assert display.iloc[0]["Weights"] == "1.00 / -2.00 / 1.00"
    # current_price is a real float for this fixture -- never the NaN dash.
    assert display.iloc[0]["Current"] != "—"


# ---------------------------------------------------------------------
# Fixture: one real, fully-computed ScanCandidateResult
# ---------------------------------------------------------------------

@pytest.fixture
def _scan_candidate() -> ScanCandidateResult:
    dates = pd.bdate_range("2024-01-01", periods=150)
    values = [100.0 + 0.01 * (i % 7) - 0.005 * (i % 5) for i in range(len(dates))]
    history_df = pd.DataFrame(
        {
            "Date": dates,
            "Leg_1": values,
            "Leg_2": values,
            "Leg_3": values,
            "Strategy": values,
        }
    )

    definition = StrategyDefinition(
        market_key="SOFR",
        offsets=(0, 1, 2),
        weights=(1.0, -2.0, 1.0),
        interval=BarInterval.DAILY,
        price_field="Close",
    )
    instance = StrategyInstance(definition=definition, rics=("SRAZ25", "SRAH26", "SRAM26"))
    history = StrategyHistory(instance=instance, history=history_df, price_field="Close")

    multi_lookback = analyze_multi_lookback(history, lookbacks=(20, 40, 60, 90, 120))

    return ScanCandidateResult(
        market_key=definition.market_key,
        rics=instance.rics,
        weights=definition.weights,
        offsets=definition.offsets,
        interval=definition.interval,
        price_field=history.price_field,
        instance=instance,
        multi_lookback=multi_lookback,
    )
