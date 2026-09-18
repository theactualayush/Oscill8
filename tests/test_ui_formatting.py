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

from core import config
from core.config import BarInterval
from core.ric import build_ric

from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition

from range_analytics.multi_lookback import analyze_multi_lookback

from strategy_engine.pricing import StrategyHistory

from strategy_sets.composite import CompositeCombinationName

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
    RESULT_COLUMN_WIDTHS,
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
    format_contract,
    format_strategy_label,
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
    # Fixture rics are ("SRAZ25", "SRAH26", "SRAM26").
    assert display.iloc[0]["Strategy Label"] == "SRA Z25 Churning"


def test_to_display_dataframe_empty_results():
    empty = results_to_dataframe([], display_lookback=20)
    display = to_display_dataframe(empty)
    assert display.empty
    assert list(display.columns)


def test_to_display_dataframe_preserves_row_order_and_formats_values(_scan_candidate):
    results_df = results_to_dataframe([_scan_candidate], display_lookback=20)
    display = to_display_dataframe(results_df)

    assert len(display) == 1
    assert display.iloc[0]["Ratio"] == "1.00 / -2.00 / 1.00"
    # current_price is a real float for this fixture -- never the NaN dash.
    assert display.iloc[0]["Current"] != "—"


# ---------------------------------------------------------------------
# format_strategy_label -- the composite ("Group A x Group B") Strategy
# Label. Range-Bound Opportunities label change.
# ---------------------------------------------------------------------

def _composite_name(name_a="3M Fly", name_b="12M Sprd", legs_a=3, legs_b=2):
    """The CompositeCombinationName strategy_sets.composite.
    composite_labels_by_definition_id() puts on a composite
    candidate's ScanCandidateResult.label."""
    return CompositeCombinationName(
        name_a=name_a, name_b=name_b, leg_count_a=legs_a, leg_count_b=legs_b
    )


def test_composite_combination_name_is_still_the_plain_name_string():
    # The enriched label IS the "A - B" string every existing consumer
    # already saw -- nothing downstream had to change to accept it.
    name = _composite_name()
    assert name == "3M Fly - 12M Sprd"
    assert isinstance(name, str)


def test_format_strategy_label_ordinary_gets_product_and_first_contract():
    # The grid's only identity column must say WHICH product and WHICH
    # contract, not just the trader's name for the shape.
    assert format_strategy_label("3M Fly 2", ("SRAH27", "SRAM27", "SRAU27")) == (
        "SRA H27 3M Fly 2"
    )
    assert format_strategy_label("3m sprd", ("SRAH27", "SRAU27")) == "SRA H27 3m sprd"
    assert format_strategy_label("3M Dfly", ("SRAH27",)) == "SRA H27 3M Dfly"


def test_format_strategy_label_uses_the_first_leg_not_a_sorted_one():
    # Deliberately non-alphabetical, non-chronological leg order: the
    # product/contract come from rics[0] exactly as the instance carries
    # it, never from sorting and never from the strategy name.
    assert format_strategy_label("3mf1-3", ("SRAU27", "SRAH27", "SRAM28")) == (
        "SRA U27 3mf1-3"
    )


@pytest.mark.parametrize(
    "ric, expected",
    [
        ("SRAH27", "SRA H27"),      # SOFR, 2-digit year
        ("FFH27", "FF H27"),        # Fed Funds, 2-digit year
        ("SONH7", "SON H7"),        # SONIA, 1-digit year
        ("CRAU6", "CRA U6"),        # CORRA, 1-digit year
        ("SREH27", "SRE H27"),      # CME ESTR
        ("FEIH7", "FEI H7"),        # EURIBOR
        ("SARO3H7", "SARO3 H7"),    # SARON, 5-character root
        ("YBAH7", "YBA H7"),        # Australia 90-day bank bill
        ("EON3H7", "EON3 H7"),      # ICE ESTR
    ],
)
def test_format_contract_splits_every_configured_market_at_its_own_root(ric, expected):
    # One rule, no per-market branch: split at the market's registered
    # ric_root length, resolved through core.ric.parse_ric.
    assert format_contract(ric) == expected


def test_format_contract_matches_the_real_ric_builder_for_every_market():
    # Anchored to the registry itself, so a future market (or a changed
    # root/year-digit convention) is covered without editing this test.
    for market_key, market in config.MARKETS.items():
        ric = build_ric(market_key, 3, 2027)
        assert format_contract(ric) == f"{market.ric_root} {ric[len(market.ric_root):]}"


