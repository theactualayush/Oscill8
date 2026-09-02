"""
tests/test_template_scanner_scan_results.py

ScanCandidateResult / results_to_dataframe() tested against real
analyze_multi_lookback() output on hand-built StrategyHistory fixtures
-- no I/O, same pattern used in tests/test_range_multi_lookback.py.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.config import BarInterval
from range_analytics import analyze_multi_lookback
from range_analytics.multi_lookback import range_to_volatility_ratio, robust_to_full_width_ratio
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory

from template_scanner.filters import at_lookback as filter_at_lookback
from template_scanner.metrics import at_lookback, normalized_crossing_frequency
from template_scanner.scan_results import ScanCandidateResult, results_to_dataframe


def _history(
    dates: list[str],
    values: list[float],
    market_key: str = "SOFR",
    rics: tuple[str, ...] = ("SRAH26", "SRAM26", "SRAU26"),
    weights: tuple[float, ...] = (1.0, -2.0, 1.0),
    offsets: tuple[int, ...] = (0, 1, 2),
    interval: BarInterval = BarInterval.DAILY,
) -> StrategyHistory:
    definition = StrategyDefinition(
        market_key=market_key, offsets=offsets, weights=weights, interval=interval,
    )
    instance = StrategyInstance(definition=definition, rics=rics)
    df = pd.DataFrame(
        {"Date": pd.to_datetime(dates), "Leg_1": values, "Strategy": values}
    )
    return StrategyHistory(instance=instance, price_field="Close", history=df)


def _dates(n: int, start: str = "2020-01-01") -> list[str]:
    return pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()


def _candidate(
    values=None,
    lookbacks=(20, 40, 60),
    rics=("SRAH26", "SRAM26", "SRAU26"),
    weights=(1.0, -2.0, 1.0),
    interval=BarInterval.DAILY,
    label=None,
) -> ScanCandidateResult:
    values = values if values is not None else ([0.98, 1.00, 1.02] * 60)[:150]
    history = _history(
        _dates(len(values)), values, rics=rics, weights=weights, interval=interval,
    )
    multi_lookback = analyze_multi_lookback(history, lookbacks=lookbacks)
    definition = history.instance.definition
    return ScanCandidateResult(
        market_key=definition.market_key,
        rics=history.instance.rics,
        weights=definition.weights,
        offsets=definition.offsets,
        interval=definition.interval,
        price_field=history.price_field,
        instance=history.instance,
        multi_lookback=multi_lookback,
        label=label,
    )


def test_results_to_dataframe_empty_list_returns_empty_frame_with_curated_columns():
    df = results_to_dataframe([], display_lookback=20)
    assert df.empty
    assert len(df.columns) == 54


def test_results_to_dataframe_column_set_has_exactly_54_curated_columns():
    # 50 pre-existing (46 original + lower_percentile/upper_percentile/
    # z_score/abs_z_score) + oscillation_count/mean_abs_change_price/
    # mean_abs_change_bp (Tradability Analytics: Oscillation Count and
    # Movement, exposed as canonical Module 5B metrics) + label (Range
    # Bound Opportunities UI enhancement: the optional Strategy Label
    # column, sourced from ScanCandidateResult.label).
    df = results_to_dataframe([_candidate()], display_lookback=20)
    assert len(df.columns) == 54


def test_results_to_dataframe_excludes_tuple_valued_stability_internals():
    df = results_to_dataframe([_candidate()], display_lookback=20)
    forbidden_substrings = ["values", "pairwise", "_min", "_max", "defined_count", "ratio_"]
    for column in df.columns:
        # "range_to_volatility_ratio"/"robust_to_full_width_ratio" legitimately
        # contain "ratio" but not as a stability-object internal ("ratio_"
        # with trailing underscore, e.g. short_vs_long_ratio, is what's banned).
        assert "pairwise" not in column
        assert not column.endswith("_values")
        assert "short_vs_long_ratio" not in column
        assert not column.endswith("_min")
        assert not column.endswith("_max")
        assert "defined_count" not in column


def test_results_to_dataframe_one_row_per_candidate():
    df = results_to_dataframe([_candidate(), _candidate(rics=("SRAM26", "SRAU26", "SRAZ26"))], display_lookback=20)
    assert len(df) == 2


def test_results_to_dataframe_raises_for_unrequested_display_lookback():
    with pytest.raises(ValueError, match="90"):
        results_to_dataframe([_candidate(lookbacks=(20, 40, 60))], display_lookback=90)


def test_results_to_dataframe_label_defaults_to_none():
    df = results_to_dataframe([_candidate()], display_lookback=20)
    assert df.iloc[0]["label"] is None


def test_results_to_dataframe_carries_explicit_label():
    df = results_to_dataframe([_candidate(label="My Strategy Set Entry")], display_lookback=20)
    assert df.iloc[0]["label"] == "My Strategy Set Entry"


def test_results_to_dataframe_preserves_exact_scaled_weights():
    scaled = _candidate(weights=(2.0, -4.0, 2.0))
    df = results_to_dataframe([scaled], display_lookback=20)
    assert df.iloc[0]["weights"] == (2.0, -4.0, 2.0)


def test_results_to_dataframe_headline_values_match_selected_lookback():
    candidate = _candidate(lookbacks=(20, 40, 60))
    df = results_to_dataframe([candidate], display_lookback=40)

    expected = at_lookback(candidate.multi_lookback, 40)
    row = df.iloc[0]
    assert row["current_price"] == pytest.approx(expected.current_price)
    assert row["efficiency_ratio"] == pytest.approx(expected.efficiency_ratio)
    assert row["half_life"] == pytest.approx(expected.half_life, nan_ok=True)
    assert row["normalized_crossing_frequency"] == pytest.approx(
        normalized_crossing_frequency(expected), nan_ok=True
    )
    assert row["range_to_volatility_ratio"] == pytest.approx(
        range_to_volatility_ratio(expected), nan_ok=True
    )
    assert row["robust_to_full_width_ratio"] == pytest.approx(
        robust_to_full_width_ratio(expected), nan_ok=True
    )


def test_results_to_dataframe_stability_columns_match_source_object():
    candidate = _candidate()
    df = results_to_dataframe([candidate], display_lookback=20)
    row = df.iloc[0]

    assert row["efficiency_ratio_stability_stdev"] == pytest.approx(
        candidate.multi_lookback.efficiency_ratio_stability.stdev, nan_ok=True
    )
    assert row["half_life_stability_short_vs_long_diff"] == pytest.approx(
        candidate.multi_lookback.half_life_stability.short_vs_long_diff, nan_ok=True
    )


def test_scan_candidate_result_retains_full_multi_lookback_object():
    candidate = _candidate(lookbacks=(20, 40, 60))
    assert candidate.multi_lookback.lookbacks_requested == (20, 40, 60)
    assert len(candidate.multi_lookback.per_lookback) == 3
    # tuple-valued internals not present in the curated table remain
    # reachable through the full object
    assert isinstance(candidate.multi_lookback.efficiency_ratio_stability.values, tuple)


def test_results_to_dataframe_dtype_of_interval_is_plain_string():
    df = results_to_dataframe([_candidate(interval=BarInterval.HOURLY)], display_lookback=20)
    assert df.iloc[0]["interval"] == "HOURLY"


def test_results_to_dataframe_and_filter_accessor_agree_on_derived_metrics():
    # Cross-consistency proof: results_to_dataframe() and
    # filters.at_lookback() both resolve derived metrics through the
    # same template_scanner.metrics.metric_value() -- so a scanner-grid
    # column and a filter/rank accessor built from the same metric name
    # and lookback must agree exactly, not merely by convention.
    candidate = _candidate(lookbacks=(20, 40, 60))
    df = results_to_dataframe([candidate], display_lookback=40)
    row = df.iloc[0]

    for field in (
        "normalized_crossing_frequency",
        "range_to_volatility_ratio",
        "robust_to_full_width_ratio",
    ):
        accessor_value = filter_at_lookback(field, 40)(candidate)
        assert row[field] == pytest.approx(accessor_value, nan_ok=True)
