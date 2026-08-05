"""
formatting.py

Pure helper functions for Module 6A's Streamlit UI: parsing user-entered
ratio text into dense weight vectors, translating template-grid rows
into strategy_engine.StrategyDefinition via the existing
template_scanner.template_from_dense_weights(), constructing filter/
ranking accessors from template_scanner's existing canonical metric
resolution (FilterCriterion / SortKey / at_lookback / stability), and
formatting values for the result grid.

No analytics, filtering, or ranking value is computed here -- every
number and every pass/fail or sort decision comes from strategy_engine /
range_analytics / template_scanner, unmodified. This module only shapes
UI input into their existing call shapes and formats their output for
display.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition

from template_scanner.filters import FilterCriterion
from template_scanner.filters import at_lookback as filter_at_lookback
from template_scanner.filters import stability as filter_stability
from template_scanner.ranking import SortKey
from template_scanner.templates import template_from_dense_weights

_RATIO_SPLIT_RE = re.compile(r"[|,\s]+")
_NAN = float("nan")


def parse_dense_weights(ratio_text: str) -> list[float]:
    """Parse a user-entered ratio string (e.g. "1 | -2 | 1") into a dense
    weight vector, one weight per consecutive curve position. Accepts
    '|', ',' or whitespace as separators.

    Raises:
        ValueError: the text is empty (after stripping separators) or
            contains a non-numeric token.
    """
    tokens = [t for t in _RATIO_SPLIT_RE.split((ratio_text or "").strip()) if t]
    if not tokens:
        raise ValueError("Ratio is empty")
    try:
        return [float(t) for t in tokens]
    except ValueError as exc:
        raise ValueError(f"'{ratio_text}' contains a non-numeric value") from exc


@dataclass(frozen=True)
class TemplateRowResult:
    """One template-grid row's parse outcome -- either a StrategyDefinition
    or a user-facing error message, never both."""

    row_index: int
    ratio_text: str
    definition: StrategyDefinition | None
    error: str | None


def build_definitions(
    ratio_rows: Sequence[str],
    market_key: str,
    interval: BarInterval,
    price_field: str = "Close",
) -> list[TemplateRowResult]:
    """Translate template-grid ratio rows into StrategyDefinitions.

    Blank rows (an empty extra row in a dynamic grid) are silently
    skipped -- not an error. All strategy-shape validation
    (offsets/weights, market, interval, price_field) is delegated to
    template_from_dense_weights() / StrategyDefinition -- never
    duplicated here.
    """
    results: list[TemplateRowResult] = []
    for i, ratio_text in enumerate(ratio_rows):
        text = (ratio_text or "").strip()
        if not text:
            continue
        try:
            weights = parse_dense_weights(text)
            definition = template_from_dense_weights(market_key, weights, interval, price_field)
            results.append(TemplateRowResult(i, text, definition, None))
        except ValueError as exc:
            results.append(TemplateRowResult(i, text, None, str(exc)))
    return results


# ---------------------------------------------------------------------
# Section E -- Range-Bound Filters
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class FilterSpec:
    """One available filter: a label, the metric it resolves (via
    template_scanner's canonical metric_value()/at_lookback()), and
    which bound ("min" or "max") the user-entered threshold applies to."""

    key: str
    label: str
    field: str
    bound: str  # "min" or "max"


FILTER_SPECS: tuple[FilterSpec, ...] = (
    FilterSpec("efficiency_ratio_max", "Efficiency Ratio (max)", "efficiency_ratio", "max"),
    FilterSpec(
        "normalized_crossing_frequency_min",
        "Normalized Crossing Frequency (min)",
        "normalized_crossing_frequency",
        "min",
    ),
    FilterSpec("ar1_beta_max", "AR(1) Beta (max)", "ar1_beta", "max"),
    FilterSpec("half_life_max", "Half-Life (max)", "half_life", "max"),
    FilterSpec("range_width_robust_max", "Robust Range Width (max)", "range_width_robust", "max"),
    FilterSpec("ar1_r_squared_min", "AR(1) R² (min)", "ar1_r_squared", "min"),
)

# One stability filter (Module 4B), per the Module 6A spec's "potentially
# one useful stability filter if it integrates cleanly": bounds how much
# a candidate's efficiency ratio moves across the requested lookbacks.
STABILITY_FILTER_SPEC = FilterSpec(
    "efficiency_ratio_stability_stdev_max",
    "Efficiency Ratio Stability, stdev (max)",
    "efficiency_ratio",
    "max",
)

ALL_FILTER_SPECS: tuple[FilterSpec, ...] = FILTER_SPECS + (STABILITY_FILTER_SPEC,)


def build_filter_criteria(
    filter_state: dict[str, dict],
    display_lookback: int,
) -> list[FilterCriterion]:
    """Build FilterCriterion objects for every ENABLED filter with a
    threshold set. `filter_state` maps FilterSpec.key ->
    {"enabled": bool, "value": float | None}. A disabled filter (or one
    left without a value) produces no FilterCriterion -- filtering stays
    entirely opt-in, matching template_scanner.filters' own contract.
    """
    criteria: list[FilterCriterion] = []
    for spec in FILTER_SPECS:
        state = filter_state.get(spec.key) or {}
        if not state.get("enabled") or state.get("value") is None:
            continue
        accessor = filter_at_lookback(spec.field, display_lookback)
        bound = {"max_value": state["value"]} if spec.bound == "max" else {"min_value": state["value"]}
        criteria.append(FilterCriterion(name=spec.label, accessor=accessor, **bound))

    stability_state = filter_state.get(STABILITY_FILTER_SPEC.key) or {}
    if stability_state.get("enabled") and stability_state.get("value") is not None:
        accessor = filter_stability("efficiency_ratio", "stdev")
        criteria.append(
            FilterCriterion(
                name=STABILITY_FILTER_SPEC.label,
                accessor=accessor,
                max_value=stability_state["value"],
            )
        )
    return criteria


# ---------------------------------------------------------------------
# Section F -- Ranking
# ---------------------------------------------------------------------

RANK_METRIC_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Efficiency Ratio", "efficiency_ratio"),
    ("Normalized Crossing Frequency", "normalized_crossing_frequency"),
    ("AR(1) Beta", "ar1_beta"),
    ("AR(1) R²", "ar1_r_squared"),
    ("Half-Life", "half_life"),
    ("Robust Range Width", "range_width_robust"),
    ("Range Position (Robust)", "range_position_robust"),
    ("Realized Vol (bp)", "realized_vol_bp"),
    ("Range/Volatility Ratio", "range_to_volatility_ratio"),
    ("Robust/Full Width Ratio", "robust_to_full_width_ratio"),
)

NO_SECONDARY_RANK = "(none)"


def build_sort_keys(
    primary_field: str,
    primary_ascending: bool,
    secondary_field: str | None,
    secondary_ascending: bool,
    display_lookback: int,
) -> list[SortKey]:
    """Build the SortKey list for rank_results() from Section F's primary
    (required) and optional secondary ranking controls."""
    keys = [
        SortKey(accessor=filter_at_lookback(primary_field, display_lookback), ascending=primary_ascending)
    ]
    if secondary_field and secondary_field != NO_SECONDARY_RANK:
        keys.append(
            SortKey(
                accessor=filter_at_lookback(secondary_field, display_lookback),
                ascending=secondary_ascending,
            )
        )
    return keys


# ---------------------------------------------------------------------
# Section G -- Result grid formatting
# ---------------------------------------------------------------------

# (display_label, source_column, kind) -- source_column matches
# template_scanner.scan_results.results_to_dataframe()'s existing
# columns exactly; nothing here recomputes a value that module doesn't
# already provide.
DISPLAY_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("Strategy (RICs)", "rics", "rics"),
    ("Weights", "weights", "weights"),
    ("Current", "current_price", "number"),
    ("Median", "median", "number"),
    ("Robust Low", "range_low_robust", "number"),
    ("Robust High", "range_high_robust", "number"),
    ("Robust Width", "range_width_robust", "number"),
    ("Range Position", "range_position_robust", "percent"),
    ("Realized Vol (bp)", "realized_vol_bp", "number"),
    ("Efficiency Ratio", "efficiency_ratio", "number"),
    ("Norm. Crossing Freq", "normalized_crossing_frequency", "percent"),
    ("AR(1) Beta", "ar1_beta", "number"),
    ("Half-Life", "half_life", "number"),
    ("AR(1) R²", "ar1_r_squared", "number"),
)


def _is_nan(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def fmt_number(value: float, decimals: int = 4) -> str:
    """Format a scalar for display; NaN/None render as '—' -- Section
    I's rule that insufficient-history/undefined metrics render
    gracefully rather than as an exception or a raw 'nan'."""
    if _is_nan(value):
        return "—"
    if isinstance(value, float):
        return f"{value:,.{decimals}f}"
    return str(value)


def fmt_percent(value: float, decimals: int = 1) -> str:
    """Format a fractional scalar (e.g. 0.42) as a percentage string;
    NaN/None render as '—'. Not clipped to [0, 100]% -- some resolved
    metrics (e.g. range_position_robust) are deliberately unbounded."""
    if _is_nan(value):
        return "—"
    return f"{value * 100:,.{decimals}f}%"


def to_display_dataframe(results_df: pd.DataFrame) -> pd.DataFrame:
    """Curate + format template_scanner.results_to_dataframe()'s output
    into the trader-friendly subset from Section G. Row order/position
    is preserved exactly (no sorting/filtering here) so it keeps mapping
    to the same-position entry in the ranked ScanCandidateResult list
    the caller built `results_df` from.
    """
    labels = [label for label, _, _ in DISPLAY_COLUMNS]
    if results_df.empty:
        return pd.DataFrame(columns=labels)

    columns: dict[str, pd.Series] = {}
    for label, source, kind in DISPLAY_COLUMNS:
        series = results_df[source]
        if kind == "rics":
            columns[label] = series.apply(lambda v: " / ".join(v))
        elif kind == "weights":
            columns[label] = series.apply(lambda v: " / ".join(fmt_number(w, 2) for w in v))
        elif kind == "percent":
            columns[label] = series.apply(fmt_percent)
        else:
            columns[label] = series.apply(fmt_number)
    return pd.DataFrame(columns)