def test_format_strategy_label_works_for_non_sofr_products():
    assert format_strategy_label("3M Fly", ("CRAU6", "CRAZ6", "CRAH7")) == (
        "CRA U6 3M Fly"
    )
    assert format_strategy_label("6M Sprd", ("SONH7", "SONU7")) == "SON H7 6M Sprd"


def test_format_strategy_label_falls_back_when_the_ric_does_not_parse():
    # An unparseable RIC preserves the existing label rather than
    # guessing a product or emitting a half-formed prefix.
    assert format_strategy_label("3m sprd", ("NOTARIC",)) == "3m sprd"
    assert format_contract("NOTARIC") is None


def test_format_strategy_label_falls_back_when_there_are_no_rics():
    assert format_strategy_label("3m sprd", ()) == "3m sprd"


def test_format_strategy_label_missing_ordinary_label_still_renders_a_dash():
    assert format_strategy_label(None, ("SRAU27",)) == "—"
    assert format_strategy_label("   ", ("SRAU27",)) == "—"


def test_format_strategy_label_composite_uses_each_side_own_first_contract():
    # A = 3-leg fly on SRAU27/SRAZ27/SRAH28, B = 2-leg spread on
    # SRAU27/SRAU28. Legs are laid out A-then-B, and rics positionally
    # match them, so A's contract is rics[0] and B's is rics[3].
    rics = ("SRAU27", "SRAZ27", "SRAH28", "SRAU27", "SRAU28")
    assert (
        format_strategy_label(_composite_name(), rics)
        == "SRA U27 3M Fly - SRA U27 12M Sprd"
    )


def test_format_strategy_label_composite_sides_can_start_on_different_contracts():
    rics = ("SRAM27", "SRAZ27", "SRAZ27", "SRAM28")
    label = _composite_name(name_a="6M Sprd", name_b="6M Sprd", legs_a=2, legs_b=2)
    assert format_strategy_label(label, rics) == "SRA M27 6M Sprd - SRA Z27 6M Sprd"


def test_format_strategy_label_composite_across_two_different_products():
    # Group A on SOFR, Group B on CORRA -- each side shows its OWN
    # product and its own first/nearest contract.
    rics = ("SRAM27", "SRAU27", "CRAU6", "CRAZ6")
    label = _composite_name(name_a="3m sprd", name_b="3M Sprd", legs_a=2, legs_b=2)
    assert format_strategy_label(label, rics) == "SRA M27 3m sprd - CRA U6 3M Sprd"


def test_format_strategy_label_never_flips_group_a_and_group_b():
    rics = ("SRAU27", "SRAZ27", "SRAH28", "SRAU27", "SRAU28")
    rendered = format_strategy_label(_composite_name(), rics)
    assert rendered.startswith("SRA U27 3M Fly")
    assert rendered.endswith("SRA U27 12M Sprd")
    assert rendered != "SRA U27 12M Sprd - SRA U27 3M Fly"
    # The A/B sides are read from their own named fields, never by
    # splitting the name string apart.
    assert rendered.index("3M Fly") < rendered.index("12M Sprd")


def test_format_strategy_label_does_not_sort_or_reorder_contracts():
    # Deliberately non-alphabetical, non-chronological rics: the
    # instance's own leg order is used exactly as given.
    rics = ("SRAZ27", "SRAM27", "SRAU28", "SRAH28", "SRAU27")
    label = _composite_name(name_a="3M Dfly", name_b="6M Sprd", legs_a=3, legs_b=2)
    assert format_strategy_label(label, rics) == "SRA Z27 3M Dfly - SRA H28 6M Sprd"


def test_format_strategy_label_same_strategy_on_both_sides_still_renders_both():
    # A composite of a strategy against itself is structurally zero and
    # the backend drops it, but the formatter must not depend on that --
    # given one, it renders both sides.
    rics = ("SRAU27", "SRAZ27", "SRAH28", "SRAU27", "SRAZ27", "SRAH28")
    label = _composite_name(name_a="3M Fly", name_b="3M Fly", legs_a=3, legs_b=3)
    assert format_strategy_label(label, rics) == "SRA U27 3M Fly - SRA U27 3M Fly"


