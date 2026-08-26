"""
execution.py

The "Strategy Set Scan" execution path: run an already-saved
StrategySet at ONE user-chosen interval, applied to every entry for
that run only -- never persisted, never touching the saved file.

This is a SEPARATE workflow from the existing grid ("Run Scan") path,
which is completely untouched by this module: the grid keeps its
per-row Market/Interval, and mixed-interval Strategy Sets (e.g. SOFR
DAILY + SONIA HOURLY in one set) continue to load into the grid and run
via template_scanner.scanner.run_scan() exactly as before. This module
adds a second, additive way to run a saved StrategySet, for the case
where a trader wants to run an ENTIRE set uniformly at one interval
without editing every row.

Architecture (see the design discussion this module implements):
    StrategySet
        -> with_interval_override()   -- transient copy, interval only
        -> strategy_sets.expansion.expand_strategy_set()
        -> template_scanner.scanner.run_scan_on_instances()

Intermarket note (Phase 2): expand_strategy_set() and
run_scan_on_instances() both already handle a StrategySet's
intermarket_entries alongside its entries with no further change needed
in THIS module beyond the with_interval_override() fix described on
that function itself -- this module still adds no candidate-generation,
dedup, pricing, or analytics logic of its own; both flow through
verbatim, single-market and intermarket instances alike, exactly as
they already did for single-market-only StrategySets.

run_scan_on_instances() is not a parallel implementation of run_scan()
-- run_scan() itself calls it internally after its own candidate-
generation step (see template_scanner/scanner.py). Reusing it here
therefore reuses run_scan()'s real pricing, MarketDataUnavailableError
skip-handling, and Module 4A/4B analytics verbatim -- nothing about
error handling, multi-market support, or result shape is reimplemented.

Why the override happens BEFORE expand_strategy_set(), not after: expa
nd_strategy_set() calls template_scanner.universe.dedupe_candidates(),
which keys deduplication on (market_key, rics, weights, interval,
price_field) -- see universe.py's _candidate_identity(). If two entries
originally had different intervals and the override were applied AFTER
expansion/dedup, they could become interval-identical post-override
without ever having been deduplicated against each other, double-
counting a candidate in the results. Overriding first means dedup runs
on the already-uniform interval, so it dedupes correctly.

Why the override is a transient StrategySet copy rather than a new
parameter on expand_strategy_set(): the same effect is achievable
entirely from this calling layer via dataclasses.replace() (already an
established pattern in this codebase -- see strategy_sets/
repository.py's rename()/duplicate()), so strategy_sets/expansion.py, a
completed and tested module, needs no change and its existing test
suite (including the multimarket regression) is provably unaffected.

ScanRequest compatibility note: run_scan_on_instances() returns only a
ScanReport, but ui/chart_view.py's render_chart()/get_selected_history()
read scan_request.price_start/.price_end/.lookbacks directly out of
session state (see ui/state.py's store_scan_result(request, report,
display_lookback)). run_strategy_set() therefore also builds and
returns a ScanRequest -- constructed from the overridden entries'
definitions and the same call-time window/lookback/percentile
arguments -- purely so the existing results/chart UI keeps working
unmodified. Its `.definitions` field does not literally drive candidate
generation (expand_strategy_set() does that, per-entry) but nothing
downstream reads it for that purpose; only price_start/price_end/
lookbacks are ever read off a stored ScanRequest by the UI.
"""

from __future__ import annotations

from dataclasses import replace

from core.config import BarInterval
from core.utils import DateLike, get_logger

from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import StrategySet

from template_scanner.scanner import ScanReport, ScanRequest, run_scan_on_instances

logger = get_logger(__name__)


def with_interval_override(strategy_set: StrategySet, interval: BarInterval) -> StrategySet:
    """A new, transient StrategySet -- never saved, never written back to
    the repository -- identical to `strategy_set` except EVERY entry's
    (both `entries` and `intermarket_entries`) definition.interval is
    replaced with `interval`. Every other field (market_key/legs,
    offsets, weights, price_field, bp_per_point, expansion filters,
    enabled flag, entry/set names) is preserved unchanged.

    Bug fixed here (Phase 2 hardening pass): an earlier version of this
    function only rebuilt `strategy_set.entries`, silently leaving
    `strategy_set.intermarket_entries` at their ORIGINAL interval --
    contradicting this function's own "every entry" contract for any
    StrategySet containing intermarket entries. `intermarket_entries`
    did not exist when this function was first written; it was not
    updated when that field was added. Both collections are now
    rebuilt identically.

    Uses dataclasses.replace() on the frozen StrategySet/StrategySetEntry/
    IntermarketStrategySetEntry/StrategyDefinition/IntermarketDefinition
    dataclasses, which re-runs each object's own __post_init__
    validation for free -- an invalid override (e.g. an interval string
    that doesn't coerce to a real BarInterval) fails the same way
    constructing any StrategyDefinition/IntermarketDefinition would, not
    silently.
    """
    overridden_entries = tuple(
        replace(entry, definition=replace(entry.definition, interval=interval))
        for entry in strategy_set.entries
    )
    overridden_intermarket_entries = tuple(
        replace(entry, definition=replace(entry.definition, interval=interval))
        for entry in strategy_set.intermarket_entries
    )
    return replace(
        strategy_set,
        entries=overridden_entries,
        intermarket_entries=overridden_intermarket_entries,
    )


def run_strategy_set(
    strategy_set: StrategySet,
    interval: BarInterval,
    contract_start: DateLike,
    contract_end: DateLike,
    price_start: DateLike,
    price_end: DateLike,
    lookbacks: tuple[int, ...] = (20, 40, 60, 90, 120),
    crossing_equilibrium: float | None = None,
    crossing_threshold: float = 0.0,
    lower_percentile: float = 5.0,
    upper_percentile: float = 95.0,
    only_enabled: bool = True,
    dedupe: bool = True,
) -> tuple[ScanRequest, ScanReport]:
    """Run `strategy_set` at a single, call-time-chosen `interval`,
    applied uniformly to every entry for THIS run only -- `strategy_set`
    itself (and whatever's saved under its name in the repository, if
    anything) is never modified.

    Returns (request, report): `report` is the real ScanReport from
    run_scan_on_instances() (results + skipped candidates, unchanged
    shape); `request` is a ScanRequest built for UI/session-state
    compatibility only -- see the module docstring's "ScanRequest
    compatibility note".
    """
    overridden = with_interval_override(strategy_set, interval)

    instances = expand_strategy_set(
        overridden, contract_start, contract_end, only_enabled=only_enabled, dedupe=dedupe
    )

    report = run_scan_on_instances(
        instances,
        price_start,
        price_end,
        lookbacks=lookbacks,
        crossing_equilibrium=crossing_equilibrium,
        crossing_threshold=crossing_threshold,
        lower_percentile=lower_percentile,
        upper_percentile=upper_percentile,
    )

    request = ScanRequest(
        definitions=tuple(entry.definition for entry in overridden.entries),
        contract_start=contract_start,
        contract_end=contract_end,
        price_start=price_start,
        price_end=price_end,
        lookbacks=lookbacks,
        crossing_equilibrium=crossing_equilibrium,
        crossing_threshold=crossing_threshold,
        lower_percentile=lower_percentile,
        upper_percentile=upper_percentile,
    )

    logger.info(
        "run_strategy_set: '%s' at %s -> %d result(s), %d skipped [%s -> %s]",
        strategy_set.name, interval.value, len(report.results), len(report.skipped),
        price_start, price_end,
    )
    return request, report


__all__ = ["with_interval_override", "run_strategy_set"]
