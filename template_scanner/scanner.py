"""
scanner.py

Module 5B orchestration: rolls one or many StrategyDefinition templates
into a deduplicated candidate universe (Module 5A, unchanged), prices
each candidate via strategy_engine (Module 3, unchanged) sharing one
leg cache across the whole scan, and measures each resulting history
via range_analytics (Module 4A/4B, unchanged).

Exception policy (Module 5B.1): run_scan() catches exactly ONE typed
exception around build_history() -- core.downloader.
MarketDataUnavailableError, LSEG's own confirmation that a RIC has no
market data at all (never inspected here as message text; only the
typed exception is caught -- see core/downloader.py for the
classification). The affected candidate is skipped and recorded on
ScanReport.skipped; the scan continues. A RIC confirmed unavailable is
remembered for the rest of the scan (unavailable_rics) so later
candidates referencing it are skipped without another LSEG/build_
history attempt.

Every other exception -- network/session/auth/vendor errors, database
errors, programming bugs (TypeError/KeyError/...), analytics errors --
is NOT caught and propagates, aborting the scan. This is deliberately
narrow: it must never grow into a general failure bucket. No-data and
short/partial history (a valid RIC with nothing in the requested date
range) are NOT exceptions at all -- they already flow through as
empty/NaN-heavy results, Modules 1-4's existing, tested behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.downloader import MarketDataUnavailableError
from core.utils import DateLike, get_logger

from range_analytics.multi_lookback import analyze_multi_lookback

from strategy_engine.combinations import StrategyInstance
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
class SkippedCandidate:
    """One candidate skipped because LSEG confirmed one of its legs has
    no market data available at all (core.downloader.
    MarketDataUnavailableError). Deliberately narrow -- this is the only
    reason a candidate ever ends up here; every other failure mode
    propagates and aborts the scan instead (see the module docstring).
    """

    instance: StrategyInstance
    unavailable_ric: str
    message: str


@dataclass(frozen=True)
class ScanReport:
    """Result of one scan.

    `results` holds every candidate that was successfully priced and
    analyzed. `skipped` holds every candidate skipped because a leg was
    confirmed unavailable (see SkippedCandidate) -- never populated for
    any other reason. A scan where every candidate is affected returns
    empty `results` and populated `skipped`, not an exception.
    """

    results: tuple[ScanCandidateResult, ...]
    skipped: tuple[SkippedCandidate, ...] = ()


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

    A candidate whose leg is confirmed unavailable by LSEG (core.
    downloader.MarketDataUnavailableError) is skipped and recorded in
    the returned ScanReport.skipped; every other build_history()
    failure propagates and aborts the scan -- see the module docstring.
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
    unavailable_rics: set[str] = set()
    histories: list[StrategyHistory] = []
    skipped: list[SkippedCandidate] = []

    for instance in candidates:
        already_known = next((r for r in instance.rics if r in unavailable_rics), None)
        if already_known is not None:
            skipped.append(
                SkippedCandidate(
                    instance=instance,
                    unavailable_ric=already_known,
                    message=(
                        f"{already_known} was already confirmed unavailable "
                        "earlier in this scan"
                    ),
                )
            )
            continue

        try:
            history = build_history(
                instance, request.price_start, request.price_end, leg_cache=leg_cache
            )
        except MarketDataUnavailableError as exc:
            unavailable_rics.add(exc.ric)
            skipped.append(
                SkippedCandidate(instance=instance, unavailable_ric=exc.ric, message=exc.message)
            )
            continue

        histories.append(history)

    logger.info(
        "run_scan: %d candidate(s) after dedup -> %d history(ies) priced, %d skipped "
        "(unavailable market data)",
        len(candidates), len(histories), len(skipped),
    )

    report = analyze_histories(
        histories,
        lookbacks=request.lookbacks,
        crossing_equilibrium=request.crossing_equilibrium,
        crossing_threshold=request.crossing_threshold,
    )
    return ScanReport(results=report.results, skipped=tuple(skipped))
