"""
composite.py

Phases 2 and 3 of the composite ("Group A x Group B") Strategy Set
design: the COMPOSITION layer that turns a Phase 1 StrategyGroupPair
configuration (strategy_sets/model.py) into concrete strategy_engine.
IntermarketDefinition objects -- one per surviving A/B source-strategy
pair. "Surviving" means two things here: the pair survived the
unordered-pair deduplication in cartesian_pairs() (Phase 2), and the
definition it composed to is not structurally zero (Phase 3, see the
STRUCTURAL ZERO section below).

This module deliberately contains NO curve generation, NO calendar
arithmetic, NO RIC construction, and NO instance-level deduplication.
It is a thin composition step that sits BETWEEN the saved configuration
and Module 9's existing, unmodified machinery:

    Group A source strategy  +  Group B source strategy
              |
              v
     ONE IntermarketDefinition  (this module)
              |
              v
    strategy_engine.intermarket_combinations.generate_intermarket_instances()
    template_scanner.universe.dedupe_intermarket_candidates()
              |
              v
    template_scanner.scanner.run_scan_on_instances() -> existing analytics

Concretely, "Strategy A - Strategy B" is expressed as ONE flat leg list:
every leg of A at its own weight, followed by every leg of B at its
NEGATED weight. That is exactly what IntermarketDefinition already
models (an arbitrary flat tuple of LegSpec, each carrying its own
market_key/offset/weight), so no new definition type, no nesting, and
no second expansion engine is introduced. Subtraction -- not addition
-- is what makes "SR3 Fly - SR3 Fly" resolve to a structurally
all-zero series, which is exactly what is_structurally_zero() detects
and resolve_composite_combinations() drops (see the STRUCTURAL ZERO
section below, and the same-pair note on cartesian_pairs(), which
deliberately still FORMS such a pair -- the filtering happens one
level up).

WHY A FLAT LEG LIST IS SUFFICIENT, INCLUDING FOR INTERMARKET SOURCES:
a source strategy may be either an ordinary StrategySetEntry (a
single-market StrategyDefinition: one market_key, N offsets/weights) or
an existing Module 9 IntermarketStrategySetEntry (an
IntermarketDefinition: N LegSpecs). BOTH flatten to the same thing --
`tuple[LegSpec, ...]` -- so composing them needs no nested/recursive
structure and cannot create one. `definition_legs()` below is the whole
of that translation. This is why supporting existing intermarket
entries as group members costs nothing here and was not deferred.

OFFSET SEMANTICS ARE INHERITED, NOT REDEFINED: every LegSpec.offset in
a composed definition keeps exactly the meaning Module 9 already gives
it -- a position on THAT LEG'S OWN contract curve, with the offset==0
legs collectively defining the anchor period (see intermarket_
combinations.py's module docstring). A single-market source
strategy's offsets are already 0-anchored and strictly increasing
(StrategyDefinition's own validation), so a composed definition always
satisfies IntermarketDefinition's min(offset) == 0 rule with no
re-basing, shifting, or reinterpretation of any offset.

STRUCTURAL ZERO (Phase 3): composing "A - B" can produce a definition
whose legs cancel exactly -- most obviously "SR3 Fly - SR3 Fly", but
also any pair whose legs cancel once aggregated. Such a definition
prices to a flat 0.0 series for every rolled contract and every date,
by construction, carrying no information at all. is_structurally_zero()
below detects this from the DEFINITION ALONE (no market data, no
provider call, no calendar arithmetic) and resolve_composite_
combinations() drops such combinations immediately after composing
them -- before instance generation, cache prewarming, history
construction, analytics, or scanning. This is deliberately automatic
and not user-configurable: it is a mathematical property of the
definition, the same class of derivation rule as template_scanner.
universe.dedupe_intermarket_candidates() removing exact duplicates,
not a trader judgement like the optional metric filters.

This must never be confused with a HISTORICALLY FLAT series -- a real
strategy whose realized prices happen not to move over some lookback
(a stale or illiquid contract, a quiet window). That is a property of
DATA, not of the definition, it can affect any candidate of any kind,
and it is already handled correctly by range_analytics (0.0 for genuine
zeros, NaN for genuine 0/0) plus the trader's own optional filters.
Nothing here filters on data.

RUNTIME INTERVAL (Phase 4): a group's source strategies are loaded
from OTHER saved Strategy Sets at resolve time, so they arrive carrying
whatever interval those files persist -- an interval the trader's
current scan-bar selection knows nothing about. Applying an override to
the composite StrategySet itself (strategy_sets.execution.
with_interval_override) cannot reach them: that function rebuilds the
composite's own `entries`/`intermarket_entries`, and a group stores only
NAMES. resolve_composite_combinations() therefore accepts an optional
`interval` and applies it to each RESOLVED SourceStrategy's definition
BEFORE pairing and composition (see _with_runtime_interval below), so
every definition reaching compose_definition() already carries the
requested interval. Two consequences, both deliberate:

  * A scan never silently runs at a source file's persisted interval
    while the scan bar shows something else.
  * compose_definition()'s "a combined strategy needs one interval"
    check is untouched and still enforced -- it simply cannot fire on
    this path, because both sides were normalised to the same interval
    first. Two sources persisted at DIFFERENT intervals are therefore
    no longer an obstacle to a runtime scan, while a caller that
    supplies no interval still gets the original, strict behaviour.

Source Strategy Sets are never mutated: the override is a
dataclasses.replace() copy of the resolved SourceStrategy, exactly the
transient-copy idiom with_interval_override() already uses. Nothing is
written back to any repository file.

DISPLAY vs. IDENTITY: CompositeCombination.name is ALWAYS
"<Group A entry name> - <Group B entry name>", in that order, for every
combination this module returns. The canonical, order-insensitive key
used to drop reverse-direction duplicates (see pair_identity()) is a
separate, purely internal value that never influences a returned
combination's name, leg order, or A/B provenance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from core import config
from core.config import BarInterval
from core.utils import get_logger

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_combinations import (
    IntermarketStrategyInstance,
    generate_intermarket_instances,
)
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec

from strategy_sets.model import StrategyGroup, StrategySet
from strategy_sets.repository import StrategySetRepository

logger = get_logger(__name__)

# Separator between the Group A and Group B strategy names in a
# generated combination's display name. Group A is ALWAYS on the left.
COMBINATION_NAME_SEPARATOR = " - "


class CompositeResolutionError(ValueError):
    """A composite StrategySet's `groups` configuration cannot be
    resolved against the saved Strategy Sets it references.

    Subclasses ValueError so existing callers that already handle a
    Strategy Set's own domain-validation failures (e.g. ui.strategy_set_
    view's `except ValueError`) keep working unchanged, while a caller
    that wants to distinguish a dangling-reference failure specifically
    still can.

    Every message identifies the composite Strategy Set, which group
    (A or B), the source Strategy Set referenced, and the specific
    problem -- a dangling reference is never silently skipped or
    substituted (see resolve_group_entries()).
    """


@dataclass(frozen=True)
class SourceStrategy:
    """One resolved member of a group: the entry that was selected,
    plus where it was selected from.

    `set_name`/`entry_name` are provenance for error messages and for
    the generated combination's display name. `definition` is the real,
    unmodified StrategyDefinition or IntermarketDefinition object taken
    off the source entry -- never a copy or a re-derived shape.
    """

    set_name: str
    entry_name: str
    definition: StrategyDefinition | IntermarketDefinition


@dataclass(frozen=True)
class CompositeCombination:
    """ONE generated "Group A - Group B" combination: its user-facing
    name, its two source strategies (kept as separate, directional
    concepts), and the single IntermarketDefinition that expresses the
    pair as one strategy.

    `name` is always built Group-A-first (see COMBINATION_NAME_SEPARATOR)
    and is never reordered by deduplication. `group_a`/`group_b` remain
    individually addressable so a caller can present, group, or filter
    by either side without re-parsing the name string.

    `definition` is a plain IntermarketDefinition -- the exact type
    Module 9's generate_intermarket_instances() already consumes, so a
    combination needs no special expansion path of its own.
    """

    name: str
    group_a: SourceStrategy
    group_b: SourceStrategy
    definition: IntermarketDefinition


def definition_legs(definition) -> tuple[LegSpec, ...]:
    """Every leg of ANY strategy definition as a flat LegSpec tuple --
    dispatched purely by TYPE, never by inspecting a market_key value.

    An IntermarketDefinition already IS a flat LegSpec tuple and is
    returned as-is (its own legs, by reference -- LegSpec is frozen, so
    sharing is safe). A single-market StrategyDefinition's parallel
    offsets/weights tuples over its one market_key are zipped into the
    equivalent LegSpec tuple.

    This is the ONLY place the two definition shapes are reconciled,
    and it is what keeps a composed definition flat: composing two
    intermarket sources yields one longer leg list, never a nested
    definition.
    """
    if isinstance(definition, IntermarketDefinition):
        return definition.legs
    return tuple(
        LegSpec(market_key=definition.market_key, offset=offset, weight=weight)
        for offset, weight in zip(definition.offsets, definition.weights)
    )


def _negated(legs: tuple[LegSpec, ...]) -> tuple[LegSpec, ...]:
    """`legs` with every weight negated -- the "minus B" half of
    "A - B". Offsets and market keys are untouched."""
    return tuple(
        LegSpec(market_key=leg.market_key, offset=leg.offset, weight=-leg.weight)
        for leg in legs
    )


def _resolve_bp_per_point(legs: tuple[LegSpec, ...]) -> float | None:
    """The bp-per-point convention for a composed definition, or None.

    Resolved ONLY when every leg belongs to the same market -- then the
    composed series is denominated in exactly that one market's points,
    so its registered MarketDefinition.bp_per_point applies with no
    ambiguity at all, and is the same value a single-market
    StrategyDefinition on that market would resolve through
    range_analytics.units.resolve_bp_per_point().

    Returns None the moment two markets are involved, preserving Module
    9's rule verbatim: a genuinely cross-market series has no principled
    single market whose convention applies, so it is never guessed from
    one of its legs (range_analytics then raises BpConversionUnavailable
    and leaves the bp-denominated metrics NaN, exactly as it already
    does for any hand-authored intermarket entry).
    """
    market_keys = {leg.market_key for leg in legs}
    if len(market_keys) != 1:
        return None
    return config.get_market(next(iter(market_keys))).bp_per_point


def compose_definition(
    group_a: SourceStrategy,
    group_b: SourceStrategy,
) -> IntermarketDefinition:
    """Compose "Group A strategy MINUS Group B strategy" into ONE
    IntermarketDefinition.

    Legs are Group A's legs (unchanged) followed by Group B's legs with
    negated weights, in that order -- so leg order itself also carries
    the A-then-B orientation, not just the display name.

    `interval` and `price_field` must AGREE between the two source
    strategies. They are not reconciled, defaulted, or silently taken
    from one side: a single strategy series cannot meaningfully mix two
    bar intervals (strategy_engine.pricing.build_history inner-joins
    every leg on Date, so mixed intervals would silently produce a
    near-empty or meaningless series) or two price fields. In practice
    they always agree -- Excel-imported entries all carry the same
    DEFAULT_IMPORT_INTERVAL placeholder, and strategy_sets.execution.
    with_interval_override() applies one interval across a whole set.

    Raises:
        CompositeResolutionError: the two source strategies disagree on
            interval or price_field.
    """
    a_definition, b_definition = group_a.definition, group_b.definition

    if a_definition.interval != b_definition.interval:
        raise CompositeResolutionError(
            f"Cannot combine '{group_a.entry_name}' (interval "
            f"{a_definition.interval.value}) with '{group_b.entry_name}' (interval "
            f"{b_definition.interval.value}): a combined strategy needs one interval. "
            "Give both source strategies the same interval."
        )
    if a_definition.price_field != b_definition.price_field:
        raise CompositeResolutionError(
            f"Cannot combine '{group_a.entry_name}' (price_field "
            f"{a_definition.price_field}) with '{group_b.entry_name}' (price_field "
            f"{b_definition.price_field}): a combined strategy needs one price field."
        )

    legs = definition_legs(a_definition) + _negated(definition_legs(b_definition))
    return IntermarketDefinition(
        legs=legs,
        interval=a_definition.interval,
        price_field=a_definition.price_field,
        bp_per_point=_resolve_bp_per_point(legs),
    )


def strategy_identity(definition) -> tuple:
    """A deterministic, hashable identity for ONE source strategy.

    Identity is the strategy's SHAPE -- its legs (market/offset/weight,
    in leg order) plus interval and price_field -- never its name and
    never which Strategy Set it happens to be saved in. This follows the
    identity rule the surrounding architecture already established
    rather than inventing a second one: template_scanner.universe's
    _candidate_identity()/_intermarket_candidate_identity() key on
    market/rics/weights/interval/price_field, and the Excel import
    likewise defines strategy identity as "the resulting
    StrategyDefinition, never the Label" (a trader's label is a
    description, not an identifier).

    The practical consequence is deliberate and testable: "Set X / SR3
    Fly" and "Set Y / SR3 Fly" are the SAME strategy only when their
    shapes are genuinely identical -- in which case they would also
    produce byte-identical instances that Module 9's own
    dedupe_intermarket_candidates() collapses anyway. Two same-named
    strategies with different shapes never collapse.
    """
    legs = definition_legs(definition)
    return (
        tuple((leg.market_key, leg.offset, leg.weight) for leg in legs),
        definition.interval,
        definition.price_field,
    )


def pair_identity(group_a: SourceStrategy, group_b: SourceStrategy) -> tuple:
    """The canonical, ORDER-INSENSITIVE identity of an A/B pair:
    identity(A, B) == identity(B, A).

    Used ONLY to detect that a reverse-direction pair has already been
    generated. It never reaches a user, never reorders a combination's
    legs, and never rewrites its Group-A-first display name -- the
    surviving combination keeps whatever real A/B orientation it was
    generated with (see cartesian_pairs()).

    Ordering uses repr() of each side's own strategy_identity() purely
    as a stable total order over two arbitrary identity tuples; the
    ordered result is a set key, never a displayed or economically
    meaningful value.
    """
    keys = (strategy_identity(group_a.definition), strategy_identity(group_b.definition))
    return tuple(sorted(keys, key=repr))


def aggregate_leg_weights(definition) -> dict[tuple[str, int], float]:
    """Total weight per (market_key, offset), summed across every leg of
    ANY strategy definition (single-market or intermarket -- legs come
    from definition_legs(), so both shapes are handled identically).

    (market_key, offset) is the correct grouping key because it is
    exactly what determines a leg's RIC: strategy_engine.intermarket_
    combinations.generate_intermarket_instances() builds an anchor
    leg's RIC directly at the anchor (year, month), and a non-anchor
    leg's RIC by stepping `offset` positions along THAT LEG'S OWN
    curve from the anchor. Both are pure functions of (market_key,
    offset) once an anchor period is fixed, so two legs sharing a
    (market_key, offset) always resolve to the SAME RIC -- and
    therefore the same price -- in every generated instance. Grouping
    by anything coarser (market alone) or finer (leg position) would
    not have that property.

    Summed with math.fsum(), not the builtin sum(): fsum computes the
    exact sum of the float weights and rounds once, so the result is
    order-independent and a group whose weights genuinely cancel comes
    out as exactly 0.0 even when a naive left-to-right accumulation
    would leave a residual (e.g. 0.1 + 0.2 - 0.1 - 0.2 accumulates to
    2.78e-17). CPython 3.12's builtin sum() happens to compensate too,
    but that is a version-specific implementation detail; fsum is
    guaranteed. See is_structurally_zero() for why this removes the
    need for any tolerance constant.
    """
    weights_by_key: dict[tuple[str, int], list[float]] = {}
    for leg in definition_legs(definition):
        weights_by_key.setdefault((leg.market_key, leg.offset), []).append(leg.weight)
    return {key: math.fsum(weights) for key, weights in weights_by_key.items()}


def is_structurally_zero(definition) -> bool:
    """True when `definition` prices to an identically-zero series for
    every rolled contract and every date, provably from the definition
    alone -- e.g. the "SR3 Fly - SR3 Fly" that composing a strategy
    with itself produces.

    The test is: aggregate_leg_weights() by (market_key, offset), and
    every resulting aggregate is zero. Because legs sharing a
    (market_key, offset) always resolve to the same RIC (see
    aggregate_leg_weights), the strategy value collapses to
    `sum(aggregate_weight * price)` over the distinct keys -- so
    all-zero aggregates means a value of exactly 0.0 everywhere.

    It is NOT `sum(all weights) == 0`, which would be badly wrong: an
    ordinary fly (offsets (0, 1, 2), weights (1, -2, 1)) has weights
    summing to zero while being a perfectly meaningful strategy. Its
    aggregates here are {(M,0): 1, (M,1): -2, (M,2): 1} -- no group is
    zero, so it correctly survives. Only genuine per-position
    cancellation counts.

    Leg ORDER is irrelevant by construction (aggregation is
    commutative, and fsum is order-independent), so a source strategy
    whose legs are listed in a different order but describe the same
    shape is detected identically -- something an order-sensitive
    comparison like strategy_identity() would miss.

    Zero is tested EXACTLY (`== 0.0`), with no tolerance constant. Two
    reasons, both grounded in this repository rather than convention
    imported from elsewhere:

      1. There is no numerical tolerance for weights anywhere in
         Oscill8 to be consistent with. Every existing weight
         comparison is exact -- StrategyDefinition/IntermarketDefinition
         both reject an all-zero shape with `w == 0`, template_scanner.
         templates.template_from_dense_weights() selects legs with
         `w != 0`, and template_scanner.universe's dedup identities
         compare weight tuples exactly and unscaled. (database's own
         _EPSILON is a timedelta for sync-range arithmetic, unrelated
         to weights.) Introducing a float tolerance here would be a new
         convention, applied in one place.
      2. fsum makes it unnecessary. A group is reported zero iff the
         exact sum of its float weights is zero, which is true whenever
         the weights genuinely cancel -- IEEE-754 negation is exact, so
         a weight and its negation always cancel exactly whatever its
         decimal value. It is false only when the floats genuinely do
         not cancel (0.1 + 0.2 - 0.3 leaves a real 2.78e-17, because
         those three floats really are not equal in magnitude).

    The residual risk is therefore asymmetric in the safe direction: an
    exact test can only ever FAIL to drop a near-zero definition, which
    then simply flows on as an ordinary candidate whose series is
    historically flat -- already handled by the existing analytics and
    filters. A tolerance could instead DROP a real strategy with
    genuinely small weights, silently destroying a candidate. Given the
    choice, the failure mode that degrades gracefully is the correct
    one.
    """
    return all(weight == 0.0 for weight in aggregate_leg_weights(definition).values())


def resolve_group_entries(
    group: StrategyGroup,
    group_label: str,
    composite_set_name: str,
    repository: StrategySetRepository,
) -> list[SourceStrategy]:
    """Resolve one group's `source_set_name`/`selected_entry_names`
    against the saved Strategy Sets, preserving the group's own
    explicit selection order.

    A selected name is looked up across the source set's `entries` AND
    `intermarket_entries` together -- one shared namespace, which
    StrategySet's own validation already guarantees is unique across
    both collections, so a name resolves to at most one strategy of
    either kind. Both kinds are supported as group members (see the
    module docstring: both flatten to a flat LegSpec tuple, so no
    nesting can arise).

    A selected entry's own `enabled` flag is NOT consulted: naming a
    strategy in a group's selection IS the decision to include it, and
    silently dropping half a Cartesian product because of a flag set in
    a different, source Strategy Set would be exactly the kind of quiet
    substitution this function exists to prevent.

    Raises:
        CompositeResolutionError: the source Strategy Set does not
            exist; the source Strategy Set is itself a composite (has
            its own `groups` -- nested composites are not supported,
            see the V1 scope note); or a selected strategy name is not
            in that source set. Every message names the composite set,
            the group (A or B), the source set, and the specific
            missing/offending value -- a missing selection is never
            skipped or substituted.
    """
    where = f"composite Strategy Set '{composite_set_name}', Group {group_label}"

    try:
        source_set = repository.load(group.source_set_name)
    except FileNotFoundError as exc:
        raise CompositeResolutionError(
            f"{where}: source Strategy Set '{group.source_set_name}' was not found "
            f"({exc})."
        ) from exc

    if source_set.groups is not None:
        raise CompositeResolutionError(
            f"{where}: source Strategy Set '{group.source_set_name}' is itself a "
            "composite Strategy Set (it has its own Group A/Group B configuration). "
            "Nested composite Strategy Sets are not supported -- a group's source "
            "must be an ordinary Strategy Set of strategies."
        )

    by_name = {entry.name: entry for entry in source_set.entries}
    by_name.update({entry.name: entry for entry in source_set.intermarket_entries})

    resolved: list[SourceStrategy] = []
    for entry_name in group.selected_entry_names:
        entry = by_name.get(entry_name)
        if entry is None:
            raise CompositeResolutionError(
                f"{where}: selected strategy '{entry_name}' was not found in source "
                f"Strategy Set '{group.source_set_name}' (available: "
                f"{sorted(by_name)})."
            )
        resolved.append(
            SourceStrategy(
                set_name=group.source_set_name,
                entry_name=entry_name,
                definition=entry.definition,
            )
        )
    return resolved


def cartesian_pairs(
    group_a_strategies: list[SourceStrategy],
    group_b_strategies: list[SourceStrategy],
) -> list[tuple[SourceStrategy, SourceStrategy]]:
    """The Group A x Group B Cartesian product, in Group-A-then-Group-B
    selection order, with reverse-direction duplicates removed.

    Iteration order is Group A's selection order in the OUTER loop and
    Group B's selection order in the INNER loop, so the product reads
    naturally as "every Group B strategy against Group A's first
    strategy, then against its second, ...". No alphabetical sorting is
    applied at any point.

    Deduplication is by pair_identity() -- an unordered key -- so if
    both (X, Y) and (Y, X) occur in the product, only the FIRST one
    generated survives, keeping its own real A/B orientation. The
    canonical key is never used to reorder or rename what survives.

    A strategy paired with ITSELF (X, X) is a legitimate combination and
    is returned (once). It is not an error, and it is deliberately NOT
    filtered out here even though "A - A" composes to a structurally
    all-zero series: this function's job is pairing, and structural-zero
    filtering is resolve_composite_combinations()' job, applied to the
    COMPOSED definition one level up (see is_structurally_zero()). That
    split is intentional -- the pair must be formed and composed before
    it can be tested, and it still consumes its slot in the unordered-
    pair dedup above, so forming it is what stops the reverse (X, X)
    from being reconsidered later.
    """
    seen: set[tuple] = set()
    pairs: list[tuple[SourceStrategy, SourceStrategy]] = []
    for a in group_a_strategies:
        for b in group_b_strategies:
            key = pair_identity(a, b)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((a, b))
    return pairs


def _with_runtime_interval(
    sources: list[SourceStrategy], interval: BarInterval | None
) -> list[SourceStrategy]:
    """`sources` with every definition's interval replaced by
    `interval` -- or `sources` unchanged when `interval` is None.

    Transient copies only (dataclasses.replace on the frozen
    SourceStrategy and its frozen definition), so the source Strategy
    Set objects these were resolved from -- and the JSON files behind
    them -- are never touched. replace() re-runs each definition's own
    __post_init__, so an invalid interval fails exactly as constructing
    that definition would.
    """
    if interval is None:
        return sources
    return [
        replace(source, definition=replace(source.definition, interval=interval))
        for source in sources
    ]


def resolve_composite_combinations(
    strategy_set: StrategySet,
    repository: StrategySetRepository,
    interval: BarInterval | None = None,
) -> list[CompositeCombination]:
    """Resolve a composite StrategySet's `groups` configuration into its
    generated "Group A - Group B" combinations.

    Returns [] -- never an error -- when the set is not an A x B
    composite at all:
      * `strategy_set.groups is None` (an ordinary Strategy Set), or
      * `groups.group_b is None` (Phase 1 explicitly allows a Group-A-
        only configuration; with no right-hand side there is no product
        to form), or
      * either group's selection is empty.

    A composed pair that is STRUCTURALLY ZERO (is_structurally_zero() --
    every leg cancels, so the series would be identically 0.0 for every
    contract and every date) is dropped here, immediately after
    composition and before any instance generation, provider call,
    cache prewarming, history construction, or analytics. "SR3 Fly -
    SR3 Fly" is the canonical example. This is automatic and not
    configurable -- see the module docstring for why it is a derivation
    rule rather than a trader-facing filter, and why it is a completely
    different thing from a historically flat series. Group A x Group B
    pairing itself is unchanged: such a pair is still GENERATED (a
    strategy may legitimately be selected on both sides) and still
    consumes its slot in the unordered-pair dedup, it is simply not
    returned.

    `interval`, when given, is the runtime scan interval: it is applied
    to every resolved Group A and Group B source definition BEFORE
    pairing and composition (see _with_runtime_interval and the module
    docstring's RUNTIME INTERVAL section). Omitting it preserves the
    original behaviour exactly -- source definitions are composed at
    whatever interval their own saved files carry, and two sources that
    disagree raise from compose_definition() as before.

    Nothing is persisted: combinations are derived here on every call
    and never written back onto the StrategySet (see strategy_sets/
    model.py -- `groups` stores the configuration only). Supplying
    `interval` does not change that: the override produces transient
    copies and never touches a source Strategy Set or its file.

    Deterministic: the same StrategySet and the same saved source sets
    always produce the same combinations, in the same order, with the
    same names.

    Raises:
        CompositeResolutionError: any group reference cannot be
            resolved (missing source set, nested composite source set,
            missing selected strategy), or a pair's two source
            strategies disagree on interval/price_field. See
            resolve_group_entries()/compose_definition().
    """
    groups = strategy_set.groups
    if groups is None or groups.group_b is None:
        return []

    group_a_strategies = _with_runtime_interval(
        resolve_group_entries(groups.group_a, "A", strategy_set.name, repository), interval
    )
    group_b_strategies = _with_runtime_interval(
        resolve_group_entries(groups.group_b, "B", strategy_set.name, repository), interval
    )

    combinations: list[CompositeCombination] = []
    structurally_zero = 0
    for a, b in cartesian_pairs(group_a_strategies, group_b_strategies):
        definition = compose_definition(a, b)
        if is_structurally_zero(definition):
            structurally_zero += 1
            continue
        combinations.append(
            CompositeCombination(
                name=f"{a.entry_name}{COMBINATION_NAME_SEPARATOR}{b.entry_name}",
                group_a=a,
                group_b=b,
                definition=definition,
            )
        )

    # One summary line, never one per dropped combination -- a large
    # Cartesian product must not flood the log.
    if structurally_zero:
        logger.info(
            "resolve_composite_combinations: '%s' dropped %d structurally-zero "
            "combination(s) (every leg cancels; the series would be identically 0)",
            strategy_set.name, structurally_zero,
        )

    logger.debug(
        "resolve_composite_combinations: '%s' (Group A '%s' x %d, Group B '%s' x %d) "
        "-> %d combination(s), %d structurally zero",
        strategy_set.name,
        groups.group_a.source_set_name, len(group_a_strategies),
        groups.group_b.source_set_name, len(group_b_strategies),
        len(combinations), structurally_zero,
    )
    return combinations


def expand_combinations(
    combinations: list[CompositeCombination],
    contract_start,
    contract_end,
) -> list[IntermarketStrategyInstance]:
    """Roll every combination's composed definition across the contract
    window, via Module 9's own UNMODIFIED generate_intermarket_instances()
    -- this module adds no calendar/RIC/rolling logic of its own.

    Returned in combination order, each combination's own rolled
    instances in the order Module 9 produced them. No deduplication
    happens here: instance-level dedup is
    template_scanner.universe.dedupe_intermarket_candidates()'s job, and
    strategy_sets.expansion.expand_strategy_set() applies it across the
    whole combined intermarket list (a set's own intermarket_entries
    included), not per combination.

    Pair this with composite_labels_by_definition_id() over the SAME
    `combinations` list to carry each candidate's "Group A - Group B"
    name into the scan (see that function's own identity note).
    """
    instances: list[IntermarketStrategyInstance] = []
    for combination in combinations:
        instances.extend(
            generate_intermarket_instances(combination.definition, contract_start, contract_end)
        )
    return instances


def composite_labels_by_definition_id(
    combinations: list[CompositeCombination],
) -> dict[int, str]:
    """`{id(combination.definition): combination.name}` -- ready to pass
    straight to template_scanner.scanner.run_scan_on_instances(
    labels_by_definition_id=...) so each generated candidate carries its
    "Group A - Group B" name into ScanCandidateResult.label.

    Safe for the same reason that mechanism already works for grid rows
    and Strategy Set entries: generate_intermarket_instances() stores
    the definition object it was given BY REFERENCE on every instance it
    rolls (see strategy_engine/intermarket_combinations.py), never a
    clone, so one id maps to every candidate a combination produced.

    IDENTITY CAVEAT: every resolve_composite_combinations() call builds
    FRESH IntermarketDefinition objects, so these ids only match
    instances rolled from THIS SAME `combinations` list. Resolve once,
    then roll that same list with expand_combinations() -- ids from one
    resolution never match instances produced by another. (This is why
    expand_strategy_set(), which resolves internally, is the unlabelled
    path: a caller that wants labels drives resolve -> expand_
    combinations -> run_scan_on_instances itself.)
    """
    return {id(combination.definition): combination.name for combination in combinations}


__all__ = [
    "COMBINATION_NAME_SEPARATOR",
    "CompositeResolutionError",
    "SourceStrategy",
    "CompositeCombination",
    "definition_legs",
    "compose_definition",
    "aggregate_leg_weights",
    "is_structurally_zero",
    "strategy_identity",
    "pair_identity",
    "resolve_group_entries",
    "cartesian_pairs",
    "resolve_composite_combinations",
    "expand_combinations",
    "composite_labels_by_definition_id",
]
