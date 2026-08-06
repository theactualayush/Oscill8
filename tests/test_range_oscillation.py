"""
tests/test_range_oscillation.py

count_crossings boundary semantics are the focus here: values sitting
exactly on the equilibrium or exactly on a hysteresis band edge must
behave deterministically (always "inside"/neutral), independent of any
> vs >= implementation detail.

count_oscillations (Tradability Analytics) is a materially different
metric sharing the same underlying zone-transition state machine:
completed Top<->Bottom traversal-and-return cycles between two fixed,
independent boundaries, where touching a boundary DOES count as
reaching it (the opposite convention from count_crossings' hysteresis
band).
"""

from __future__ import annotations

import pandas as pd
import pytest

from range_analytics.oscillation import count_crossings, count_oscillations


def test_below_to_above_through_equilibrium_counts_one_crossing():
    s = pd.Series([-1.0, 0.0, 1.0])
    assert count_crossings(s, equilibrium=0.0, threshold=0.0) == 1


def test_above_to_below_through_equilibrium_counts_one_crossing():
    s = pd.Series([1.0, 0.0, -1.0])
    assert count_crossings(s, equilibrium=0.0, threshold=0.0) == 1


def test_repeated_observations_exactly_at_equilibrium_never_cross():
    s = pd.Series([0.0, 0.0, 0.0, 0.0])
    assert count_crossings(s, equilibrium=0.0, threshold=0.0) == 0


def test_repeated_equilibrium_touches_do_not_reset_established_side():
    s = pd.Series([-1.0, 0.0, 0.0, 0.0, -1.0])
    assert count_crossings(s, equilibrium=0.0, threshold=0.0) == 0


def test_below_lower_threshold_then_inside_then_exactly_upper_threshold_no_crossing():
    # Touching the upper edge exactly does not register as reaching "above".
    s = pd.Series([-2.0, 0.0, 1.0])
    assert count_crossings(s, equilibrium=0.0, threshold=1.0) == 0


def test_exactly_lower_threshold_then_inside_then_exactly_upper_threshold_no_crossing():
    # Both edges touched exactly -- never strictly exceeded -- so no side is
    # ever established and there is nothing to cross.
    s = pd.Series([-1.0, 0.0, 1.0])
    assert count_crossings(s, equilibrium=0.0, threshold=1.0) == 0


def test_observations_entirely_inside_hysteresis_band_have_no_crossings():
    s = pd.Series([0.2, -0.3, 0.4, -0.1])
    assert count_crossings(s, equilibrium=0.0, threshold=1.0) == 0


def test_genuine_excursion_beyond_band_registers_a_crossing():
    s = pd.Series([-2.0, 0.0, 2.0])
    assert count_crossings(s, equilibrium=0.0, threshold=1.0) == 1


def test_multiple_crossings_are_all_counted():
    s = pd.Series([-2.0, 2.0, -2.0, 2.0])
    assert count_crossings(s, equilibrium=0.0, threshold=1.0) == 3


def test_threshold_zero_matches_raw_equilibrium_crossings():
    s = pd.Series([-1.0, 1.0, -1.0])
    assert count_crossings(s, equilibrium=0.0, threshold=0.0) == 2


def test_first_observation_establishes_side_without_counting_as_a_crossing():
    s = pd.Series([5.0])
    assert count_crossings(s, equilibrium=0.0, threshold=0.0) == 0


def test_empty_series_has_zero_crossings():
    assert count_crossings(pd.Series([], dtype=float), equilibrium=0.0, threshold=0.0) == 0


def test_negative_threshold_raises():
    with pytest.raises(ValueError):
        count_crossings(pd.Series([1.0]), equilibrium=0.0, threshold=-1.0)


def test_nan_in_series_raises():
    with pytest.raises(ValueError):
        count_crossings(pd.Series([1.0, float("nan")]), equilibrium=0.0, threshold=0.0)


# ---------------------------------------------------------------------
# count_oscillations (Tradability Analytics)
#
# Boundaries used throughout: lower=0.0 (Bottom), upper=10.0 (Top),
# 5.0 as a representative "Middle" value strictly between them.
# ---------------------------------------------------------------------

