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
from dataclasses import dataclass, replace
from typing import Sequence

import pandas as pd

from core.config import BarInterval

from core.ric import parse_ric

from strategy_engine.definitions import StrategyDefinition

from strategy_sets.composite import (
    COMBINATION_NAME_SEPARATOR,
    CompositeCombinationName,
)

from template_scanner.filters import FilterCriterion
from template_scanner.filters import at_lookback as filter_at_lookback
from template_scanner.filters import stability as filter_stability
from template_scanner.metrics import at_lookback as metrics_at_lookback
from template_scanner.ranking import SortKey
from template_scanner.scan_results import ScanCandidateResult
from template_scanner.templates import template_from_dense_weights

_NAN = float("nan")

# What every formatter in this module renders for a missing value.
# Named because format_strategy_label() has to RECOGNISE it (a label it
# cannot decorate with a product/contract must stay exactly as
# fmt_label() produced it), not merely produce it.
_MISSING_VALUE = "—"  # em dash

# Compact, always-visible caption (spec: no large instructional
# paragraph should dominate the screen) -- the full explanation moves
# into CURVE_POSITION_HELP below, shown only as a hover tooltip.
CURVE_POSITION_HELP_COMPACT = (
    "Positions are consecutive curve offsets · Tab moves across cells · Enter moves to next row"
)

CURVE_POSITION_HELP = (
    "Columns are consecutive curve positions (contract offsets). Templates roll "
    "across the active contract universe; blank cells are ignored. Keyboard "
    "workflow: click a cell once, then type a value and press Tab to commit it "
    "and move to the next cell -- Label, Market, Interval, and every weight "
    "column all work this way, so a full row can be entered without touching "
    "the mouse again. Only press Enter on the last cell of a row, when you "
    "actually want to commit it and drop down to the next row -- pressing Enter "
    "mid-row also commits and moves down, which skips the rest of the row. "
    "Arrow keys move the selection between cells as usual when a cell isn't "
    "being edited; once you start typing, arrow keys move the text cursor "
    "inside the cell instead. Example: 1 -2 1 = first contract minus 2x second "
    "contract plus third contract."
)

PRIMARY_LOOKBACK_HELP = (
    "Headline range metrics use this horizon. Other selected lookbacks are used "
    "to measure stability."
)

UNIVERSE_HELP = (
    "Which listed contracts are eligible for strategy expansion. Oscill8 scans the currently "
    "active contract curve -- from today out to a fixed forward horizon -- automatically; there "
    "is no manual date range to set here. This does not change how much price history is "
    "fetched; see History below."
)

HISTORY_HELP = (
    "How far back market price data is retrieved for the expanded strategies -- Price History "
    "Start/End bound the date range fetched and analyzed, and default to the last six months. "
    "Combined with each strategy's own interval (DAILY/HOURLY/4H), the same date range can mean "
    "a very different number of bars: e.g. six months of DAILY data vs. six months of HOURLY "
    "data. This does not change which contracts are eligible; see Universe above."
)

PERCENTILE_RANGE_HELP = (
    "Defines the historical range used for Low, High and Position. Example: "
    "25 / 75 uses the middle 50% of observations."
)

Z_SCORE_HELP = "Current distance from the lookback mean, measured in standard deviations."

ABS_Z_SCORE_HELP = (
    "Absolute value of Z-Score -- useful for ranking dislocations regardless of direction."
)

RANGE_POSITION_HELP = (
    "Current location relative to the selected percentile range. 0% = Low, 100% = High. "
    "Values outside this range indicate the strategy is beyond the selected historical band."
)

MOVEMENT_HELP = (
    "Mean absolute bar-to-bar change, in bp -- how much the strategy typically moves per bar, "
    "regardless of direction. Not a textbook OHLC ATR (the synthetic strategy series has no "
    "economically meaningful intrabar High/Low). Independent of the selected percentile range."
)

OSCILLATION_COUNT_HELP = (
    "Number of completed Top<->Bottom traversal-and-return cycles between the selected "
    "percentile range's Low and High boundaries during the resolved lookback. Depends directly "
    "on the selected percentile range -- P5/P95 and P25/P75 can produce different counts for "
    "the same series."
)


