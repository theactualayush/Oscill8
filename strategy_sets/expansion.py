"""
expansion.py

Expands a StrategySet into concrete strategy_engine.StrategyInstance
objects -- the only bridge between Module 7A's domain model and the
existing, unmodified scanner/pricing pipeline.

Delegates entirely to template_scanner.universe (Module 5B's own
same-market candidate rolling + deterministic deduplication) for each
enabled entry, then combines and (by default) deduplicates across the
WHOLE set. This module adds no new rolling, filtering, or dedup logic
of its own.

contract_start/contract_end are call-time arguments here, shared across
every entry in one expansion -- matching template_scanner.scanner.
ScanRequest, which likewise carries one contract window shared across
its whole list[StrategyDefinition]. A StrategySet describes WHAT to
scan; WHEN to scan it is supplied at expand/run time, exactly like a
scan. See strategy_sets/model.py's "Design correction" note for why
this replaced an earlier per-entry contract-window design.

Audit note (see the Module 7A design review): the scanner's actual
entry point today, template_scanner.scanner.run_scan(ScanRequest),
does not literally accept StrategyInstance[] as an argument -- it
takes a flat list[StrategyDefinition] plus one shared contract window
on ScanRequest, and performs exactly this same generate_candidates()+
dedupe_candidates() step internally before pricing/analyzing. This
module produces the identical StrategyInstance[] type that internal
step produces, so a saved StrategySet's expanded output is usable
anywhere a manually-built candidate list is today without any change
to scanner.py/ScanRequest -- consistent with "the scanner remains
completely unaware this module exists" and "do not duplicate existing
scanner logic". Wiring StrategySet output INTO a scan is an explicit
non-goal of this phase (see the package docstring's out-of-scope list).
"""

from __future__ import annotations

from core.utils import DateLike, get_logger

from strategy_engine.combinations import StrategyInstance
from template_scanner.universe import dedupe_candidates, generate_candidates

from strategy_sets.model import StrategySet

logger = get_logger(__name__)


def expand_strategy_set(
    strategy_set: StrategySet,
    contract_start: DateLike,
    contract_end: DateLike,
    only_enabled: bool = True,
    dedupe: bool = True,
) -> list[StrategyInstance]:
    """Roll every (by default, enabled-only) entry in `strategy_set`
    across the SAME `contract_start`/`contract_end` window and combine
    the results into one list of StrategyInstance.

    `contract_start`/`contract_end` are supplied here, not stored on
    the StrategySet or any entry, so the same saved set can be expanded
    against a different contract window every time it's used --
    exactly matching template_scanner.scanner.ScanRequest's own
    contract_start/contract_end, one shared window per call/request.

    Each entry's own `max_curve_position`/`eligible_rics`
    (StrategySetEntry.expansion) ARE still applied per entry -- those
    are strategy-shape/liquidity-dependent filters, not a calendar
    concept, so they stay independent per entry even though the window
    itself is shared.

    `only_enabled` (default True) skips any entry whose `enabled` flag
    is False -- e.g. a strategy temporarily switched off without being
    removed from the set. Set False to expand every entry regardless
    of its enabled flag.

    `dedupe` (default True) removes exact duplicate StrategyInstances
    (same market, RICs, weights, interval, price_field) that can arise
    when two entries happen to roll into the same concrete instance --
    see template_scanner.universe.dedupe_candidates. Set False to keep
    one instance per entry even if some turn out identical.

    Returns [] (not an error) if `strategy_set` has no enabled entries,
    or if the contract window doesn't contain enough listed contracts
    to fill some entry's largest offset span -- inherited unchanged
    from strategy_engine.generate_instances().
    """
    instances: list[StrategyInstance] = []
    skipped_disabled = 0
    for entry in strategy_set.entries:
        if only_enabled and not entry.enabled:
            skipped_disabled += 1
            continue

        eligible = set(entry.expansion.eligible_rics) if entry.expansion.eligible_rics else None
        instances.extend(
            generate_candidates(
                entry.definition,
                contract_start,
                contract_end,
                max_curve_position=entry.expansion.max_curve_position,
                eligible_rics=eligible,
            )
        )

    if dedupe:
        instances = dedupe_candidates(instances)

    logger.debug(
        "expand_strategy_set: '%s' (%d entrie(s), %d skipped disabled) -> %d instance(s) [%s -> %s]",
        strategy_set.name, len(strategy_set.entries), skipped_disabled, len(instances),
        contract_start, contract_end,
    )
    return instances
