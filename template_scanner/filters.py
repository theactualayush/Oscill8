"""
filters.py

User-configurable, optional filtering over ScanCandidateResult, kept
deliberately separate from measurement (range_analytics) and from
ranking (ranking.py). No criterion is ever applied unless the caller
supplies it -- an empty criteria list returns every input candidate
unchanged. No threshold is hard-coded anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import pandas as pd

from core.utils import get_logger

from template_scanner.metrics import at_lookback as _range_analytics_at_lookback
from template_scanner.scan_results import ScanCandidateResult

logger = get_logger(__name__)


@dataclass(frozen=True)
class FilterCriterion:
    """One optional filtering criterion against a single scalar metric.

    A candidate passes iff its accessor value is not NaN and falls
    within [min_value, max_value] -- both bounds inclusive; either or
    both may be left None to leave that side unconstrained.
    """

    name: str
    accessor: Callable[[ScanCandidateResult], float]
    min_value: float | None = None
    max_value: float | None = None

    def passes(self, result: ScanCandidateResult) -> bool:
        value = self.accessor(result)
        if pd.isna(value):
            return False
        if self.min_value is not None and value < self.min_value:
            return False
        if self.max_value is not None and value > self.max_value:
            return False
        return True


def apply_filters(
    results: Sequence[ScanCandidateResult],
    criteria: Sequence[FilterCriterion] = (),
) -> list[ScanCandidateResult]:
    """Keep only candidates passing every criterion (AND).

    No criteria supplied returns every input candidate, unfiltered --
    this is the only way a candidate is ever excluded, never a default
    threshold.
    """
    if not criteria:
        return list(results)
    filtered = [r for r in results if all(c.passes(r) for c in criteria)]
    logger.debug(
        "apply_filters: %d -> %d candidate(s) after %d criterion(a)",
        len(results), len(filtered), len(criteria),
    )
    return filtered


def at_lookback(field: str, lookback: int) -> Callable[[ScanCandidateResult], float]:
    """Accessor factory: reads `field` off the RangeAnalytics computed
    at exactly `lookback` (see template_scanner.metrics.at_lookback)."""

    def accessor(result: ScanCandidateResult) -> float:
        analytics = _range_analytics_at_lookback(result.multi_lookback, lookback)
        return getattr(analytics, field)

    return accessor


def stability(metric: str, field: str) -> Callable[[ScanCandidateResult], float]:
    """Accessor factory: reads `field` off the `{metric}_stability`
    LookbackStability, e.g. stability("efficiency_ratio", "stdev")."""

    def accessor(result: ScanCandidateResult) -> float:
        stability_obj = getattr(result.multi_lookback, f"{metric}_stability")
        return getattr(stability_obj, field)

    return accessor
