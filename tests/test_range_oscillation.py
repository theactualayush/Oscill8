"""
tests/test_range_oscillation.py

count_crossings boundary semantics are the focus here: values sitting
exactly on the equilibrium or exactly on a hysteresis band edge must
behave deterministically (always "inside"/neutral), independent of any
> vs >= implementation detail.
"""

from __future__ import annotations

import pandas as pd
import pytest

from range_analytics.oscillation import count_crossings


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