def test_format_strategy_label_composite_falls_back_if_either_side_cannot_parse():
    # One row must never show a product for one half and not the other.
    label = _composite_name(name_a="3M Fly", name_b="12M Sprd", legs_a=1, legs_b=1)
    assert format_strategy_label(label, ("SRAH27", "NOTARIC")) == "3M Fly - 12M Sprd"
    assert format_strategy_label(label, ("NOTARIC", "SRAH27")) == "3M Fly - 12M Sprd"


def test_format_strategy_label_falls_back_when_rics_cannot_cover_the_split():
    # Structurally impossible for a real instance (every leg produces
    # exactly one RIC) -- it must degrade to the plain name, never
    # raise and never invent a contract.
    assert format_strategy_label(_composite_name(), ("SRAU27", "SRAZ27")) == (
        "3M Fly - 12M Sprd"
    )


def test_to_display_dataframe_renders_a_composite_strategy_label(_scan_candidate):
    import dataclasses

    labeled = dataclasses.replace(
        _scan_candidate,
        label=_composite_name(name_a="3M Fly", name_b="6M Sprd", legs_a=2, legs_b=1),
    )
    results_df = results_to_dataframe([labeled], display_lookback=20)
    display = to_display_dataframe(results_df)
    # The fixture's rics are ("SRAZ25", "SRAH26", "SRAM26").
    assert display.iloc[0][STRATEGY_LABEL_COLUMN] == "SRA Z25 3M Fly - SRA M26 6M Sprd"


def test_to_display_dataframe_no_longer_has_a_raw_ric_strategy_column(_scan_candidate):
    # Range-Bound Opportunities table change: the raw-RIC "Strategy"
    # column ("SRAZ25 / SRAH26 / SRAM26") was removed in favour of the
    # single human-readable Strategy Label column. No blank replacement
    # column is left behind, and the RICs themselves are unaffected --
    # they still reach the Selected Strategy panel via
    # selected_strategy_summary() and remain on the candidate itself.
    results_df = results_to_dataframe([_scan_candidate], display_lookback=20)
    display = to_display_dataframe(results_df)

    assert "Strategy" not in display.columns
    assert STRATEGY_LABEL_COLUMN in display.columns
    assert "rics" in results_df.columns          # untouched upstream
    assert _scan_candidate.rics == ("SRAZ25", "SRAH26", "SRAM26")


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
    # table dropped it. Strategy Label is now the table's ONLY identity
    # column and leads it: the raw-RIC "Strategy" column was removed
    # (Range-Bound Opportunities label change) with no blank column
    # left in its place.
    labels = [label for label, _, _ in DISPLAY_COLUMNS]
    assert labels == [
        "Strategy Label", "Ratio", "Current", "Low", "Median", "High",
        "Position", "Z", "|Z|", "Movement", "Osc", "ER", "Half-Life",
    ]
    assert "Cross Freq" not in labels
    assert "Strategy" not in labels


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


def test_default_visible_columns_includes_strategy_label():
    # Strategy Label used to start hidden, back when the raw-RIC
    # "Strategy" column was the table's identity column. Now that it IS
    # that column, hiding it by default would leave a nameless table.
    assert STRATEGY_LABEL_COLUMN in DEFAULT_VISIBLE_COLUMNS
    assert set(DEFAULT_VISIBLE_COLUMNS) == set(OPTIONAL_COLUMN_LABELS)


def test_default_visible_columns_matches_the_approved_table():
    # Pinning test: this is the exact set that must render when every
    # column is left at its default.
    assert list(DEFAULT_VISIBLE_COLUMNS) == [
        "Rank", "Strategy Label", "Ratio", "Current", "Low", "Median",
        "High", "Position", "Z", "|Z|", "Movement", "Osc", "ER",
        "Half-Life",
    ]
    assert "Strategy" not in DEFAULT_VISIBLE_COLUMNS


def test_strategy_label_column_is_still_independently_hideable():
    # Every column, Strategy Label included, stays optional -- row
    # selection is positional, so nothing depends on it being visible.
    assert STRATEGY_LABEL_COLUMN in OPTIONAL_COLUMN_LABELS
    df = pd.DataFrame({"Rank": ["#1"], STRATEGY_LABEL_COLUMN: ["X"], "Z": [0.1]})
    assert list(apply_column_selection(df, ["Rank", "Z"]).columns) == ["Rank", "Z"]


def test_result_column_widths_give_strategy_label_room():
    # A composite label ("SRAU27 3M Fly - SRAU27 12M Sprd") is far wider
    # than the numeric metric columns beside it.
    assert RESULT_COLUMN_WIDTHS[STRATEGY_LABEL_COLUMN] == "large"


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
