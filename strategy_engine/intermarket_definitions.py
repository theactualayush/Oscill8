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

    `offset` is a position on THIS LEG'S OWN contract curve, counted
    forward from the nearest position at or after the current anchor
    period (see intermarket_combinations.generate_intermarket_instances
    for the full algorithm). `offset=0` legs collectively define the
    anchor period (their calendars are intersected to find valid shared
    anchor months); a non-anchor leg's `offset=1` means "the next
    contract on THIS market's own curve after the anchor month" -- it is
    NEVER an index into a curve shared/intersected across all legs,
    which would make its meaning depend on which other markets happen to
    share the same IntermarketDefinition.
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


def resolve_display_market_key(definition) -> str:
    """A human-readable market label for ANY strategy definition --
    strategy_engine.definitions.StrategyDefinition (single-market) or
    IntermarketDefinition (this module) -- dispatched purely by TYPE,
    never by inspecting a specific market_key value (no
    `if market_key == "..."` anywhere).

    DISPLAY ONLY. Never pass this value into provider resolution
    (core.providers.resolve_provider), cache lookup, QuantHub/LSEG
    instrument mapping, core.config market-registry lookups, or bp
    conversion -- all of those operate strictly per RIC or per single
    LegSpec/StrategyDefinition.market_key, never via a composite label.
    An intermarket definition's label here is simply its leg market
    keys joined with "/" in leg order -- purely cosmetic, carries no
    identity/economic meaning of its own.
    """
    if isinstance(definition, IntermarketDefinition):
        return "/".join(definition.market_keys)
    return definition.market_key


def resolve_display_offsets(definition) -> tuple[int, ...]:
    """Leg offsets for ANY strategy definition, single-market or
    intermarket, in leg order -- dispatched purely by TYPE, same caveat
    as resolve_display_market_key: DISPLAY ONLY.

    An intermarket definition's offsets are each a position on that
    leg's OWN contract curve (see LegSpec's own docstring), not
    positions on one shared curve -- this tuple must never be
    reinterpreted downstream as if it were.
    """
    if isinstance(definition, IntermarketDefinition):
        return tuple(leg.offset for leg in definition.legs)
    return definition.offsets
