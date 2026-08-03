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


def price_to_bp(value: float, market_key: str) -> float:
    """Convert a price-unit value into basis points for `market_key`.

    NaN propagates automatically (NaN * anything == NaN).

    Raises:
        KeyError: if market_key is not a registered market.
    """
    market = config.get_market(market_key)
    return value * market.bp_per_point
