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

Two separate entry points into the scanner (multi-market audit note):
Oscill8 currently has two independent paths that end up building the
StrategyInstance/StrategyDefinition objects the scanner/pricing layer
actually consumes:

  MANUAL GRID:  ui grid rows -> ui.formatting.build_definitions_from_
                grid() -> list[StrategyDefinition] -> ScanRequest ->
                template_scanner.scanner.run_scan()
  STRATEGY SET: StrategySet -> expand_strategy_set() (this module) ->
                list[StrategyInstance] -> template_scanner.scanner.
                run_scan_on_instances()

Both are real, currently-tested paths, not a WIP/replaced-by
relationship -- the UI's "Run Scan" button drives the manual grid path
only; expand_strategy_set() is not wired into it (see the audit note
above). Whichever path a caller uses, market and interval always come
from each StrategyDefinition/StrategyInstance individually -- never
from a global scanner-level market/interval, because none exists:
template_scanner.scanner.ScanRequest has no market/interval field at
all. Concretely, this means the manual grid's top-level ScanSetup
market/interval controls are never an override of anything -- for the
manual path they are only fallback/default values `build_definitions_
from_grid()` applies to a genuinely blank grid row (see that function's
own docstring); for the Strategy Set path they are not consulted at
all, since expand_strategy_set() doesn't take a market/interval
argument in the first place. See tests/test_strategy_sets_multimarket_
pipeline.py and tests/test_multimarket_cache_key_independence.py for
the regression coverage proving both paths keep every entry's market,
interval, RICs, and cached history fully independent of one another.

Intermarket entries (Phase 2, additive): `StrategySet.intermarket_entries`
(strategy_sets/model.py) holds any entries whose legs span different
markets -- expand_strategy_set() below now ALSO rolls those, via the
existing, UNMODIFIED strategy_engine.intermarket_combinations.
generate_intermarket_instances() (exactly the same "this module adds no
new rolling/dedup logic of its own" principle as the single-market loop
above), and returns ONE combined list mixing both instance types. No
change was needed to generate_intermarket_instances() itself, to
build_history()/prewarm_leg_cache(), or to template_scanner.scanner.
run_scan_on_instances() -- all three already operate on `.rics` alone,
agnostic to which instance type they're given (see each module's own
docstring for confirmation of exactly what it reads off an instance).
"""

from __future__ import annotations

from core.utils import DateLike, get_logger

from strategy_engine.combinations import StrategyInstance
from strategy_engine.intermarket_combinations import (
    IntermarketStrategyInstance,
    generate_intermarket_instances,
)
from template_scanner.universe import (
    dedupe_candidates,
    dedupe_intermarket_candidates,
    generate_candidates,
)

from strategy_sets.model import StrategySet

logger = get_logger(__name__)


def expand_strategy_set(
    strategy_set: StrategySet,
    contract_start: DateLike,
    contract_end: DateLike,
    only_enabled: bool = True,
    dedupe: bool = True,
) -> list[StrategyInstance | IntermarketStrategyInstance]:
    """Roll every (by default, enabled-only) entry in `strategy_set`
    (both `strategy_set.entries` and `strategy_set.intermarket_entries`)
    across the SAME `contract_start`/`contract_end` window and combine
    the results into ONE list, single-market and intermarket instances
    freely mixed -- ready to pass directly to template_scanner.scanner.
    run_scan_on_instances().

    `contract_start`/`contract_end` are supplied here, not stored on
    the StrategySet or any entry, so the same saved set can be expanded
    against a different contract window every time it's used --
    exactly matching template_scanner.scanner.ScanRequest's own
    contract_start/contract_end, one shared window per call/request.

    Each entry's own `eligible_rics` (StrategySetEntry.expansion /
    IntermarketStrategySetEntry.expansion) IS still applied per entry,
    for both entry types -- it needs no single-shared-curve concept,
    applying identically to any instance via its own `.rics`.
    `max_curve_position` is applied per single-market entry exactly as
    before; it is rejected at IntermarketStrategySetEntry construction
    time instead (see strategy_sets/model.py), since "curve position"
    has no defined meaning once legs span different markets/curves.

    `only_enabled` (default True) skips any entry (of either type)
    whose `enabled` flag is False. Set False to expand every entry
    regardless of its enabled flag.

    `dedupe` (default True) removes exact duplicates WITHIN each
    instance type independently -- see template_scanner.universe.
    dedupe_candidates() (single-market) and dedupe_intermarket_
    candidates() (intermarket). The two types are never compared
    against each other for dedup purposes: a single-market instance and
    an intermarket instance can never be "the same strategy" by
    construction (their definitions are different types entirely), so
    no cross-type dedup step exists or is needed.

    Returns [] (not an error) if `strategy_set` has no enabled entries
    of either type, or if the contract window doesn't contain enough
    listed contracts to fill some entry's largest offset span --
    inherited unchanged from strategy_engine.generate_instances()/
    generate_intermarket_instances().

    ORDERING NOTE (Phase 2 design review): the returned list is always
    every single-market instance (in `strategy_set.entries` order)
    followed by every intermarket instance (in `strategy_set.
    intermarket_entries` order) -- NOT necessarily the original relative
    order entries appeared in when a StrategySet was deserialized from
    one JSON `entries` array (see strategy_sets/serialization.py's own
    "Intermarket entries" docstring section), since that original
    cross-type interleaving is not retained once split into these two
    separate, typed collections. This was deliberately NOT "fixed" by
    adding order-tracking to StrategySet, for two reasons: (1) doing so
    would need a real change to StrategySet's stored shape (a new field
    plus a new cross-field consistency invariant to validate), which is
    a materially bigger change than anything else in this phase for a
    narrow benefit; (2) the only current consumer that displays
    ScanCandidateResult order to a trader (ui/results_view.py's results
    grid) ALWAYS applies a ranking before display -- its own
    `_current_rank_state()` defaults `primary_field` to the first
    available rank metric rather than "no ranking", so this raw,
    pre-ranking order is never actually what a trader sees. It is
    directly observable only to a caller that inspects ScanReport.
    results (or this function's own return value) without ranking --
    e.g. a test, or a future non-UI consumer -- and is fully
    deterministic (stable within each type, single-market always first
    across types) even though it isn't literal-JSON-order. Revisit if a
    future UI ever lets a trader author/reorder intermarket entries
    directly and displays raw scan order without ranking.
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

    intermarket_instances: list[IntermarketStrategyInstance] = []
    skipped_intermarket_disabled = 0
    for entry in strategy_set.intermarket_entries:
        if only_enabled and not entry.enabled:
            skipped_intermarket_disabled += 1
            continue

        generated = generate_intermarket_instances(entry.definition, contract_start, contract_end)
        if entry.expansion.eligible_rics is not None:
            eligible = set(entry.expansion.eligible_rics)
            generated = [inst for inst in generated if set(inst.rics) <= eligible]
        intermarket_instances.extend(generated)

    if dedupe:
        intermarket_instances = dedupe_intermarket_candidates(intermarket_instances)

    combined: list[StrategyInstance | IntermarketStrategyInstance] = instances + intermarket_instances

    logger.debug(
        "expand_strategy_set: '%s' (%d entrie(s), %d skipped disabled; %d intermarket "
        "entrie(s), %d skipped disabled) -> %d instance(s) [%s -> %s]",
        strategy_set.name, len(strategy_set.entries), skipped_disabled,
        len(strategy_set.intermarket_entries), skipped_intermarket_disabled,
        len(combined), contract_start, contract_end,
    )
    return combined
