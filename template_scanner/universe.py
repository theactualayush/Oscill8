"""
universe.py

Generates a candidate-strategy universe from one or many same-market
templates (StrategyDefinitions), rolled across a market's contract
curve via strategy_engine.generate_instances() -- unmodified. Adds two
filtering concepts strategy_engine doesn't have (a maximum curve
position, an explicit eligible-RIC set) as post-filters over the
already-generated candidates, and deterministic deduplication across
however many templates/rows contributed to the universe.

No market data is fetched anywhere in this module -- StrategyInstance
generation is pure calendar/combinatorics (core.futures_calendar),
exactly as in strategy_engine.combinations.

Individual template generation (generate_candidates, one definition at
a time) is kept deliberately separate from batch orchestration across
many rows (generate_candidate_universe), so a caller can use either
independently.
"""

from __future__ import annotations

from core import futures_calendar
from core.utils import DateLike, get_logger

from strategy_engine.combinations import StrategyInstance, generate_instances
from strategy_engine.definitions import StrategyDefinition

logger = get_logger(__name__)


def generate_candidates(
    definition: StrategyDefinition,
    contract_start: DateLike,
    contract_end: DateLike,
    max_curve_position: int | None = None,
    eligible_rics: set[str] | None = None,
) -> list[StrategyInstance]:
    """Roll one StrategyDefinition across its market's contract curve.

    Delegates entirely to strategy_engine.generate_instances() for the
    actual rolling -- this function adds only the two filtering
    concepts strategy_engine doesn't provide:

    `max_curve_position`, if given, drops any instance whose furthest
    leg is more than `max_curve_position` listed contracts out from
    `contract_start` (0-indexed: the nearest eligible contract is
    position 0). Computed by re-deriving the same ordered contract
    list generate_instances() builds internally (a second call to the
    already-public futures_calendar.generate_contracts -- pure
    calendar arithmetic, no I/O) purely to look up each instance's
    furthest RIC's ordinal position.

    `eligible_rics`, if given, keeps only instances whose every leg is
    in the given set -- e.g. to restrict a scan to a curated,
    currently-liquid contract list.

    Returns an empty list (not an error) if the window doesn't contain
    enough contracts to fill the definition's largest offset span --
    inherited unchanged from generate_instances().
    """
    instances = generate_instances(definition, contract_start, contract_end)

    if max_curve_position is not None:
        contracts = futures_calendar.generate_contracts(
            definition.market_key, contract_start, contract_end
        )
        index_of = {ric: i for i, ric in enumerate(contracts)}
        instances = [
            inst for inst in instances
            if max(index_of[r] for r in inst.rics) <= max_curve_position
        ]

    if eligible_rics is not None:
        instances = [inst for inst in instances if set(inst.rics) <= eligible_rics]

    logger.debug(
        "generate_candidates: %s offsets=%s -> %d candidate(s) [%s -> %s]",
        definition.market_key, definition.offsets, len(instances),
        contract_start, contract_end,
    )
    return instances


def generate_candidate_universe(
    definitions: list[StrategyDefinition],
    contract_start: DateLike,
    contract_end: DateLike,
    max_curve_position: int | None = None,
    eligible_rics: set[str] | None = None,
) -> list[StrategyInstance]:
    """Combine the rolled candidates from many template rows into one
    universe. Each definition is processed independently through
    generate_candidates(); this function is pure orchestration --
    definition-level generation logic lives entirely in
    generate_candidates(), not duplicated here.
    """
    universe: list[StrategyInstance] = []
    for definition in definitions:
        universe.extend(
            generate_candidates(
                definition, contract_start, contract_end,
                max_curve_position=max_curve_position,
                eligible_rics=eligible_rics,
            )
        )
    return universe


def _candidate_identity(instance: StrategyInstance) -> tuple:
    """Deterministic identity for deduplication: two candidates are the
    same iff they would produce a byte-identical StrategyDefinition +
    RICs, and therefore a byte-identical StrategyHistory and
    analytics. Weights are compared as exact tuples, never
    normalized/scaled -- (1, -2, 1) and (2, -4, 2) on the same RICs are
    the same shape but different economic exposure and must never
    collapse together.
    """
    definition = instance.definition
    return (
        definition.market_key,
        instance.rics,
        definition.weights,
        definition.interval,
        definition.price_field,
    )


def dedupe_candidates(instances: list[StrategyInstance]) -> list[StrategyInstance]:
    """Remove exact duplicates -- same market, same RICs, same weights
    (unscaled), same interval, same price_field -- keeping the first
    occurrence and preserving order. Two candidates that share RICs but
    differ in weight scale (e.g. (1, -2, 1) vs (2, -4, 2)) are NOT
    duplicates and both are kept.
    """
    seen: set[tuple] = set()
    result: list[StrategyInstance] = []
    for instance in instances:
        key = _candidate_identity(instance)
        if key not in seen:
            seen.add(key)
            result.append(instance)
    return result
