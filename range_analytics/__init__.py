"""
range_analytics package

Module 4A -- Range-Bound Analytics. Consumes a strategy_engine.
StrategyHistory and computes independently-interpretable range/
location, movement, oscillation, and mean-reversion diagnostics for a
selected historical window. Never retrieves market data itself and
never imports database or core.downloader.

Module 4B -- Range Stability & Persistence. Repeatedly analyzes ONE
StrategyHistory at MULTIPLE lookback windows and describes how Module
4A's own measurements move across them (dispersion, short-vs-long
change, step-by-step structure) -- built entirely on top of
analyze_range(), never reaching into 4A's lower-level primitives.

Both modules produce measurements only -- no range-bound
classification, no composite score, no regime-duration/age detection,
no ranking. Those are out of scope for this package (reserved for a
future Module 5 Template/Scanner layer).
"""

from range_analytics.lookback import resolve_window
from range_analytics.mean_reversion import AR1Fit, fit_ar1
from range_analytics.multi_lookback import (
    MultiLookbackAnalytics,
    analyze_multi_lookback,
    range_to_volatility_ratio,
    robust_to_full_width_ratio,
)
from range_analytics.oscillation import count_crossings
from range_analytics.results import RangeAnalytics, analyze_range
from range_analytics.stability import LookbackStability
from range_analytics.units import price_to_bp

__all__ = [
    "RangeAnalytics",
    "analyze_range",
    "resolve_window",
    "count_crossings",
    "AR1Fit",
    "fit_ar1",
    "price_to_bp",
    "MultiLookbackAnalytics",
    "analyze_multi_lookback",
    "LookbackStability",
    "range_to_volatility_ratio",
    "robust_to_full_width_ratio",
]
