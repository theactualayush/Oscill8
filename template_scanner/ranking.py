"""
ranking.py

Transparent, multi-key sorting over ScanCandidateResult, kept
deliberately separate from filtering (filters.py) and from measurement
(range_analytics). No composite/opaque score is computed anywhere in
this module -- every key is one caller-supplied accessor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import pandas as pd

from core.utils import get_logger

from template_scanner.scan_results import ScanCandidateResult

logger = get_logger(__name__)


@dataclass(frozen=True)
class SortKey:
    """One transparent sort key: an accessor plus a direction. NaN
    values always sort last, regardless of `ascending`."""

    accessor: Callable[[ScanCandidateResult], float]
    ascending: bool = True


def rank_results(
    results: Sequence[ScanCandidateResult],
    keys: Sequence[SortKey],
) -> list[ScanCandidateResult]:
    """Sort candidates by one or more transparent metric keys, in
    priority order (first key is primary, subsequent keys break ties).
    NaN sorts last on every key, regardless of that key's direction.

    No keys supplied returns the input candidates in their original
    order, unchanged.
    """
    results = list(results)
    if not keys:
        return results

    columns = {f"key_{i}": [key.accessor(r) for r in results] for i, key in enumerate(keys)}
    order = pd.DataFrame(columns, index=range(len(results)))
    ranked_index = order.sort_values(
        by=list(order.columns),
        ascending=[key.ascending for key in keys],
        na_position="last",
        kind="mergesort",  # stable, so equal keys preserve input order
    ).index

    ranked = [results[i] for i in ranked_index]
    logger.debug("rank_results: sorted %d candidate(s) by %d key(s)", len(ranked), len(keys))
    return ranked
