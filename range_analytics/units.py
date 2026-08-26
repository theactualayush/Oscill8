"""
units.py

Price-point -> basis-point conversion, sourced from each market's own
MarketDefinition.bp_per_point (core.config) rather than a hard-coded
*100 scattered through the analytics code. Keeps every diagnostic
function in this package market-agnostic; only this one-line function
touches core.config for the conversion.
"""

from __future__ import annotations

from core import config
from strategy_engine.intermarket_definitions import IntermarketDefinition


def price_to_bp(value: float, market_key: str) -> float:
    """Convert a price-unit value into basis points for `market_key`.

    NaN propagates automatically (NaN * anything == NaN).

    Raises:
        KeyError: if market_key is not a registered market.
    """
    market = config.get_market(market_key)
    return value * market.bp_per_point


class BpConversionUnavailable(ValueError):
    """Raised by resolve_bp_per_point() when a bp-per-point conversion
    factor cannot be resolved for a strategy definition -- e.g. an
    IntermarketDefinition with no explicit bp_per_point override.

    This is never silently worked around: not by falling back to the
    first leg's market, the anchor leg's market, any other arbitrary
    leg's market, or an average of the legs' conventions. There is no
    single market whose bp convention is economically meaningful for a
    combined cross-market series, so callers must either supply an
    explicit override on the IntermarketDefinition or accept that
    bp-denominated metrics are undefined (NaN) for that strategy.
    """


def resolve_bp_per_point(definition) -> float:
    """Resolve the bp-per-point conversion factor for ANY strategy
    definition -- dispatched purely by TYPE, never by inspecting a
    specific market_key value.

    - A single-market definition (anything exposing `.market_key`, e.g.
      strategy_engine.definitions.StrategyDefinition) resolves via its
      one registered market's own MarketDefinition.bp_per_point --
      identical to price_to_bp(value, market_key)'s existing lookup.
    - An IntermarketDefinition resolves via its own explicit
      `bp_per_point` override, if set.

    Raises:
        BpConversionUnavailable: an IntermarketDefinition has no
            bp_per_point override set -- see that exception's own
            docstring for why this is never guessed instead.
        KeyError: (single-market only) market_key is not a registered
            market -- identical to price_to_bp's existing behavior.
    """
    if isinstance(definition, IntermarketDefinition):
        if definition.bp_per_point is None:
            raise BpConversionUnavailable(
                "No bp_per_point is set on this IntermarketDefinition -- "
                "a cross-market strategy has no single market whose "
                "convention could apply automatically. Set an explicit "
                "bp_per_point on the definition to enable bp-denominated "
                "metrics for this strategy."
            )
        return definition.bp_per_point

    return config.get_market(definition.market_key).bp_per_point
