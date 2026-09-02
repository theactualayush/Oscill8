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

from range_analytics.location import validate_percentiles
from range_analytics.multi_lookback import analyze_multi_lookback

from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_combinations import IntermarketStrategyInstance
from strategy_engine.intermarket_definitions import resolve_display_market_key, resolve_display_offsets
from strategy_engine.pricing import StrategyHistory, build_history, prewarm_leg_cache

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
    lower_percentile: float = 5.0
    upper_percentile: float = 95.0

    def __post_init__(self) -> None:
        if not self.definitions:
            raise ValueError("definitions must be non-empty")
        start = pd.Timestamp(self.price_start)
        end = pd.Timestamp(self.price_end)
        if start > end:
            raise ValueError(f"price_start ({start}) must be <= price_end ({end})")
        validate_percentiles(self.lower_percentile, self.upper_percentile)


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
    lower_percentile: float = 5.0,
    upper_percentile: float = 95.0,
    labels: list[str | None] | None = None,
) -> ScanReport:
    """Measure a list of already-built StrategyHistory objects.

    Mode-agnostic core of Module 5B: operates purely on StrategyHistory
    with no knowledge of how those histories were generated (REAL-mode
    dated contracts today; a future candidate source that produces
    StrategyHistory objects some other way could call this directly
    without touching filtering/ranking/results) -- and, since
    resolve_display_market_key()/resolve_display_offsets() dispatch by
    type, no knowledge of whether a given history's instance is a
    single-market StrategyInstance or an intermarket
    IntermarketStrategyInstance either. Both flow through this exact
    same loop into one ScanReport.

    `labels`, if given, must be the same length as `histories` and is
    zipped positionally onto each resulting ScanCandidateResult.label
    -- purely descriptive caller-supplied metadata (e.g. a UI strategy-
    grid row's name), never computed or inferred here. `None` (the
    default) leaves every result's `label` as `None`, unchanged from
    before this parameter existed.
    """
    if labels is not None and len(labels) != len(histories):
        raise ValueError(
            f"labels length ({len(labels)}) must match histories length ({len(histories)})"
        )

    results = []
    for i, history in enumerate(histories):
        multi_lookback = analyze_multi_lookback(
            history,
            lookbacks=lookbacks,
            crossing_equilibrium=crossing_equilibrium,
            crossing_threshold=crossing_threshold,
            lower_percentile=lower_percentile,
            upper_percentile=upper_percentile,
        )
        definition = history.instance.definition
        results.append(
            ScanCandidateResult(
                market_key=resolve_display_market_key(definition),
                rics=history.instance.rics,
                weights=definition.weights,
                offsets=resolve_display_offsets(definition),
                interval=definition.interval,
                price_field=history.price_field,
                instance=history.instance,
                multi_lookback=multi_lookback,
                label=labels[i] if labels is not None else None,
            )
        )

    logger.debug(
        "analyze_histories: %d histories -> %d result(s)", len(histories), len(results)
    )
    return ScanReport(results=tuple(results))


