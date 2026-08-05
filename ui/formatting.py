"""
formatting.py

Pure helper functions for Module 6A's Streamlit UI: translating
strategy-grid rows (curve position -> ratio) into
strategy_engine.StrategyDefinition via the existing
template_scanner.template_from_dense_weights(), constructing filter/
ranking accessors from template_scanner's existing canonical metric
resolution (FilterCriterion / SortKey / at_lookback / stability), and
formatting values for the result grid and the selected-strategy summary.

No analytics, filtering, or ranking value is computed here -- every
number and every pass/fail or sort decision comes from strategy_engine /
range_analytics / template_scanner, unmodified. This module only shapes
UI input into their existing call shapes and formats their output for
display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition

from template_scanner.filters import FilterCriterion
from template_scanner.filters import at_lookback as filter_at_lookback
from template_scanner.filters import stability as filter_stability
from template_scanner.metrics import at_lookback as metrics_at_lookback
from template_scanner.ranking import SortKey
from template_scanner.scan_results import ScanCandidateResult
from template_scanner.templates import template_from_dense_weights

_NAN = float("nan")

CURVE_POSITION_HELP = (
    "The ratio is rolled across all eligible contracts in the selected universe. "
    "Use 0 to skip a curve position."
)


def position_column(index: int) -> str:
    """Name of the data_editor column for curve position `index` (1-based)
    -- shared by ui.controls (which builds the grid) and this module
    (which reads it back), so both sides agree on the naming without
    duplicating the format string."""
    return f"Curve Position {index}"


def _cell_to_float(value: object) -> float:
    """A blank/cleared data_editor cell arrives as None or NaN, not 0 --
    treat both as 0 (an explicitly skipped curve position), matching the
    grid's stated convention."""
    if value is None:
        return 0.0
    if isinstance(value, float) and math.isnan(value):
        return 0.0
    return float(value)


@dataclass(frozen=True)
class TemplateRowResult:
    """One strategy-grid row's translation outcome -- either a
    StrategyDefinition or a user-facing error message, never both."""

    row_index: int
    label: str
    definition: StrategyDefinition | None
    error: str | None


def build_definitions_from_grid(
    rows: Sequence[dict],
    position_columns: Sequence[str],
    market_key: str,
    interval: BarInterval,
    price_field: str = "Close",
) -> list[TemplateRowResult]:
    """Translate strategy-grid rows into StrategyDefinitions.

    `rows` is one dict per grid row (as returned by
    `DataFrame.to_dict("records")`), each holding an optional "Label" and
    a value per entry in `position_columns`. An all-zero/blank row (an
    empty extra row in a dynamic grid) is silently skipped -- not an
    error. All strategy-shape validation (offsets/weights, market,
    interval, price_field) is delegated to template_from_dense_weights()
    / StrategyDefinition -- never duplicated here. Because grid cells are
    numeric-only (Streamlit's NumberColumn), the non-numeric-input error
    case that free-text ratio entry required no longer applies here.
    """
    results: list[TemplateRowResult] = []
    for i, row in enumerate(rows):
        label = str(row.get("Label") or "").strip() or f"Strategy {i + 1}"
        dense_weights = [_cell_to_float(row.get(col)) for col in position_columns]
        if not any(w != 0 for w in dense_weights):
            continue
        try:
            definition = template_from_dense_weights(market_key, dense_weights, interval, price_field)
            results.append(TemplateRowResult(i, label, definition, None))
        except ValueError as exc:
            results.append(TemplateRowResult(i, label, None, str(exc)))
    return results


# ---------------------------------------------------------------------
# Range-Bound Filters
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class FilterSpec:
    """One available filter: a label, the metric it resolves (via
    template_scanner's canonical metric_value()/at_lookback()), which
    bound ("min" or "max") the user-entered threshold applies to, and a
    short trader-oriented help string shown as a tooltip."""

    key: str
    label: str
    field: str
    bound: str  # "min" or "max"
    help_text: str


FILTER_SPECS: tuple[FilterSpec, ...] = (
    FilterSpec(
        "efficiency_ratio_max", "Efficiency Ratio (max)", "efficiency_ratio", "max",
        "Lower means the strategy moved less directionally and oscillated more.",
    ),
    FilterSpec(
        "normalized_crossing_frequency_min", "Normalized Crossing Frequency (min)",
        "normalized_crossing_frequency", "min",
        "Higher means the strategy crossed its equilibrium more frequently.",
    ),
    FilterSpec(
        "ar1_beta_max", "AR(1) Beta (max)", "ar1_beta", "max",
        "Lower generally indicates faster mean reversion.",
    ),
    FilterSpec(
        "half_life_max", "Half-Life (max)", "half_life", "max",
        "Estimated time for a deviation from equilibrium to decay by half.",
    ),
    FilterSpec(
        "range_width_robust_max", "Robust Range Width (max)", "range_width_robust", "max",
        "Width of the strategy's typical historical trading range.",
    ),
    FilterSpec(
        "ar1_r_squared_min", "AR(1) R² (min)", "ar1_r_squared", "min",
        "How well the simple mean-reversion model explains historical movements.",
    ),
)

