"""
test_ui_formatting.py

Tests for Module 6A's pure UI helper logic (ui/formatting.py):
strategy-grid-row translation, filter/sort-key construction, and result/
selection display formatting. No Streamlit rendering is exercised here
-- these are plain functions operating on plain data, backed by the
real, unmodified strategy_engine/template_scanner objects.
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
    DEFAULT_VISIBLE_COLUMNS,
    DISPLAY_COLUMNS,
    FILTER_SPECS,
    NO_SECONDARY_RANK,
    OPTIONAL_COLUMN_LABELS,
    RANK_COLUMN,
    RANK_METRIC_OPTIONS,
    RESULT_COLUMN_HELP,
    STABILITY_FILTER_SPEC,
    STRATEGY_LABEL_COLUMN,
    add_rank_column,
    apply_column_selection,
    apply_interval_override,
    available_markets,
    build_definitions_from_grid,
    build_filter_criteria,
    build_sort_keys,
    fmt_label,
    fmt_number,
    fmt_percent,
    format_percentile,
    format_percentile_range,
    format_ranked_by,
    position_column,
    selected_strategy_summary,
    to_display_dataframe,
)


# ---------------------------------------------------------------------
# position_column
# ---------------------------------------------------------------------

def test_position_column_naming():
    assert position_column(1) == "Curve Position 1"
    assert position_column(8) == "Curve Position 8"


# ---------------------------------------------------------------------
# build_definitions_from_grid
# ---------------------------------------------------------------------

_POS3 = tuple(position_column(i) for i in (1, 2, 3))
_POS4 = tuple(position_column(i) for i in (1, 2, 3, 4))


def test_build_definitions_from_grid_translates_valid_rows():
    # Grid cells arrive as TextColumn strings, not numbers -- see
    # ui.controls' column_config (verified empirically that a numeric
    # NumberColumn cell renders the literal text "None" when blank in
    # this Streamlit build, regardless of dtype).
    rows = [
        {"Label": "Fly", _POS3[0]: "1", _POS3[1]: "-2", _POS3[2]: "1"},
    ]
    results = build_definitions_from_grid(rows, _POS3, "SOFR", BarInterval.DAILY)

    assert len(results) == 1
    assert results[0].error is None
    fly = results[0].definition
    assert fly.offsets == (0, 1, 2)
    assert fly.weights == (1.0, -2.0, 1.0)


def test_build_definitions_from_grid_handles_gapped_ratio():
    # (2, -3, 0, 1) -- matches the CLAUDE.md-documented live-tested case.
    rows = [{"Label": "Gapped", _POS4[0]: "2", _POS4[1]: "-3", _POS4[2]: "0", _POS4[3]: "1"}]
    results = build_definitions_from_grid(rows, _POS4, "SOFR", BarInterval.DAILY)

    assert len(results) == 1
    gapped = results[0].definition
    assert gapped.offsets == (0, 1, 3)
    assert gapped.weights == (2.0, -3.0, 1.0)


def test_build_definitions_from_grid_multiple_rows():
    rows = [
        {"Label": "Fly", _POS3[0]: "1", _POS3[1]: "-2", _POS3[2]: "1"},
        {"Label": "Spread", _POS3[0]: "1", _POS3[1]: "-1", _POS3[2]: "0"},
    ]
    results = build_definitions_from_grid(rows, _POS3, "SOFR", BarInterval.DAILY)
    assert len(results) == 2
    assert [r.label for r in results] == ["Fly", "Spread"]


def test_build_definitions_from_grid_treats_blank_text_cell_as_zero():
    # An empty string is how an unpopulated TextColumn cell arrives --
    # equivalent to an explicit 0 (skip this position), not an error.
    rows = [{"Label": "Spread", _POS3[0]: "1", _POS3[1]: "-1", _POS3[2]: ""}]
    results = build_definitions_from_grid(rows, _POS3, "SOFR", BarInterval.DAILY)
    assert len(results) == 1
    assert results[0].definition.offsets == (0, 1)
    assert results[0].definition.weights == (1.0, -1.0)


def test_build_definitions_from_grid_treats_incomplete_number_as_zero():
    # A lone "-" or "." is a valid intermediate typing state under the
    # grid's numeric-pattern validator but not a complete number --
    # treated as blank/0 rather than raised as an error.
    rows = [{"Label": "Spread", _POS3[0]: "1", _POS3[1]: "-1", _POS3[2]: "-"}]
    results = build_definitions_from_grid(rows, _POS3, "SOFR", BarInterval.DAILY)
    assert len(results) == 1
    assert results[0].definition.offsets == (0, 1)
    assert results[0].definition.weights == (1.0, -1.0)


def test_build_definitions_from_grid_skips_all_zero_rows():
    rows = [
        {"Label": "Fly", _POS3[0]: 1, _POS3[1]: -2, _POS3[2]: 1},
        {"Label": "Blank", _POS3[0]: 0, _POS3[1]: 0, _POS3[2]: 0},
    ]
    results = build_definitions_from_grid(rows, _POS3, "SOFR", BarInterval.DAILY)
    assert len(results) == 1
    assert results[0].label == "Fly"


def test_build_definitions_from_grid_treats_missing_and_nan_cells_as_zero():
    rows = [
        {"Label": "Spread", _POS3[0]: 1, _POS3[1]: float("nan")},  # third column absent entirely
    ]
    results = build_definitions_from_grid(rows, _POS3, "SOFR", BarInterval.DAILY)
    assert len(results) == 1
    assert results[0].definition.offsets == (0,)
    assert results[0].definition.weights == (1.0,)


def test_build_definitions_from_grid_defaults_label_when_blank():
    rows = [{"Label": "", _POS3[0]: 1, _POS3[1]: -1, _POS3[2]: 0}]
    results = build_definitions_from_grid(rows, _POS3, "SOFR", BarInterval.DAILY)
    assert results[0].label == "Strategy 1"


# ---------------------------------------------------------------------
# apply_interval_override (Task 1: Scan Configuration's Interval is the
# single runtime interval for every leg of a scan)
# ---------------------------------------------------------------------

def _definition(market_key="SOFR", interval=BarInterval.DAILY, weights=(1.0, -2.0, 1.0)) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=tuple(range(len(weights))), weights=weights, interval=interval,
    )


def test_apply_interval_override_forces_every_definition_to_one_interval():
    definitions = [
        _definition(market_key="SOFR", interval=BarInterval.HOURLY),
        _definition(market_key="SONIA", interval=BarInterval.FOUR_HOUR),
        _definition(market_key="CORRA", interval=BarInterval.DAILY),
    ]
    overridden = apply_interval_override(definitions, BarInterval.DAILY)
    assert [d.interval for d in overridden] == [BarInterval.DAILY] * 3


def test_apply_interval_override_leaves_market_offsets_weights_untouched():
    original = _definition(market_key="SONIA", interval=BarInterval.DAILY, weights=(1.0, -1.0))
    (overridden,) = apply_interval_override([original], BarInterval.HOURLY)
    assert overridden.market_key == original.market_key
    assert overridden.offsets == original.offsets
    assert overridden.weights == original.weights
    assert overridden.price_field == original.price_field
    assert overridden.interval == BarInterval.HOURLY


def test_apply_interval_override_does_not_mutate_the_original_definitions():
    original = _definition(interval=BarInterval.DAILY)
    apply_interval_override([original], BarInterval.HOURLY)
    assert original.interval == BarInterval.DAILY


def test_apply_interval_override_on_empty_list():
    assert apply_interval_override([], BarInterval.DAILY) == []


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


def test_every_filter_spec_has_help_text():
    for spec in ALL_FILTER_SPECS:
        assert spec.help_text


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


def test_filter_specs_exclude_signed_z_score_but_keep_abs_z_score_min():
    # Trader-facing filter cleanup: signed Z-Score min/max controls are
    # redundant once Absolute Z-Score filtering exists, so they're
    # removed from FILTER_SPECS -- but signed z_score itself stays fully
    # available elsewhere (RangeAnalytics, canonical metric resolution,
    # RANK_METRIC_OPTIONS, results, Selected Strategy); see
    # test_rank_metric_options_include_z_score_and_absolute_z_score.
    by_key = {spec.key: spec for spec in FILTER_SPECS}
    assert "z_score_min" not in by_key
    assert "z_score_max" not in by_key
    assert by_key["abs_z_score_min"].field == "abs_z_score"
    assert by_key["abs_z_score_min"].bound == "min"


def test_filter_specs_include_oscillation_count_and_movement_minimums():
    by_key = {spec.key: spec for spec in FILTER_SPECS}
    assert by_key["oscillation_count_min"].field == "oscillation_count"
    assert by_key["oscillation_count_min"].bound == "min"
    assert by_key["mean_abs_change_bp_min"].field == "mean_abs_change_bp"
    assert by_key["mean_abs_change_bp_min"].bound == "min"


# ---------------------------------------------------------------------
# build_sort_keys / format_ranked_by
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


def test_format_ranked_by_primary_only_ascending():
    rank_state = {
        "primary_field": "efficiency_ratio",
        "primary_ascending": True,
        "secondary_field": None,
        "secondary_ascending": True,
    }
    text = format_ranked_by(rank_state)
    assert text == "Ranked by: Efficiency Ratio ↑ · Lower is better"


def test_format_ranked_by_descending_says_higher_is_better():
    rank_state = {
        "primary_field": "ar1_r_squared",
        "primary_ascending": False,
        "secondary_field": None,
        "secondary_ascending": True,
    }
    text = format_ranked_by(rank_state)
    assert "↓" in text
    assert "Higher is better" in text


def test_format_ranked_by_includes_secondary_when_set():
    rank_state = {
        "primary_field": "efficiency_ratio",
        "primary_ascending": True,
        "secondary_field": "ar1_beta",
        "secondary_ascending": False,
    }
    text = format_ranked_by(rank_state)
    assert "then AR(1) Beta ↓" in text


def test_format_ranked_by_omits_secondary_when_none():
    rank_state = {
        "primary_field": "efficiency_ratio",
        "primary_ascending": True,
        "secondary_field": NO_SECONDARY_RANK,
        "secondary_ascending": True,
    }
    assert "then" not in format_ranked_by(rank_state)


def test_rank_metric_options_include_z_score_and_absolute_z_score():
    fields = {field for _, field in RANK_METRIC_OPTIONS}
    assert "z_score" in fields
    assert "abs_z_score" in fields


def test_rank_metric_options_include_oscillation_count_and_movement():
    fields = {field for _, field in RANK_METRIC_OPTIONS}
    assert "oscillation_count" in fields
    assert "mean_abs_change_bp" in fields


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


def test_fmt_label_renders_none_as_dash():
    assert fmt_label(None) == "—"


def test_fmt_label_renders_blank_string_as_dash():
    assert fmt_label("   ") == "—"


def test_fmt_label_passes_through_a_real_label():
    assert fmt_label("Churning") == "Churning"


def test_to_display_dataframe_strategy_label_defaults_to_dash(_scan_candidate):
    # _scan_candidate has no label set (ScanCandidateResult.label defaults
    # to None) -- the new Strategy Label column must render the same
    # missing-value dash as every other column, never "None".
    results_df = results_to_dataframe([_scan_candidate], display_lookback=20)
    display = to_display_dataframe(results_df)
    assert display.iloc[0]["Strategy Label"] == "—"


def test_to_display_dataframe_shows_a_real_strategy_label(_scan_candidate):
    import dataclasses

    labeled = dataclasses.replace(_scan_candidate, label="Churning")
    results_df = results_to_dataframe([labeled], display_lookback=20)
    display = to_display_dataframe(results_df)
    assert display.iloc[0]["Strategy Label"] == "Churning"


def test_to_display_dataframe_empty_results():
    empty = results_to_dataframe([], display_lookback=20)
    display = to_display_dataframe(empty)
    assert display.empty
    assert list(display.columns)


def test_to_display_dataframe_preserves_row_order_and_formats_values(_scan_candidate):
    results_df = results_to_dataframe([_scan_candidate], display_lookback=20)
    display = to_display_dataframe(results_df)

    assert len(display) == 1
    assert display.iloc[0]["Strategy"] == " / ".join(_scan_candidate.rics)
    assert display.iloc[0]["Ratio"] == "1.00 / -2.00 / 1.00"
    # current_price is a real float for this fixture -- never the NaN dash.
    assert display.iloc[0]["Current"] != "—"


def test_display_columns_include_low_high_z_and_absolute_z():
    labels = [label for label, _, _ in DISPLAY_COLUMNS]
    assert "Low" in labels
    assert "High" in labels
    assert "Z" in labels
    assert "|Z|" in labels


def test_display_columns_exclude_ar1_beta_and_width_from_default_table():
    # Approved amendment: AR(1) Beta and Robust Width stay available for
    # filtering/ranking (FILTER_SPECS/RANK_METRIC_OPTIONS) but are not
    # shown in the default, compact results table.
    labels = [label for label, _, _ in DISPLAY_COLUMNS]
    assert "AR1 β" not in labels
    assert "Width" not in labels


def test_display_columns_match_approved_column_order():
    # Tradability Analytics: Movement/Osc took Cross Freq's place in the
    # default visible table to keep it compact. Cross Frequency stays
    # fully available in the backend -- FILTER_SPECS still has
    # "normalized_crossing_frequency_min" and RANK_METRIC_OPTIONS still
    # has "Normalized Crossing Frequency" -- only the default visible
    # table dropped it. Strategy Label is the newest column (Range Bound
    # Opportunities UI enhancement) -- optional, appended last, not in
    # DEFAULT_VISIBLE_COLUMNS (see test_default_visible_columns_* below).
    labels = [label for label, _, _ in DISPLAY_COLUMNS]
    assert labels == [
        "Strategy", "Ratio", "Current", "Low", "Median", "High",
        "Position", "Z", "|Z|", "Movement", "Osc", "ER", "Half-Life",
        "Strategy Label",
    ]
    assert "Cross Freq" not in labels


def test_result_column_help_covers_every_new_column():
    for label in ("Low", "High", "Position", "Z", "|Z|", "Movement", "Osc"):
        assert RESULT_COLUMN_HELP[label]


def test_to_display_dataframe_shows_low_high_z_for_a_real_candidate(_scan_candidate):
    results_df = results_to_dataframe([_scan_candidate], display_lookback=20)
    display = to_display_dataframe(results_df)
    row = display.iloc[0]

    assert row["Low"] != "—"
    assert row["High"] != "—"
    assert row["Z"] != "—"
    assert row["|Z|"] != "—"


def test_to_display_dataframe_shows_movement_and_osc_for_a_real_candidate(_scan_candidate):
    results_df = results_to_dataframe([_scan_candidate], display_lookback=20)
    display = to_display_dataframe(results_df)
    row = display.iloc[0]

    analytics = _scan_candidate.multi_lookback.per_lookback[
        _scan_candidate.multi_lookback.lookbacks_requested.index(20)
    ]
    assert row["Movement"] != "—"
    assert row["Movement"] == fmt_number(analytics.mean_abs_change_bp)
    assert row["Osc"] == str(analytics.oscillation_count)


def test_to_display_dataframe_z_renders_nan_as_dash():
    # A single-observation candidate: z_score/abs_z_score are NaN (std
    # undefined below 2 observations) -- must render as the dash, not
    # "nan" or a crash.
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1.0,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26",))
    history = StrategyHistory(
        instance=instance, price_field="Close",
        history=pd.DataFrame({"Date": pd.to_datetime(["2024-01-01"]), "Leg_1": [100.0], "Strategy": [100.0]}),
    )
    multi_lookback = analyze_multi_lookback(history, lookbacks=(20,))
    candidate = ScanCandidateResult(
        market_key="SOFR", rics=("SRAH26",), weights=(1.0,), offsets=(0,),
        interval=BarInterval.DAILY, price_field="Close", instance=instance, multi_lookback=multi_lookback,
    )

    results_df = results_to_dataframe([candidate], display_lookback=20)
    display = to_display_dataframe(results_df)
    row = display.iloc[0]
    assert row["Z"] == "—"
    assert row["|Z|"] == "—"


def test_add_rank_column_prepends_sequential_rank():
    df = pd.DataFrame({"Strategy": ["A", "B", "C"]})
    ranked = add_rank_column(df)
    assert list(ranked.columns)[0] == "Rank"
    assert list(ranked["Rank"]) == ["#1", "#2", "#3"]
    # Row order/content otherwise untouched.
    assert list(ranked["Strategy"]) == ["A", "B", "C"]


# ---------------------------------------------------------------------
# Column selector (OPTIONAL_COLUMN_LABELS / DEFAULT_VISIBLE_COLUMNS /
# apply_column_selection) -- Range Bound Opportunities UI enhancement.
# ---------------------------------------------------------------------

def test_optional_column_labels_is_rank_plus_every_display_column():
    assert OPTIONAL_COLUMN_LABELS[0] == RANK_COLUMN
    assert OPTIONAL_COLUMN_LABELS[1:] == tuple(label for label, _, _ in DISPLAY_COLUMNS)


def test_default_visible_columns_excludes_only_strategy_label():
    assert STRATEGY_LABEL_COLUMN not in DEFAULT_VISIBLE_COLUMNS
    assert set(DEFAULT_VISIBLE_COLUMNS) == set(OPTIONAL_COLUMN_LABELS) - {STRATEGY_LABEL_COLUMN}


def test_default_visible_columns_matches_the_original_thirteen_plus_rank():
    # Pinning test: this is the exact set that must render when every
    # column is left at its default (existing behaviour/appearance
    # preserved) -- Strategy Label is the only new column and it is
    # deliberately excluded here.
    assert list(DEFAULT_VISIBLE_COLUMNS) == [
        "Rank", "Strategy", "Ratio", "Current", "Low", "Median", "High",
        "Position", "Z", "|Z|", "Movement", "Osc", "ER", "Half-Life",
    ]


def test_apply_column_selection_keeps_only_selected_columns_in_original_order():
    df = pd.DataFrame({"Rank": ["#1"], "Strategy": ["SRAH26"], "Current": [1.0], "Z": [0.1]})
    projected = apply_column_selection(df, ["Z", "Rank"])  # selection order shouldn't matter
    assert list(projected.columns) == ["Rank", "Z"]


def test_apply_column_selection_empty_selection_returns_zero_columns_same_rows():
    df = pd.DataFrame({"Rank": ["#1", "#2"], "Strategy": ["A", "B"]})
    projected = apply_column_selection(df, [])
    assert list(projected.columns) == []
    assert len(projected) == 2


def test_apply_column_selection_never_recomputes_values():
    df = pd.DataFrame({"Rank": ["#1"], "Current": [42.0]})
    projected = apply_column_selection(df, ["Current"])
    assert projected.iloc[0]["Current"] == 42.0


# ---------------------------------------------------------------------
# Market filter options (available_markets) -- Range Bound Opportunities
# UI enhancement.
# ---------------------------------------------------------------------

def test_available_markets_returns_sorted_unique_market_keys(_scan_candidate):
    import dataclasses

    other = dataclasses.replace(_scan_candidate, market_key="SONIA")
    assert available_markets([_scan_candidate, other, _scan_candidate]) == ["SOFR", "SONIA"]


def test_available_markets_empty_results():
    assert available_markets([]) == []


def test_selected_strategy_summary_fields(_scan_candidate):
    summary = selected_strategy_summary(_scan_candidate, display_lookback=20)

    assert summary["rics"] == " / ".join(_scan_candidate.rics)
    assert summary["weights"] == "1.00 / -2.00 / 1.00"
    assert summary["interval"] == "DAILY"
    assert "–" in summary["robust_range"]  # combined "low – high" string
    assert summary["current"] != "—"


def test_selected_strategy_summary_includes_mean_robust_bounds_and_z_score(_scan_candidate):
    summary = selected_strategy_summary(_scan_candidate, display_lookback=20)

    analytics = _scan_candidate.multi_lookback.per_lookback[
        _scan_candidate.multi_lookback.lookbacks_requested.index(20)
    ]
    assert summary["mean"] == fmt_number(analytics.mean)
    assert summary["median"] == fmt_number(analytics.median)
    assert summary["robust_low"] == fmt_number(analytics.range_low_robust)
    assert summary["robust_high"] == fmt_number(analytics.range_high_robust)
    assert summary["z_score"] == fmt_number(analytics.z_score, 2)
    assert summary["efficiency_ratio"] == fmt_number(analytics.efficiency_ratio)


def test_selected_strategy_summary_includes_percentile_range_label(_scan_candidate):
    summary = selected_strategy_summary(_scan_candidate, display_lookback=20)
    assert summary["percentile_range_label"] == "P5-P95"


def test_selected_strategy_summary_includes_movement_and_oscillations(_scan_candidate):
    summary = selected_strategy_summary(_scan_candidate, display_lookback=20)

    analytics = _scan_candidate.multi_lookback.per_lookback[
        _scan_candidate.multi_lookback.lookbacks_requested.index(20)
    ]
    assert summary["movement"] == fmt_number(analytics.mean_abs_change_bp, 2)
    assert summary["oscillations"] == fmt_number(analytics.oscillation_count, 0)
    assert summary["oscillations"] != "—"
    # Percentile-range label stays alongside Oscillations -- the count is
    # only meaningful together with the boundaries it was computed from.
    assert summary["percentile_range_label"] == "P5-P95"


# ---------------------------------------------------------------------
# Percentile formatting: integer-style default, decimal preserved when needed
# ---------------------------------------------------------------------

def test_format_percentile_renders_whole_numbers_without_decimal():
    assert format_percentile(5.0) == "5"
    assert format_percentile(95.0) == "95"
    assert format_percentile(0.0) == "0"


def test_format_percentile_renders_fractional_values_with_decimal():
    assert format_percentile(12.5) == "12.5"


def test_format_percentile_range_default_band():
    assert format_percentile_range(5.0, 95.0) == "P5-P95"


def test_format_percentile_range_custom_band():
    assert format_percentile_range(25.0, 75.0) == "P25-P75"


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
