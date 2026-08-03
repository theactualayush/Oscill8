"""
range_analytics package

Module 4A -- Range-Bound Analytics. Consumes a strategy_engine.
StrategyHistory and computes independently-interpretable range/
location, movement, oscillation, and mean-reversion diagnostics for a
selected historical window. Never retrieves market data itself and
never imports database or core.downloader.

Produces measurements only -- no range-bound classification, no
composite score, no regime-duration detection, no ranking. Those are
out of scope for this module.
"""

from range_analytics.lookback import resolve_window
from range_analytics.mean_reversion import AR1Fit, fit_ar1
from range_analytics.oscillation import count_crossings
from range_analytics.results import RangeAnalytics, analyze_range
from range_analytics.units import price_to_bp

__all__ = [
    "RangeAnalytics",
    "analyze_range",
    "resolve_window",
    "count_crossings",
    "AR1Fit",
    "fit_ar1",
    "price_to_bp",
]