# One stability filter (Module 4B): bounds how much a candidate's
# efficiency ratio moves across the requested lookbacks.
STABILITY_FILTER_SPEC = FilterSpec(
    "efficiency_ratio_stability_stdev_max",
    "Efficiency Ratio Stability, stdev (max)",
    "efficiency_ratio",
    "max",
    "How consistent this metric has been across the requested lookback windows.",
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
# Ranking
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
    """Build the SortKey list for rank_results() from the primary
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


def format_ranked_by(rank_state: dict) -> str:
    """Render the current ranking as a trader-readable label, e.g.
    "Ranked by: Efficiency Ratio ↑ · Lower is better, then AR(1) Beta ↓".

    "Lower/Higher is better" follows mechanically from the sort
    direction (ascending puts the smallest value at Rank #1) -- this is
    a property of the sort itself, not a new domain judgment about any
    particular metric.
    """
    label_by_field = {field: label for label, field in RANK_METRIC_OPTIONS}

    def _describe(field: str, ascending: bool) -> str:
        label = label_by_field.get(field, field)
        arrow = "↑" if ascending else "↓"
        return f"{label} {arrow}"

    primary = _describe(rank_state["primary_field"], rank_state["primary_ascending"])
    better = "Lower is better" if rank_state["primary_ascending"] else "Higher is better"
    text = f"Ranked by: {primary} · {better}"

    secondary_field = rank_state.get("secondary_field")
    if secondary_field and secondary_field != NO_SECONDARY_RANK:
        secondary = _describe(secondary_field, rank_state["secondary_ascending"])
        text += f", then {secondary}"
    return text


# ---------------------------------------------------------------------
# Result grid formatting
# ---------------------------------------------------------------------

# (display_label, source_column, kind) -- source_column matches
# template_scanner.scan_results.results_to_dataframe()'s existing
# columns exactly; nothing here recomputes a value that module doesn't
# already provide. Curated to the trader-facing subset -- Robust Low/
# High, Realized Vol (bp), and AR(1) R² stay reachable via the selected
# candidate rather than as primary-table columns.
DISPLAY_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("Strategy", "rics", "rics"),
    ("Ratio", "weights", "weights"),
    ("Current", "current_price", "number"),
    ("Median", "median", "number"),
    ("Pos", "range_position_robust", "percent"),
    ("Width", "range_width_robust", "number"),
    ("ER", "efficiency_ratio", "number"),
    ("Cross Freq", "normalized_crossing_frequency", "percent"),
    ("Half-Life", "half_life", "number"),
    ("AR1 β", "ar1_beta", "number"),
)

# Tooltip text for the result-grid column headers with a non-obvious
# meaning -- shown via st.column_config's `help=`. Same wording as the
# matching FilterSpec.help_text where one exists.
RESULT_COLUMN_HELP: dict[str, str] = {
    "Pos": (
        "Current level's position within the estimated robust range. "
        "0% is near the lower bound; 100% is near the upper bound."
    ),
    "Width": "Width of the strategy's typical historical trading range.",
    "ER": "Lower means the strategy moved less directionally and oscillated more.",
    "Cross Freq": "Higher means the strategy crossed its equilibrium more frequently.",
    "Half-Life": "Estimated time for a deviation from equilibrium to decay by half.",
    "AR1 β": "Lower generally indicates faster mean reversion.",
}

RANK_COLUMN = "Rank"


def _is_nan(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def fmt_number(value: float, decimals: int = 4) -> str:
    """Format a scalar for display; NaN/None render as '—' rather than
    raising or showing a raw 'nan' -- insufficient-history/undefined
    metrics render gracefully."""
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
    into the trader-friendly subset above. Row order/position is
    preserved exactly (no sorting/filtering here) so it keeps mapping to
    the same-position entry in the ranked ScanCandidateResult list the
    caller built `results_df` from.
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


def add_rank_column(display_df: pd.DataFrame) -> pd.DataFrame:
    """Prepend a `Rank` column (#1, #2, ...) reflecting the DataFrame's
    current row order. Purely a display-layer position label over an
    already-`rank_results()`-sorted list -- never a new metric."""
    ranked = display_df.copy()
    ranked.insert(0, RANK_COLUMN, [f"#{i + 1}" for i in range(len(ranked))])
    return ranked


def selected_strategy_summary(candidate: ScanCandidateResult, display_lookback: int) -> dict[str, str]:
    """Format the headline stats for the Selected Strategy panel:
    identity, ratio, interval, current level, robust range (combined
    low-high), median, range position, and efficiency ratio -- all read
    directly off the candidate's already-computed RangeAnalytics at
    `display_lookback`, nothing recomputed.
    """
    analytics = metrics_at_lookback(candidate.multi_lookback, display_lookback)
    return {
        "rics": " / ".join(candidate.rics),
        "weights": " / ".join(fmt_number(w, 2) for w in candidate.weights),
        "interval": candidate.interval.value,
        "current": fmt_number(analytics.current_price),
        "robust_range": f"{fmt_number(analytics.range_low_robust)} – {fmt_number(analytics.range_high_robust)}",
        "median": fmt_number(analytics.median),
        "position": fmt_percent(analytics.range_position_robust),
        "efficiency_ratio": fmt_number(analytics.efficiency_ratio),
    }
