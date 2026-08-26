"""
intermarket_combinations.py

Generates rolling contract combinations for an IntermarketDefinition --
the additive-sibling counterpart to combinations.py's generate_instances(),
for legs that span more than one market.

Same delegation principle as combinations.py: no new calendar or
RIC-building logic lives here. Each leg's own market contract list comes
from the existing, UNMODIFIED core.futures_calendar.generate_contracts()
(called once per leg, independently -- one market's call never depends
on another market's listing_cycle), and every RIC is built via the
existing, UNMODIFIED core.ric.build_ric()/parsed via core.ric.parse_ric().

OFFSET SEMANTICS (design-reviewed, corrected from an earlier draft of
this module -- see git history/CLAUDE.md for the "interpretation A -> C"
review): LegSpec.offset ALWAYS refers to a position on THAT LEG'S OWN
contract curve -- exactly the same meaning StrategyDefinition.offsets
already has for a single-market strategy (position N on the one curve
that exists). It NEVER refers to a shared/intersected curve. An earlier
draft applied every leg's offset to one curve shared across ALL legs in
the definition, which made a given offset's real-world meaning depend on
which OTHER markets happened to share the definition (e.g. FED_FUNDS's
own January/February contracts became invisible whenever FED_FUNDS was
paired with a quarterly-only market) -- silently discarding real,
tradeable contracts on the finer-grained leg's own curve. That is wrong
and is not what this module does.

The shared/intersected curve still exists, but its role is narrower and
more precise: it is used ONLY to determine which calendar months are
valid ANCHOR periods -- i.e. the periods where the strategy's offset=0
leg(s) (there must be at least one, enforced by IntermarketDefinition's
own min(offsets) == 0 validation) can all simultaneously reference the
exact same month. Concretely:

    1. Split legs into anchor legs (offset == 0) and non-anchor legs
       (offset > 0).
    2. Generate each anchor leg's own market's contracts independently
       via generate_contracts(leg.market_key, contract_start,
       contract_end), parse each back to (year, month) via parse_ric(),
       and intersect those sets across the anchor legs only -- this is
       what "SOFR Dec-2026 pairs with SONIA Dec-2026" means concretely:
       every anchor leg must have a real, independently-generated listed
       contract in that exact month for it to be a valid anchor period.
    3. For each anchor leg at each valid anchor period, build its RIC
       directly at that (year, month) via build_ric().
    4. For each non-anchor leg, independently generate ITS OWN full
       (year, month) curve over the same window (never intersected with
       anything). At each anchor period, find the first position on that
       leg's own curve at or after the anchor month, then step forward
       `offset` MORE positions on that SAME curve -- never on the shared
       anchor axis. If that position doesn't exist (window doesn't reach
       far enough), this anchor period produces no instance for the
       whole definition, mirroring combinations.generate_instances()'s
       own "too few contracts" behavior.

This also fixes a latent chronological-ordering bug in the (month, year)
intersection this module previously computed: sorting (month, year)
tuples directly sorts by month first, which is wrong across a year
boundary (e.g. (12, 2026) would incorrectly sort after (3, 2027)).
Internally this module now always sorts/compares (year, month) tuples,
converting to build_ric()'s own (market_key, month, year) argument order
only at the point of construction.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

import core.ric as ric_module
from core import futures_calendar
from core.utils import DateLike, get_logger

from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec

logger = get_logger(__name__)


@dataclass(frozen=True)
class IntermarketStrategyInstance:
    """One concrete, dated occurrence of an IntermarketDefinition's shape.

    Deliberately mirrors strategy_engine.combinations.StrategyInstance's
    exact field names/shapes (definition, rics) -- this is what lets
    strategy_engine.pricing.build_history()/prewarm_leg_cache() consume
    an instance of this class with NO code change: neither function
    reads `market_key` anywhere, only `instance.rics` and
    `instance.definition.{interval, price_field, weights}`, all of
    which IntermarketDefinition also exposes (see its `weights`
    property).
    """

    definition: IntermarketDefinition
    rics: tuple[str, ...]


def _own_year_months(
    market_key: str,
    contract_start: DateLike,
    contract_end: DateLike,
) -> list[tuple[int, int]]:
    """One market's own (year, month) contract curve between
    contract_start/contract_end, sorted chronologically.

    (year, month) -- not (month, year) -- specifically so sorting/
    bisecting compares chronologically correctly across a year boundary;
    core.ric.build_ric() itself still takes (market_key, month, year), so
    callers unpack accordingly at the point of construction.
    """
    contracts = futures_calendar.generate_contracts(market_key, contract_start, contract_end)
    return sorted(
        (parsed.year, parsed.month)
        for parsed in (ric_module.parse_ric(contract) for contract in contracts)
    )


def _anchor_axis(
    anchor_legs: list[LegSpec],
    contract_start: DateLike,
    contract_end: DateLike,
) -> list[tuple[int, int]]:
    """(year, month) anchor periods valid for EVERY anchor (offset == 0)
    leg's own market, sorted chronologically.

    Intersected across anchor legs ONLY -- never across non-anchor legs,
    whose own curves are handled independently in
    generate_intermarket_instances (see the module docstring).
    """
    year_month_sets = [
        set(_own_year_months(leg.market_key, contract_start, contract_end))
        for leg in anchor_legs
    ]
    common = set.intersection(*year_month_sets)
    return sorted(common)


def generate_intermarket_instances(
    definition: IntermarketDefinition,
    contract_start: DateLike,
    contract_end: DateLike,
) -> list[IntermarketStrategyInstance]:
    """Generate rolling IntermarketStrategyInstances for a definition's
    legs/shape.

    See the module docstring for the full offset-semantics explanation:
    each leg's offset is always a position on THAT LEG'S OWN contract
    curve, counted from the nearest position at or after the current
    anchor period -- never a position on a curve shared across legs.

    Returns an empty list (not an error) if the window doesn't produce
    any valid anchor period, or if a non-anchor leg's own curve doesn't
    reach far enough forward at some anchor period -- mirrors
    combinations.generate_instances()'s own "too few contracts" behavior
    (that anchor period simply contributes no instance, rather than
    raising).
    """
    legs = definition.legs
    anchor_legs = [leg for leg in legs if leg.offset == 0]
    anchor_axis = _anchor_axis(anchor_legs, contract_start, contract_end)

    # Each non-anchor leg's own curve is independent of every other leg
    # (including other non-anchor legs) and of the anchor axis itself --
    # generated once here, reused across every anchor period below.
    own_curves: list[list[tuple[int, int]] | None] = [
        None if leg.offset == 0 else _own_year_months(leg.market_key, contract_start, contract_end)
        for leg in legs
    ]

    instances = []
    for anchor_year_month in anchor_axis:
        rics = []
        for leg, own_curve in zip(legs, own_curves):
            if leg.offset == 0:
                year, month = anchor_year_month
                rics.append(ric_module.build_ric(leg.market_key, month, year))
                continue

            base_idx = bisect_left(own_curve, anchor_year_month)
            target_idx = base_idx + leg.offset
            if target_idx >= len(own_curve):
                rics = None
                break
            year, month = own_curve[target_idx]
            rics.append(ric_module.build_ric(leg.market_key, month, year))

        if rics is not None:
            instances.append(IntermarketStrategyInstance(definition=definition, rics=tuple(rics)))

    logger.debug(
        "Generated %d intermarket instance(s) for markets=%s offsets=%s [%s -> %s]",
        len(instances), definition.market_keys, [leg.offset for leg in legs],
        contract_start, contract_end,
    )
    return instances