def format_percentile(value: float) -> str:
    """Render a percentile as a compact label: whole numbers drop the
    decimal ("25"), fractional ones keep it ("12.5") -- the Streamlit
    controls default to integer-step input, but the backend/API accepts
    any float, so this must handle both without showing "25.0" for the
    common case."""
    if _is_nan(value):
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def format_percentile_range(lower_percentile: float, upper_percentile: float) -> str:
    """"P{lower}-P{upper}", e.g. "P25-P75" -- the active percentile band,
    read directly off an already-computed RangeAnalytics (never
    recomputed), so it always matches whichever band Low/High/Position
    were actually computed from."""
    return f"P{format_percentile(lower_percentile)}-P{format_percentile(upper_percentile)}"


LABEL_COLUMN = "Label"
MARKET_COLUMN = "Market"
INTERVAL_COLUMN = "Interval"


def position_column(index: int) -> str:
    """Name of the data_editor column for curve position `index` (1-based)
    -- shared by ui.controls (which builds the grid) and this module
    (which reads it back), so both sides agree on the naming without
    duplicating the format string. This is the internal DataFrame column
    key only; the column's displayed header is the bare number (see
    ui.controls' column_config) -- CURVE_POSITION_HELP explains the
    "curve position" terminology once, rather than repeating it in
    every header."""
    return f"Curve Position {index}"