def test_top_bottom_top_is_one_oscillation():
    s = pd.Series([10.0, 0.0, 10.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 1


def test_bottom_top_bottom_is_one_oscillation():
    s = pd.Series([0.0, 10.0, 0.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 1


def test_top_middle_bottom_middle_top_is_one_oscillation():
    s = pd.Series([10.0, 5.0, 0.0, 5.0, 10.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 1


def test_bottom_middle_top_middle_bottom_is_one_oscillation():
    s = pd.Series([0.0, 5.0, 10.0, 5.0, 0.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 1


def test_top_to_bottom_only_is_zero_oscillations():
    s = pd.Series([10.0, 0.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 0


def test_top_middle_bottom_only_is_zero_oscillations():
    s = pd.Series([10.0, 5.0, 0.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 0


def test_top_middle_top_is_zero_oscillations():
    # Never leaves the "Top" side -- the middle observation is neutral
    # and does not itself establish or reset a side.
    s = pd.Series([10.0, 5.0, 10.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 0


def test_internal_median_noise_without_boundary_touch_is_zero_oscillations():
    s = pd.Series([5.0, 6.0, 4.0, 5.0, 6.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 0


def test_return_to_top_not_yet_occurred_is_zero_completed_oscillations():
    # Middle -> Top -> Middle -> Bottom -> Middle: side flips Top -> Bottom
    # once (a half traversal) but never returns to Top -- 0 completed.
    s = pd.Series([5.0, 10.0, 5.0, 0.0, 5.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 0


def test_multiple_complete_oscillations_are_counted():
    s = pd.Series([10.0, 0.0, 10.0, 0.0, 10.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 2


def test_intermediate_observations_do_not_reset_state():
    # Top -> Middle -> Middle -> Bottom -> Middle -> Middle -> Top = 1.
    s = pd.Series([10.0, 5.0, 5.0, 0.0, 5.0, 5.0, 10.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 1


def test_boundary_touch_counts_as_reaching_it():
    # Values sit exactly on the boundaries (not beyond them) -- must
    # still register as Top/Bottom, unlike count_crossings' hysteresis
    # convention where an exact touch is neutral.
    s = pd.Series([10.0, 0.0, 10.0])
    assert count_oscillations(s, lower=0.0, upper=10.0) == 1


def test_zero_width_range_is_zero_oscillations_not_nan():
    s = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0])
    assert count_oscillations(s, lower=2.0, upper=2.0) == 0
    # Also true for an actually-flat series at that same point.
    flat = pd.Series([2.0, 2.0, 2.0])
    assert count_oscillations(flat, lower=2.0, upper=2.0) == 0


def test_empty_series_has_zero_oscillations():
    assert count_oscillations(pd.Series([], dtype=float), lower=0.0, upper=10.0) == 0


def test_lower_greater_than_upper_raises():
    with pytest.raises(ValueError):
        count_oscillations(pd.Series([1.0]), lower=10.0, upper=0.0)


def test_nan_in_series_raises_for_oscillations():
    with pytest.raises(ValueError):
        count_oscillations(pd.Series([1.0, float("nan")]), lower=0.0, upper=10.0)


def test_narrower_percentile_band_can_produce_a_different_oscillation_count():
    # Same series, two different (lower, upper) boundary pairs -- as
    # P5/P95 vs P25/P75 would produce for the same window. The narrower
    # band (P25/P75-style) is touched by the mid-sized excursion to 7.0
    # that the wider band (P5/P95-style) never reaches, so it registers
    # more completed oscillations for identical underlying data.
    s = pd.Series([0.0, 5.0, 10.0, 5.0, 0.0, 5.0, 7.0, 5.0, 0.0])
    wide = count_oscillations(s, lower=0.0, upper=10.0)
    narrow = count_oscillations(s, lower=2.0, upper=6.0)
    assert wide == 1
    assert narrow == 2
    assert wide != narrow