def run_scan_on_instances(
    instances: list[StrategyInstance | IntermarketStrategyInstance],
    price_start: DateLike,
    price_end: DateLike,
    lookbacks: tuple[int, ...] = (20, 40, 60, 90, 120),
    crossing_equilibrium: float | None = None,
    crossing_threshold: float = 0.0,
    lower_percentile: float = 5.0,
    upper_percentile: float = 95.0,
    labels_by_definition_id: dict[int, str] | None = None,
) -> ScanReport:
    """Price and measure an already-built candidate list -- exactly the
    build_history()+skip-handling loop run_scan() runs internally (see
    the module docstring's exception policy), exposed directly for a
    caller that already has StrategyInstance objects and doesn't need
    Module 5A's template-rolling/generation step run again.

    This is the "instances-in, ScanReport-out" entry point noted as
    missing in the project roadmap: it lets any candidate source other
    than ScanRequest's own definitions+contract-window rolling reuse
    this exact pricing/skip/analyze pipeline without duplicating it --
    this module has no knowledge of what that other source is or how
    it built its candidate list. run_scan() itself is refactored below
    to call this function too, so there is only one implementation of
    the loop, not two.

    `instances` may freely mix single-market StrategyInstance and
    intermarket IntermarketStrategyInstance objects in one call -- this
    function (and build_history()/prewarm_leg_cache() beneath it) reads
    only `.rics` off each instance, never `.definition.market_key`, so
    it is already agnostic to which kind of instance it's given and to
    whatever caller assembled the mixed list.

    leg_cache is pre-warmed via strategy_engine.pricing.prewarm_leg_cache()
    -- batches every distinct QuantHub-routed leg required by `instances`
    into as few HTTP requests as possible (see database.get_history_batch)
    before the per-instance loop below begins, instead of each instance
    triggering its own lazy, one-RIC-at-a-time fetch. This is purely a
    request-volume optimization: the per-candidate MarketDataUnavailableError
    skip-and-continue policy below is completely unaffected, since any leg
    prewarm_leg_cache() couldn't resolve simply falls back to the existing
    lazy build_history()/get_history() path and is caught here exactly as
    before (see prewarm_leg_cache's own docstring).

    `labels_by_definition_id`, if given, maps `id(instance.definition)` ->
    a caller-facing label -- e.g. a UI strategy-grid row's Label/Strategy
    Set entry name (see ui.scan_view.handle_run_scan()). One definition
    object is shared by reference across every rolled instance generated
    from it (strategy_engine.combinations.generate_instances()/
    template_scanner.universe never clone or replace it), so this single
    id-keyed lookup correctly labels every candidate a given grid row
    produced, however many contracts it rolled into. Purely descriptive:
    never affects which candidates are generated, priced, skipped, or
    deduplicated -- only ScanCandidateResult.label on the survivors.
    """
    leg_cache = prewarm_leg_cache(instances, price_start, price_end)
    unavailable_rics: set[str] = set()
    histories: list[StrategyHistory] = []
    labels: list[str | None] = []
    skipped: list[SkippedCandidate] = []

    for instance in instances:
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
            history = build_history(instance, price_start, price_end, leg_cache=leg_cache)
        except MarketDataUnavailableError as exc:
            unavailable_rics.add(exc.ric)
            skipped.append(
                SkippedCandidate(instance=instance, unavailable_ric=exc.ric, message=exc.message)
            )
            continue

        histories.append(history)
        labels.append(
            labels_by_definition_id.get(id(instance.definition))
            if labels_by_definition_id is not None
            else None
        )

    logger.info(
        "run_scan_on_instances: %d candidate(s) -> %d history(ies) priced, %d skipped "
        "(unavailable market data)",
        len(instances), len(histories), len(skipped),
    )

    report = analyze_histories(
        histories,
        lookbacks=lookbacks,
        crossing_equilibrium=crossing_equilibrium,
        crossing_threshold=crossing_threshold,
        lower_percentile=lower_percentile,
        upper_percentile=upper_percentile,
        labels=labels,
    )
    return ScanReport(results=report.results, skipped=tuple(skipped))


def run_scan(
    request: ScanRequest, labels_by_definition_id: dict[int, str] | None = None
) -> ScanReport:
    """Run a complete REAL-mode scan: candidate generation (Module 5A)
    -> pricing (Module 3, one leg cache shared across the whole
    universe) -> measurement (Module 4A/4B via analyze_histories).

    A candidate whose leg is confirmed unavailable by LSEG (core.
    downloader.MarketDataUnavailableError) is skipped and recorded in
    the returned ScanReport.skipped; every other build_history()
    failure propagates and aborts the scan -- see the module docstring.

    `labels_by_definition_id` is passed straight through to
    run_scan_on_instances() -- see that function's own docstring. Purely
    optional, caller-facing metadata; does not affect candidate
    generation, pricing, or deduplication.
    """
    candidates = generate_candidate_universe(
        list(request.definitions),
        request.contract_start,
        request.contract_end,
        max_curve_position=request.max_curve_position,
        eligible_rics=request.eligible_rics,
    )
    candidates = dedupe_candidates(candidates)

    return run_scan_on_instances(
        candidates,
        request.price_start,
        request.price_end,
        lookbacks=request.lookbacks,
        crossing_equilibrium=request.crossing_equilibrium,
        crossing_threshold=request.crossing_threshold,
        lower_percentile=request.lower_percentile,
        upper_percentile=request.upper_percentile,
        labels_by_definition_id=labels_by_definition_id,
    )