def _cell_to_float(value: object) -> float:
    """Parse one grid cell into a dense-weight float.

    Position cells are Streamlit TextColumns (constrained client-side to
    a numeric-looking pattern) rather than NumberColumns, specifically
    so an unpopulated cell can render as genuinely blank -- verified
    empirically that Streamlit 1.60.0's NumberColumn renders a blank/NaN
    numeric cell as the literal text "None", regardless of dtype
    (float64 NaN, object None, or pandas' nullable Float64 + pd.NA all
    reproduce it), while TextColumn with an empty string renders
    correctly blank.

    Blank text, or an incomplete number left mid-edit (a lone "-" or
    "."), means "skip this position" -- 0.0, same as an explicitly
    typed 0.
    """
    if value is None:
        return 0.0
    if isinstance(value, float) and math.isnan(value):
        return 0.0
    text = str(value).strip()
    if not text or text in ("-", ".", "-."):
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


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
    `DataFrame.to_dict("records")`), each holding an optional "Label", an
    optional per-row "Market"/"Interval" (see below), and a value per
    entry in `position_columns`. An all-zero/blank row (an empty extra
    row in a dynamic grid) is silently skipped -- not an error. All
    strategy-shape validation (offsets/weights, market, interval,
    price_field) is delegated to template_from_dense_weights() /
    StrategyDefinition -- never duplicated here. Grid cells are
    constrained client-side to a numeric-looking pattern (see
    ui.controls' TextColumn `validate` regex), and _cell_to_float()
    treats anything that still isn't a clean number as blank/0 rather
    than raising, so the non-numeric-input error case that free-text
    ratio entry required does not apply here either.

    Per-row Market/Interval (Module 7B multi-market fix): the grid's
    "Market"/"Interval" columns let different rows belong to different
    markets/intervals in the SAME grid (needed for a Strategy Set like
    "Intermarket Churning" spanning SOFR/SONIA/CORRA -- a single global
    market_key/interval could not represent that without silently
    normalizing every row to one market on save, corrupting the saved
    set). A row's own "Market"/"Interval" values take priority when
    present and non-empty; the `market_key`/`interval` parameters are
    the fallback for rows that don't carry them (e.g. legacy callers/
    tests that only ever passed one grid-wide market_key/interval,
    which keeps this function fully backward compatible).

    Task 1 simplification note: the per-row Interval this function reads
    is still what a Strategy Set persists (see ui.strategy_set_view/
    ui.strategy_set_formatting) and still seeds a freshly-typed row, but
    it is no longer the runtime interval a scan actually executes at --
    see apply_interval_override() below, which ui.scan_view.
    handle_run_scan() applies to this function's output before running.
    Market has no equivalent override: each row's own Market always
    determines which market that leg prices against.
    """
    results: list[TemplateRowResult] = []
    for i, row in enumerate(rows):
        label = str(row.get(LABEL_COLUMN) or "").strip() or f"Strategy {i + 1}"
        dense_weights = [_cell_to_float(row.get(col)) for col in position_columns]
        if not any(w != 0 for w in dense_weights):
            continue
        row_market_key = row.get(MARKET_COLUMN) or market_key
        row_interval = row.get(INTERVAL_COLUMN) or interval
        try:
            definition = template_from_dense_weights(row_market_key, dense_weights, row_interval, price_field)
            results.append(TemplateRowResult(i, label, definition, None))
        except ValueError as exc:
            results.append(TemplateRowResult(i, label, None, str(exc)))
    return results


def apply_interval_override(
    definitions: Sequence[StrategyDefinition], interval: BarInterval
) -> list[StrategyDefinition]:
    """Force every definition to run at ONE interval -- Scan
    Configuration's own Interval selector (see ui.controls), the single
    runtime source of truth for a scan (Task 1 simplification: the grid's
    per-row Interval column, and a saved Strategy Set's persisted
    per-entry interval, no longer drive execution -- they remain only
    for what gets saved/edited). Never mutates `definitions`; each row
    keeps its own market/offsets/weights/price_field unchanged, only
    `interval` is replaced.
    """
    return [replace(d, interval=interval) for d in definitions]


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
    FilterSpec("abs_z_score_min", "Absolute Z-Score (min)", "abs_z_score", "min", ABS_Z_SCORE_HELP),
    FilterSpec(
        "oscillation_count_min", "Oscillation Count (min)", "oscillation_count", "min",
        OSCILLATION_COUNT_HELP,
    ),
    FilterSpec(
        "mean_abs_change_bp_min", "Movement, bp (min)", "mean_abs_change_bp", "min",
        MOVEMENT_HELP,
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
    ("Z-Score", "z_score"),
    ("Absolute Z-Score", "abs_z_score"),
    ("Oscillation Count", "oscillation_count"),
    ("Movement (bp)", "mean_abs_change_bp"),
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
# already provide. Curated to the trader-facing subset -- Robust Range
# Width and AR(1) Beta stay available for filtering/ranking (see
# FILTER_SPECS/RANK_METRIC_OPTIONS above) and via the selected candidate,
# but are deliberately left out of the default table to keep it compact.
DISPLAY_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("Strategy Label", "label", "strategy_label"),
    ("Ratio", "weights", "weights"),
    ("Current", "current_price", "number"),
    ("Low", "range_low_robust", "number"),
    ("Median", "median", "number"),
    ("High", "range_high_robust", "number"),
    ("Position", "range_position_robust", "percent"),
    ("Z", "z_score", "number"),
    ("|Z|", "abs_z_score", "number"),
    ("Movement", "mean_abs_change_bp", "number"),
    ("Osc", "oscillation_count", "number"),
    ("ER", "efficiency_ratio", "number"),
    ("Half-Life", "half_life", "number"),
)

# Strategy Label is the results table's ONLY identity column: the
# separate raw-RIC "Strategy" column ("SRAU27 / SRAZ27 / SRAM28 / ...")
# was removed in favour of one human-readable name per row. The full
# RIC list is still shown -- unchanged -- in the Selected Strategy
# panel below the table (see selected_strategy_summary()), so nothing
# was actually lost from the screen, only from the grid.
#
# For an ORDINARY (single-market or hand-authored intermarket)
# candidate the value is exactly ScanCandidateResult.label, unchanged:
# the actual originating Strategy Set entry name / strategy-grid row
# Label (see template_scanner.scanner.run_scan()) -- never derived or
# reconstructed from RICs or the strategy definition.
#
# For a COMPOSITE (Group A x Group B) candidate it is enriched with
# each side's own first contract -- see format_strategy_label().
STRATEGY_LABEL_COLUMN = "Strategy Label"

# Column widths passed straight to st.column_config.Column(width=...)
# in ui.results_view. Strategy Label now carries a composite's full
# "<contract> <name> - <contract> <name>" text, which needs noticeably
# more room than the numeric metric columns beside it.
RESULT_COLUMN_WIDTHS: dict[str, str] = {
    STRATEGY_LABEL_COLUMN: "large",
}


def format_contract(ric: str) -> str | None:
    """One RIC as a trader-facing "<product> <contract>" pair, e.g.
    `"SRAH27"` -> `"SRA H27"`, `"CRAU6"` -> `"CRA U6"`.

    The split point is the market's own registered
    `MarketDefinition.ric_root` length, resolved through
    `core.ric.parse_ric()` -- the repository's single, canonical RIC
    parser. This function deliberately adds NO parsing of its own (no
    regex, no fixed root length, no month-code table): a second RIC
    parser that could disagree with the real one is exactly what must
    not exist here. That also makes it work unchanged for every
    configured market -- `SRA`/`FF`/`SON`/`CRA`/`SRE`/`FEI`/`SARO3`/
    `YBA`/`EON3` -- including the 1-digit-year roots, without a
    per-market branch.

    The contract half is the ORIGINAL RIC's own remaining characters,
    sliced rather than rebuilt, so a 1-digit-year market renders what
    its RIC actually says (`CRA U6`) instead of a normalised year this
    function invented.

    Returns None -- never a guess, never a partial string -- when the
    RIC cannot be parsed against any known market root. Callers fall
    back to the unadorned label.
    """
    try:
        parsed = parse_ric(ric)
    except (ValueError, KeyError):
        return None

    root = parsed.market.ric_root
    remainder = ric[len(root):]
    if not remainder:
        return None
    return f"{root} {remainder}"


def format_strategy_label(label: object, rics: Sequence[str]) -> str:
    """The results grid's Strategy Label cell for one candidate.

    The grid's only identity column, so it has to answer "which product,
    which contract, which strategy?" on its own:

        ordinary:   "<product> <contract> <strategy name>"
        composite:  "<product> <contract> <A name>"
                    " - <product> <contract> <B name>"

    e.g. `"SRA H27 3M Fly 2"`, `"CRA U6 3m sprd"`,
    `"SRA H27 3M Fly - SRA H27 12M Sprd"`.

    PRODUCT AND CONTRACT COME FROM THE ACTUAL RICs, never from the
    strategy name and never from the old raw-RIC "Strategy" display
    string (which no longer exists -- see DISPLAY_COLUMNS). `rics` is
    the instance's own leg tuple, used exactly as it carries it:
    `rics[0]` is the first/nearest leg. Nothing here sorts, reorders,
    re-derives or normalises a RIC; format_contract() only splits one
    at its registered root boundary.

    COMPOSITE SPLIT. A composite's label arrives as a
    strategy_sets.composite.CompositeCombinationName (a `str` subclass
    -- see that class for why the structured provenance rides along
    with the name rather than being re-derived here), carrying each
    side's own entry name plus Group A's leg count. Because
    compose_definition() lays a combination's legs out as A's legs
    followed by B's negated legs, and generate_intermarket_instances()
    builds `rics` positionally from that same leg tuple, `rics[0]` is
    A's first/nearest contract and `rics[leg_count_a]` is B's.

    Group A is ALWAYS on the left: the two sides are read from their
    own separately-named fields, never by splitting the name string
    apart, so the A/B orientation constructed upstream can never be
    inverted or alphabetically reordered here.

    FALLBACKS -- preserve, never guess. The plain fmt_label() label is
    returned unchanged when:
      * there is no name to decorate (`None`/blank renders as the same
        "—" every other missing value does);
      * `rics` is empty, or too short for a composite's recorded leg
        split (structurally impossible for a real instance, since every
        leg produces exactly one RIC);
      * the relevant RIC does not parse against any known market root
        -- for a composite, if EITHER side fails to resolve, both sides
        stay undecorated, so one row can never show a product for one
        half and not the other.
    """
    name = fmt_label(label)
    rics = tuple(rics)

    if isinstance(label, CompositeCombinationName):
        if len(rics) <= label.leg_count_a:
            return name

        contract_a = format_contract(rics[0])
        contract_b = format_contract(rics[label.leg_count_a])
        if contract_a is None or contract_b is None:
            return name

        return (
            f"{contract_a} {label.name_a}"
            f"{COMBINATION_NAME_SEPARATOR}"
            f"{contract_b} {label.name_b}"
        )

    if name == _MISSING_VALUE or not rics:
        return name

    contract = format_contract(rics[0])
    if contract is None:
        return name
    return f"{contract} {name}"

# Cross Frequency is deliberately NOT in the default DISPLAY_COLUMNS --
# Movement and Osc took its place to keep the default grid compact (see
# the Tradability Analytics design review). It remains fully available
# via FILTER_SPECS/RANK_METRIC_OPTIONS and template_scanner's canonical
# metric resolution; only the default visible table dropped it.

# Tooltip text for the result-grid column headers with a non-obvious
# meaning -- shown via st.column_config's `help=`. Same wording as the
# matching FilterSpec.help_text where one exists.
RESULT_COLUMN_HELP: dict[str, str] = {
    "Low": "Lower bound of the selected percentile range.",
    "High": "Upper bound of the selected percentile range.",
    "Position": RANGE_POSITION_HELP,
    "Z": Z_SCORE_HELP,
    "|Z|": ABS_Z_SCORE_HELP,
    "Movement": MOVEMENT_HELP,
    "Osc": OSCILLATION_COUNT_HELP,
    "ER": "Lower means the strategy moved less directionally and oscillated more.",
    "Half-Life": "Estimated time for a deviation from equilibrium to decay by half.",
}

RANK_COLUMN = "Rank"

# Every column a user can independently show/hide in the result grid --
# Rank plus every DISPLAY_COLUMNS label, in the same order the table
# already renders them. OPTIONAL_COLUMN_LABELS/DEFAULT_VISIBLE_COLUMNS
# are the single source of truth for the "Columns ▾" popover (see
# ui.results_view) -- adding a future optional column means adding it to
# DISPLAY_COLUMNS above, nothing else.
OPTIONAL_COLUMN_LABELS: tuple[str, ...] = (RANK_COLUMN,) + tuple(
    label for label, _, _ in DISPLAY_COLUMNS
)

# Every column is visible by default. Strategy Label used to start
# hidden, back when the raw-RIC "Strategy" column was the table's
# identity column; now that Strategy Label IS that column, hiding it by
# default would leave a nameless table. Each column remains
# independently hideable via the Columns popover.
DEFAULT_VISIBLE_COLUMNS: tuple[str, ...] = OPTIONAL_COLUMN_LABELS


def apply_column_selection(display_df: pd.DataFrame, selected_labels: Sequence[str]) -> pd.DataFrame:
    """Project `display_df` down to the user-selected columns.

    Pure display-layer filtering: never removes/recomputes the
    underlying metric on ScanCandidateResult, only whether it's shown.
    Preserves `display_df`'s own existing column order (not the order
    columns were selected in), so toggling one column never reshuffles
    the rest. An empty `selected_labels` returns a same-row-count,
    zero-column DataFrame -- the user's own explicit choice, not
    special-cased.
    """
    selected = set(selected_labels)
    keep = [c for c in display_df.columns if c in selected]
    return display_df[keep]


def available_markets(results: Sequence[ScanCandidateResult]) -> list[str]:
    """Distinct `market_key` values represented in `results`, sorted --
    the Market filter's options (see ui.results_view), always derived
    fresh from the current scan's own results, never a fixed/global
    market list."""
    return sorted({result.market_key for result in results})


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


def fmt_label(value: object) -> str:
    """Format an optional caller-supplied label (e.g.
    ScanCandidateResult.label); None/blank renders as _MISSING_VALUE,
    matching fmt_number/fmt_percent's own missing-value convention."""
    if value is None:
        return _MISSING_VALUE
    text = str(value).strip()
    return text if text else _MISSING_VALUE


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
        if kind == "strategy_label":
            # The only column built from two source columns: a
            # composite's label needs its candidate's own `rics` to
            # name each side's first contract (see
            # format_strategy_label). Row order/index is preserved --
            # both series come from the same DataFrame.
            columns[label] = pd.Series(
                [
                    format_strategy_label(value, ric_tuple)
                    for value, ric_tuple in zip(series, results_df["rics"])
                ],
                index=results_df.index,
            )
        elif kind == "weights":
            columns[label] = series.apply(lambda v: " / ".join(fmt_number(w, 2) for w in v))
        elif kind == "percent":
            columns[label] = series.apply(fmt_percent)
        elif kind == "text":
            columns[label] = series.apply(fmt_label)
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
    identity, ratio, interval, current level, mean, median, robust low/
    high, range position, z-score, efficiency ratio, movement,
    oscillation count, and the active percentile-range label -- all
    read directly off the candidate's already-computed RangeAnalytics
    at `display_lookback`, nothing recomputed. The percentile-range
    label stays alongside Oscillations since the count is only
    meaningful together with the boundaries it was computed against.
    """
    analytics = metrics_at_lookback(candidate.multi_lookback, display_lookback)
    return {
        "rics": " / ".join(candidate.rics),
        "weights": " / ".join(fmt_number(w, 2) for w in candidate.weights),
        "interval": candidate.interval.value,
        "current": fmt_number(analytics.current_price),
        "mean": fmt_number(analytics.mean),
        "median": fmt_number(analytics.median),
        "robust_low": fmt_number(analytics.range_low_robust),
        "robust_high": fmt_number(analytics.range_high_robust),
        "robust_range": f"{fmt_number(analytics.range_low_robust)} – {fmt_number(analytics.range_high_robust)}",
        "position": fmt_percent(analytics.range_position_robust),
        "z_score": fmt_number(analytics.z_score, 2),
        "efficiency_ratio": fmt_number(analytics.efficiency_ratio),
        "movement": fmt_number(analytics.mean_abs_change_bp, 2),
        "oscillations": fmt_number(analytics.oscillation_count, 0),
        "percentile_range_label": format_percentile_range(
            analytics.lower_percentile, analytics.upper_percentile
        ),
    }
