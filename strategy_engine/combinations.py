"""
combinations.py

Generates rolling contract combinations for a StrategyDefinition by
delegating entirely to core.futures_calendar -- no new calendar or
RIC-building logic lives here. generate_contracts() already returns
fully-resolved RIC strings, so this module never touches core.ric.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import futures_calendar
from core.utils import DateLike, get_logger

from strategy_engine.definitions import StrategyDefinition

logger = get_logger(__name__)


@dataclass(frozen=True)
class StrategyInstance:
    """One concrete, dated occurrence of a StrategyDefinition's shape."""

    definition: StrategyDefinition
    rics: tuple[str, ...]


def generate_instances(
    definition: StrategyDefinition,
    contract_start: DateLike,
    contract_end: DateLike,
) -> list[StrategyInstance]:
    """Generate rolling StrategyInstances for a definition's market/shape.

    Lists the market's contracts between contract_start/contract_end
    via futures_calendar.generate_contracts, then slides the
    definition's offsets across them via futures_calendar.rolling_windows.
    Returns an empty list (not an error) if the window doesn't contain
    enough contracts to fill the largest offset span.
    """
    contracts = futures_calendar.generate_contracts(
        definition.market_key, contract_start, contract_end
    )
    windows = futures_calendar.rolling_windows(contracts, list(definition.offsets))

    instances = [StrategyInstance(definition=definition, rics=rics) for rics in windows]
    logger.debug(
        "Generated %d instance(s) for %s offsets=%s [%s -> %s]",
        len(instances), definition.market_key, definition.offsets,
        contract_start, contract_end,
    )
    return instances
