"""
tests/test_range_stability.py

build_stability/LookbackStability tested with hand-built value tuples --
no StrategyHistory or RangeAnalytics needed for this generic primitive.
"""

from __future__ import annotations

import math
import statistics

import pytest

from range_analytics.stability import build_stability


def test_build_stability_basic_summary_stats():
    result = build_stability((1.0, 2.0, 4.0), signed=False)
    assert result.values == (1.0, 2.0, 4.0)
    assert result.defined_count == 3
    assert result.min == 1.0
    assert result.max == 4.0
    assert result.stdev == pytest.approx(statistics.stdev([1.0, 2.0, 4.0]))


def test_build_stability_short_vs_long_diff_and_ratio_unsigned():
    result = build_stability((2.0, 4.0, 8.0), signed=False)
    assert result.short_vs_long_diff == pytest.approx(6.0)
    assert result.short_vs_long_ratio == pytest.approx(4.0)


def test_build_stability_signed_metric_never_reports_ratio():
    result = build_stability((-0.5, 0.0, 0.5), signed=True)
    assert result.short_vs_long_diff == pytest.approx(1.0)
    assert math.isnan(result.short_vs_long_ratio)
    assert all(math.isnan(r) for r in result.pairwise_ratios)


def test_build_stability_pairwise_diffs_and_ratios_worked_example():
    # The exact example from the design review: robust width
    # 2.5/2.5/3.0/6.0/12.5bp at lookbacks 20/40/60/90/120.
    result = build_stability((2.5, 2.5, 3.0, 6.0, 12.5), signed=False)
    assert result.pairwise_diffs == pytest.approx((0.0, 0.5, 3.0, 6.5))
    assert result.pairwise_ratios == pytest.approx((1.0, 1.2, 2.0, 2.5 / 1.2))


def test_build_stability_ratio_nan_when_denominator_zero():
    result = build_stability((0.0, 5.0), signed=False)
    assert math.isnan(result.short_vs_long_ratio)
    assert math.isnan(result.pairwise_ratios[0])


def test_build_stability_nan_propagates_through_pairwise_and_defined_count():
    result = build_stability((1.0, float("nan"), 3.0), signed=False)
    assert result.defined_count == 2
    assert result.min == 1.0
    assert result.max == 3.0
    assert math.isnan(result.pairwise_diffs[0])  # 1.0 -> NaN
    assert math.isnan(result.pairwise_diffs[1])  # NaN -> 3.0
    # short_vs_long uses values[0]/values[-1] directly, both defined here
    assert result.short_vs_long_diff == pytest.approx(2.0)


def test_build_stability_stdev_nan_below_two_defined_values():
    result = build_stability((5.0, float("nan"), float("nan")), signed=False)
    assert result.defined_count == 1
    assert math.isnan(result.stdev)
    assert result.min == 5.0
    assert result.max == 5.0


def test_build_stability_all_nan_degrades_gracefully():
    result = build_stability((float("nan"), float("nan")), signed=False)
    assert result.defined_count == 0
    assert math.isnan(result.stdev)
    assert math.isnan(result.min)
    assert math.isnan(result.max)
    assert math.isnan(result.short_vs_long_diff)
    assert math.isnan(result.short_vs_long_ratio)


def test_build_stability_single_value_no_pairwise_entries():
    result = build_stability((7.0,), signed=False)
    assert result.pairwise_diffs == ()
    assert result.pairwise_ratios == ()
    assert math.isnan(result.short_vs_long_diff) is False
    assert result.short_vs_long_diff == 0.0
