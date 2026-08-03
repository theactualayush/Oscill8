"""
pricing.py

Turns StrategyInstances into historical strategy price series. Leg
histories are retrieved exclusively through database.get_history --
this module never imports core.downloader or lseg.data, preserving the
rule that only the data layer talks to LSEG.

Legs are aligned by an inner join on Date: the strategy value is only
computed for timestamps where every leg has an observation. No
forward-fill -- a missing leg bar drops that timestamp entirely rather
than fabricating a value from a stale price.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce

import pandas as pd

from core.config import BarInterval
from core.utils import DateLike, get_logger
from database import get_history

from strategy_engine.combinations import StrategyInstance

logger = get_logger(__name__)

LegCache = dict[tuple, pd.DataFrame]


@dataclass(frozen=True)
class StrategyHistory:
    """Historical price series for one StrategyInstance.

    `history` is a clean DataFrame: Date, Leg_1..Leg_N, Strategy. All
    identifying metadata (market, RICs, weights, offsets, interval,
    price field) lives on `instance`/`price_field`, not as DataFrame
    columns, so `history` stays purely numeric for downstream analytics.
    """

    instance: StrategyInstance
    price_field: str
    history: pd.DataFrame


def _fetch_leg(
    ric: str,
    interval: BarInterval,
    price_start: DateLike,
    price_end: DateLike,
    leg_cache: LegCache | None,
) -> pd.DataFrame:
    if leg_cache is None:
        return get_history(ric, interval, price_start, price_end)

    key = (ric, interval.value, str(price_start), str(price_end))
    if key not in leg_cache:
        leg_cache[key] = get_history(ric, interval, price_start, price_end)
    return leg_cache[key]


def build_history(
    instance: StrategyInstance,
    price_start: DateLike,
    price_end: DateLike,
    leg_cache: LegCache | None = None,
) -> StrategyHistory:
    """Fetch, align, and weight one StrategyInstance's leg histories.

    `leg_cache`, when supplied, is a plain dict reused across multiple
    build_history calls (see generate_histories) so a RIC shared by
    several instances is only ever fetched once.
    """
    definition = instance.definition
    price_field = definition.price_field
    leg_columns = [f"Leg_{i + 1}" for i in range(len(instance.rics))]

    legs = []
    for ric, col in zip(instance.rics, leg_columns):
        frame = _fetch_leg(ric, definition.interval, price_start, price_end, leg_cache)
        leg = frame[["Date", price_field]].rename(columns={price_field: col})
        # database.get_history returns Date as datetime64[ns], except on
        # a completely-empty result where it defaults to object dtype --
        # normalize so an all-empty leg still merges cleanly instead of
        # raising a dtype-mismatch error on the join key.
        leg["Date"] = pd.to_datetime(leg["Date"])
        legs.append(leg)

    aligned = reduce(lambda left, right: pd.merge(left, right, on="Date", how="inner"), legs)
    aligned["Strategy"] = sum(
        aligned[col] * weight for col, weight in zip(leg_columns, definition.weights)
    )
    aligned = aligned.sort_values("Date").reset_index(drop=True)

    return StrategyHistory(instance=instance, price_field=price_field, history=aligned)


def generate_histories(
    instances: list[StrategyInstance],
    price_start: DateLike,
    price_end: DateLike,
) -> list[StrategyHistory]:
    """Build histories for many instances, fetching each distinct leg once.

    Shares one leg_cache across all instances so overlapping legs (e.g.
    adjacent rolling flies sharing two of three contracts) are only
    retrieved from database.get_history once each, regardless of how
    many instances reference them.
    """
    leg_cache: LegCache = {}
    return [
        build_history(instance, price_start, price_end, leg_cache=leg_cache)
        for instance in instances
    ]
