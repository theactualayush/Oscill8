"""
intermarket_definitions.py

Additive sibling to definitions.py: models a strategy whose individual
legs belong to DIFFERENT markets (e.g. a SOFR leg vs. a SONIA leg
combined into one series), as opposed to StrategyDefinition, which is
deliberately single-market_key (one market, N offsets/weights on that
one market's own curve).

Design principle: never modify StrategyDefinition/StrategyInstance to
support this -- see strategy_sets/expansion.py's own note that a future
intermarket concept "is expected to need an additive sibling concept
... not a change to" the existing single-market path. This module is
that sibling. Existing single-market strategies are completely
unaffected -- nothing here is imported by definitions.py/combinations.py/
pricing.py/template_scanner/strategy_sets.

LegSpec bundles (market_key, offset, weight) together per leg, rather
than StrategyDefinition's three parallel tuples (offsets, weights) over
one shared market_key -- this is a deliberate difference, not an
oversight: a "mismatched array lengths" failure mode is structurally
impossible here, since each leg's own three fields travel together.

bp_per_point is carried as an explicit, optional override rather than
looked up from a single market's own MarketDefinition.bp_per_point
(range_analytics.units.price_to_bp today does exactly that lookup for a
StrategyDefinition's one market_key) -- for a genuinely cross-market
combined series there is no principled way to pick "the" market whose
convention applies automatically, so the choice is left explicit and
unset (None) by default. Wiring this into range_analytics is
deliberately NOT done in this module -- an explicit follow-up phase.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import config
from core.config import BarInterval

from strategy_engine.definitions import _SUPPORTED_PRICE_FIELDS


@dataclass(frozen=True)
class LegSpec:
    """One leg of an IntermarketDefinition: its own market, its own
    offset into that market's contract calendar, and its own weight.

    `offset` is NOT an index into this leg's own market's raw contract
    list -- it is an index into the SHARED, calendar-month-aligned
    sequence computed across every leg's market in one
    IntermarketDefinition (see intermarket_combinations.
    generate_intermarket_instances). `offset=0` means "this leg's
    contract at the shared window's current aligned position";
    `offset=1` means "this leg's contract at the next aligned position"
    -- never simply "the next contract on this market's own curve" once
    that curve has been intersected with another market's.
    """

    market_key: str
    offset: int
    weight: float

    def __post_init__(self) -> None:
        config.get_market(self.market_key)  # raises KeyError if unknown

        if not isinstance(self.offset, int) or isinstance(self.offset, bool):
            raise TypeError(f"LegSpec.offset must be an int, got {type(self.offset)}")
        if self.offset < 0:
            raise ValueError(f"LegSpec.offset must be >= 0, got {self.offset}")


@dataclass(frozen=True)
class IntermarketDefinition:
    """The generic shape of a strategy whose legs may belong to
    different markets.

    Examples:
        SOFR vs SONIA basis:
            legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0))
        SOFR vs CORRA, one quarter forward on each leg:
            legs=(LegSpec("SOFR", 1, 1.0), LegSpec("CORRA", 1, -1.0))

    Unlike StrategyDefinition.offsets (positions on ONE shared curve,
    required strictly increasing), LegSpec.offset values may repeat
    across legs -- two different markets' legs both at offset=0 is the
    ordinary intermarket-spread case, not a degenerate one. The only
    cross-leg constraint is that at least one leg anchors the window at
    offset=0 (min(offsets) == 0), mirroring StrategyDefinition's own
    "offsets must start at 0" anchoring rule without requiring a
    specific leg ordering, since legs here reference independent curves
    rather than positions on one shared curve.
    """

    legs: tuple[LegSpec, ...]
    interval: BarInterval
    price_field: str = "Close"
    bp_per_point: float | None = None

    def __post_init__(self) -> None:
        legs = tuple(self.legs)
        object.__setattr__(self, "legs", legs)

        if len(legs) < 1:
            raise ValueError(f"An intermarket strategy needs at least 1 leg, got {len(legs)}")
        if not all(isinstance(leg, LegSpec) for leg in legs):
            raise TypeError("IntermarketDefinition.legs must contain only LegSpec instances")

        offsets = [leg.offset for leg in legs]
        if min(offsets) != 0:
            raise ValueError(
                f"At least one leg must anchor the window at offset 0, got offsets {offsets}"
            )

        if all(leg.weight == 0 for leg in legs):
            raise ValueError("weights cannot be all zero")

        interval = self.interval
        if isinstance(interval, str):
            interval = BarInterval(interval)
        object.__setattr__(self, "interval", interval)

        if self.price_field not in _SUPPORTED_PRICE_FIELDS:
            raise ValueError(
                f"Unsupported price_field '{self.price_field}'. "
                f"Supported: {sorted(_SUPPORTED_PRICE_FIELDS)}"
            )

        if self.bp_per_point is not None and self.bp_per_point <= 0:
            raise ValueError(f"bp_per_point must be > 0 if given, got {self.bp_per_point}")

    @property
    def weights(self) -> tuple[float, ...]:
        """Leg weights in leg order -- exists so an IntermarketStrategyInstance
        satisfies the exact interface strategy_engine.pricing.build_history
        already reads off `instance.definition` (interval, price_field,
        weights), with no change to that function."""
        return tuple(leg.weight for leg in self.legs)

    @property
    def market_keys(self) -> tuple[str, ...]:
        """Leg market keys in leg order -- for callers that need to know
        which market produced which leg (e.g. tests, future analytics)
        without re-parsing RICs."""
        return tuple(leg.market_key for leg in self.legs)
