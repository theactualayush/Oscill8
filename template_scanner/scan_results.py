"""
scan_results.py

Module 5B's per-candidate result type and its curated DataFrame
projection. ScanCandidateResult stays a thin identity wrapper around
the unmodified Module 3/4 objects it was built from -- no analytics
value is duplicated onto it, only referenced. results_to_dataframe()
is the only place that decides which of those values become columns
for a scanner grid; it does not compute anything new.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.config import BarInterval
from core.utils import get_logger

from range_analytics.multi_lookback import MultiLookbackAnalytics

from strategy_engine.combinations import StrategyInstance

from template_scanner.metrics import at_lookback, metric_value

logger = get_logger(__name__)

# The ten Module 4B cross-lookback stability metrics, by attribute
# prefix on MultiLookbackAnalytics (each field is "{name}_stability").
# Kept as one explicit list rather than derived via introspection so
# growth of the curated table is a visible, deliberate choice, not a
# silent side effect of Module 4B growing a field.
_STABILITY_METRICS = (
    "range_width_robust",
    "range_low_robust",
    "range_high_robust",
    "median",
    "realized_vol_bp",
    "efficiency_ratio",
    "normalized_crossing_frequency",
    "ar1_beta",
    "half_life",
    "range_to_volatility_ratio",
)

_IDENTITY_COLUMNS = [
    "market_key",
    "rics",
    "weights",
    "offsets",
    "interval",
    "price_field",
    "display_lookback",
]

# Curated, scalar subset of the display lookback's RangeAnalytics --
# deliberately not every field (window bounds, full-range low/high,
# z_score, ar1_gamma/std_error, crossing_equilibrium/threshold, ...
# stay reachable only via ScanCandidateResult.multi_lookback for a
# future detail/chart view).
_HEADLINE_COLUMNS = [
    "observation_count",
    "current_price",
    "mean",
    "median",
    "range_low_robust",
    "range_high_robust",
    "range_width_robust",
    "range_position_robust",
    "realized_vol_price",
    "realized_vol_bp",
    "efficiency_ratio",
    "raw_crossing_count",
    "hysteresis_crossing_count",
    "normalized_crossing_frequency",
    "range_to_volatility_ratio",
    "robust_to_full_width_ratio",
    "ar1_beta",
    "ar1_r_squared",
    "half_life",
]

# Two scalar summaries per stability metric (dispersion, short-vs-long
# change) -- never the tuple-valued values/pairwise_diffs/pairwise_ratios
# or the unconditionally-NaN-for-signed-metrics short_vs_long_ratio.
# Those stay reachable only via ScanCandidateResult.multi_lookback.
_STABILITY_COLUMNS = [
    column
    for metric in _STABILITY_METRICS
    for column in (f"{metric}_stability_stdev", f"{metric}_stability_short_vs_long_diff")
]

_COLUMNS = _IDENTITY_COLUMNS + _HEADLINE_COLUMNS + _STABILITY_COLUMNS


@dataclass(frozen=True)
class ScanCandidateResult:
    """One scanned candidate: identity plus its full Module 4 analytics.

    `multi_lookback` is the complete, unmodified MultiLookbackAnalytics
    -- every per_lookback RangeAnalytics and every *_stability field
    (including the tuple-valued values/pairwise_diffs/pairwise_ratios)
    stays reachable here even though results_to_dataframe() only
    surfaces a curated scalar subset as columns.
    """

    market_key: str
    rics: tuple[str, ...]
    weights: tuple[float, ...]
    offsets: tuple[int, ...]
    interval: BarInterval
    price_field: str
    instance: StrategyInstance
    multi_lookback: MultiLookbackAnalytics


def results_to_dataframe(
    results: list[ScanCandidateResult],
    display_lookback: int,
) -> pd.DataFrame:
    """Curated, scanner-grid-ready DataFrame: one row per candidate.

    `display_lookback` selects which single lookback's RangeAnalytics
    becomes the headline measurement columns. Every requested lookback's
    contribution to Module 4B's cross-lookback stability summaries is
    still represented via the *_stability_stdev / *_stability_
    short_vs_long_diff columns regardless of which lookback is selected
    for display.

    An empty `results` returns an empty DataFrame with the full curated
    column set (nothing to validate display_lookback against in that
    case).

    Raises:
        ValueError: `display_lookback` is not in some candidate's
            multi_lookback.lookbacks_requested.
    """
    if not results:
        return pd.DataFrame(columns=_COLUMNS)

    rows = []
    for result in results:
        if display_lookback not in result.multi_lookback.lookbacks_requested:
            raise ValueError(
                f"display_lookback {display_lookback} was not requested for "
                f"{result.rics}; lookbacks_requested="
                f"{result.multi_lookback.lookbacks_requested}"
            )

        headline = at_lookback(result.multi_lookback, display_lookback)

        row = {
            "market_key": result.market_key,
            "rics": result.rics,
            "weights": result.weights,
            "offsets": result.offsets,
            "interval": result.interval.value,
            "price_field": result.price_field,
            "display_lookback": display_lookback,
            "observation_count": headline.observation_count,
            "current_price": headline.current_price,
            "mean": headline.mean,
            "median": headline.median,
            "range_low_robust": headline.range_low_robust,
            "range_high_robust": headline.range_high_robust,
            "range_width_robust": headline.range_width_robust,
            "range_position_robust": headline.range_position_robust,
            "realized_vol_price": headline.realized_vol_price,
            "realized_vol_bp": headline.realized_vol_bp,
            "efficiency_ratio": headline.efficiency_ratio,
            "raw_crossing_count": headline.raw_crossing_count,
            "hysteresis_crossing_count": headline.hysteresis_crossing_count,
            "normalized_crossing_frequency": metric_value(headline, "normalized_crossing_frequency"),
            "range_to_volatility_ratio": metric_value(headline, "range_to_volatility_ratio"),
            "robust_to_full_width_ratio": metric_value(headline, "robust_to_full_width_ratio"),
            "ar1_beta": headline.ar1_beta,
            "ar1_r_squared": headline.ar1_r_squared,
            "half_life": headline.half_life,
        }

        for metric in _STABILITY_METRICS:
            stability = getattr(result.multi_lookback, f"{metric}_stability")
            row[f"{metric}_stability_stdev"] = stability.stdev
            row[f"{metric}_stability_short_vs_long_diff"] = stability.short_vs_long_diff

        rows.append(row)

    logger.debug(
        "results_to_dataframe: %d row(s), display_lookback=%d", len(rows), display_lookback
    )
    return pd.DataFrame(rows, columns=_COLUMNS)
