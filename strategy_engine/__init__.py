"""
strategy_engine package

Turns individual futures contracts into historical multi-leg strategy
price series. Strategies are represented generically by market, leg
offsets, and leg weights -- never by a per-strategy-name calculation
path. Historical prices are retrieved exclusively through
database.get_history; this package never imports core.downloader or
lseg.data directly.
"""

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.combinations import StrategyInstance, generate_instances
from strategy_engine.pricing import StrategyHistory, build_history, generate_histories

__all__ = [
    "StrategyDefinition",
    "StrategyInstance",
    "generate_instances",
    "StrategyHistory",
    "build_history",
    "generate_histories",
]
