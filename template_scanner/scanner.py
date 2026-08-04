"""
scanner.py

Module 5B orchestration: rolls one or many StrategyDefinition templates
into a deduplicated candidate universe (Module 5A, unchanged), prices
each candidate via strategy_engine (Module 3, unchanged) sharing one
leg cache across the whole scan, and measures each resulting history
via range_analytics (Module 4A/4B, unchanged).

v1 exception policy: build_history() failures are NOT caught here. The
data layer (core.downloader / database) does not currently expose a
typed exception distinguishing an "expected market-data retrieval
failure" from a genuine programming bug -- catching broadly at this
boundary risked silently converting real bugs into structured-looking
failure records, which is worse than letting the scan fail loudly. A
future, separately-scoped improvement can add per-candidate
infrastructure-failure isolation once the data layer exposes a proper
typed retrieval exception. No-data and short/partial history are NOT
exceptions -- they already flow through as empty/NaN-heavy results,
Modules 1-4's existing, tested behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.utils import DateLike, get_logger

from range_analytics.multi_lookback import analyze_multi_lookback

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory, build_history

from template_scanner.scan_results import ScanCandidateResult
from template_scanner.universe import dedupe_candidates, generate_candidate_universe

logger = get_logger(__name__)


@dataclass(frozen=True)
class ScanRequest:
    """Everything needed to run one Module 5B scan.

    `contract_start`/`contract_end` select which rolling contract
    combinations to generate (Module 5A); `price_start`/`price_end`
    select the historical pricing window fetched for each of them
    (Module 3) -- independent windows, matching strategy_engine's own
    contract-selection-vs-price-history separation.
    """

    definitions: tuple[StrategyDefinition, ...]
    contract_start: DateLike
    contract_end: DateLike
    price_start: DateLike
    price_end: DateLike
    lookbacks: tuple[int, ...] = (20, 40, 60, 90, 120)
    max_curve_position: int | None = None
    eligible_rics: set[str] | None = None
    crossing_equilibrium: float | None = None
    crossing_threshold: float = 0.0

    def __post_init__(self) -> None:
        if not self.definitions:
            raise ValueError("definitions must be non-empty")
        start = pd.Timestamp(self.price_start)
        end = pd.Timestamp(self.price_end)
        if start > end:
            raise ValueError(f"price_start ({start}) must be <= price_end ({end})")


@dataclass(frozen=True)
class ScanReport:
    """Result of one scan: every candidate that was successfully priced
    and analyzed.

    v1 has no separate failure list -- see the module docstring for why
    a build_history() failure propagates and aborts the scan instead of
    being recorded here.
    """

    results: tuple[ScanCandidateResult, ...]


def analyze_histories(
    histories: list[StrategyHistory],
    lookbacks: tuple[int, ...],
    crossing_equilibrium: float | None = None,
    crossing_threshold: float = 0.0,
) -> ScanReport:
    """Measure a list of already-built StrategyHistory objects.

    Mode-agnostic core of Module 5B: operates purely on StrategyHistory
    with no knowledge of how those histories were generated (REAL-mode
    dated contracts today; a future candidate source that produces
    StrategyHistory objects some other way could call this directly
    without touching filtering/ranking/results).
    """
    results = []
    for history in histories:
        multi_lookback = analyze_multi_lookback(
            history,
            lookbacks=lookbacks,
            crossing_equilibrium=crossing_equilibrium,
            crossing_threshold=crossing_threshold,
        )
        definition = history.instance.definition
        results.append(
            ScanCandidateResult(
                market_key=definition.market_key,
                rics=history.instance.rics,
                weights=definition.weights,
                offsets=definition.offsets,
                interval=definition.interval,
                price_field=history.price_field,
                instance=history.instance,
                multi_lookback=multi_lookback,
            )
        )

    logger.debug(
        "analyze_histories: %d histories -> %d result(s)", len(histories), len(results)
    )
    return ScanReport(results=tuple(results))


def run_scan(request: ScanRequest) -> ScanReport:
    """Run a complete REAL-mode scan: candidate generation (Module 5A)
    -> pricing (Module 3, one leg cache shared across the whole
    universe) -> measurement (Module 4A/4B via analyze_histories).

    A build_history() failure propagates and aborts the scan -- see the
    module docstring's v1 exception policy.
    """
    candidates = generate_candidate_universe(
        list(request.definitions),
        request.contract_start,
        request.contract_end,
        max_curve_position=request.max_curve_position,
        eligible_rics=request.eligible_rics,
    )
    candidates = dedupe_candidates(candidates)

    leg_cache: dict = {}
    histories = [
        build_history(instance, request.price_start, request.price_end, leg_cache=leg_cache)
        for instance in candidates
    ]

    logger.info(
        "run_scan: %d candidate(s) after dedup -> %d history(ies) priced",
        len(candidates), len(histories),
    )

    return analyze_histories(
        histories,
        lookbacks=request.lookbacks,
        crossing_equilibrium=request.crossing_equilibrium,
        crossing_threshold=request.crossing_threshold,
    )
