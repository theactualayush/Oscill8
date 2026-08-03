"""
definitions.py

Generic strategy-shape data model. A multi-leg futures strategy is
fully described by a market, a set of leg offsets, a matching set of
leg weights, a bar interval, and a price field -- never by a
per-strategy-name calculation path. Spread/fly/condor are shapes of
this same data, not distinct code branches.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import config
from core.config import BarInterval

# Only Close is supported today, per Module 3's explicit scope. Kept as
# a whitelist (not a hard-coded literal) so adding Open/VWAP/etc. later
# is a one-line change, not a structural one.
_SUPPORTED_PRICE_FIELDS = {"Close"}


@dataclass(frozen=True)
class StrategyDefinition:
    """The generic shape of a multi-leg futures strategy.

    Examples:
        Spread:  offsets=(0, 1),       weights=(1, -1)
        Fly:     offsets=(0, 1, 2),    weights=(1, -2, 1)
        Condor:  offsets=(0, 1, 2, 3), weights=(1, -1, -1, 1)
        Custom:  offsets=(0, 1, 2),    weights=(2, -5, 3)
    """

    market_key: str
    offsets: tuple[int, ...]
    weights: tuple[float, ...]
    interval: BarInterval
    price_field: str = "Close"

    def __post_init__(self) -> None:
        config.get_market(self.market_key)  # raises KeyError if unknown

        offsets = tuple(self.offsets)
        weights = tuple(self.weights)
        object.__setattr__(self, "offsets", offsets)
        object.__setattr__(self, "weights", weights)

        if len(offsets) < 2:
            raise ValueError(f"A strategy needs at least 2 legs, got {len(offsets)}")
        if len(offsets) != len(weights):
            raise ValueError(
                f"offsets and weights must be the same length, got "
                f"{len(offsets)} offsets and {len(weights)} weights"
            )
        if offsets[0] != 0:
            raise ValueError(f"offsets must start at 0, got {offsets}")
        if any(a >= b for a, b in zip(offsets, offsets[1:])):
            raise ValueError(f"offsets must be strictly increasing, got {offsets}")
        if all(w == 0 for w in weights):
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
