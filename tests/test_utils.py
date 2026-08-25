"""
tests/test_utils.py

Unit tests for core/utils.py's shared, provider-agnostic helpers:
missing_business_days() and longest_missing_business_day_run() -- the
'valid observation' gap-detection primitives database.service uses to
decide LSEG completeness for the cache -> LSEG -> QuantHub fallback
(see database.service._is_complete_history). missing_business_days()
mirrors ui.chart_view._missing_weekdays' own test conventions (weekend
exclusion, empty input, time-of-day normalization).
"""

from __future__ import annotations

import pandas as pd

from core.utils import longest_missing_business_day_run, missing_business_days


# ---------------------------------------------------------------------
# missing_business_days
# ---------------------------------------------------------------------

def test_missing_business_days_empty_for_pure_weekend_gap():
    # Friday -> Monday, no weekday skipped.
    dates = pd.to_datetime(["2026-01-02", "2026-01-05"])  # Fri, Mon
    assert missing_business_days(pd.Series(dates)) == []


def test_missing_business_days_detects_a_dropped_business_day():
    # Monday and Wednesday present, Tuesday missing.
    dates = pd.to_datetime(["2026-01-05", "2026-01-07"])
    missing = missing_business_days(pd.Series(dates))
    assert missing == [pd.Timestamp("2026-01-06")]


def test_missing_business_days_never_includes_a_saturday_or_sunday():
    dates = pd.to_datetime(["2026-01-01", "2026-01-10"])
    missing = missing_business_days(pd.Series(dates))
    assert all(d.weekday() < 5 for d in missing)


def test_missing_business_days_empty_for_empty_input():
    assert missing_business_days(pd.Series([], dtype="datetime64[ns]")) == []


def test_missing_business_days_normalizes_time_of_day():
    # Same calendar dates, arbitrary times -- must not be misclassified
    # as missing just because the exact timestamp differs.
    dates = pd.to_datetime(["2026-01-05 09:30:00", "2026-01-06 14:00:00", "2026-01-07 23:59:00"])
    missing = missing_business_days(pd.Series(dates))
    assert missing == []


def test_missing_business_days_any_bar_at_any_time_counts_as_present():
    # HOURLY-style data: a date with only ONE bar at any time of day
    # still counts as "present", not missing -- this is what lets the
    # same helper generalize from DAILY to HOURLY/4H.
    dates = pd.to_datetime(["2026-01-05 03:00:00", "2026-01-07 21:00:00"])
    missing = missing_business_days(pd.Series(dates))
    assert missing == [pd.Timestamp("2026-01-06")]


# ---------------------------------------------------------------------
# longest_missing_business_day_run
# ---------------------------------------------------------------------

def test_longest_run_zero_when_nothing_missing():
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])  # Mon-Wed, no gap
    assert longest_missing_business_day_run(pd.Series(dates)) == 0


def test_longest_run_a_single_dropped_weekday():
    dates = pd.to_datetime(["2026-01-05", "2026-01-07"])  # Tue missing
    assert longest_missing_business_day_run(pd.Series(dates)) == 1


def test_longest_run_bridges_a_weekend_between_two_missing_weeks():
    # A whole two-week stretch of missing weekdays, split by a normal
    # weekend in the middle -- must be counted as ONE continuous run,
    # not fragmented into two isolated 5-day runs.
    dates = pd.to_datetime(["2026-01-02", "2026-01-19"])  # Fri Jan 2 -> Mon Jan 19
    run = longest_missing_business_day_run(pd.Series(dates))
    # Missing weekdays: Jan 5-9, 12-16 (10 business days) -- one run.
    assert run == 10


def test_longest_run_normal_holiday_cluster_stays_short():
    # A realistic single holiday cluster (a Mon-Fri closure) -- 5
    # consecutive missing business days, comfortably under any sensible
    # incomplete-history threshold.
    dates = pd.to_datetime(["2026-01-02", "2026-01-12"])  # Fri Jan 2 -> Mon Jan 12
    run = longest_missing_business_day_run(pd.Series(dates))
    assert run == 5


def test_longest_run_genuine_multi_week_gap_is_long():
    # ~6-week hole, matching the user's own "Jan 1 -- MISSING -- Jun 1"
    # style example -- must be a large, unambiguous run.
    dates = pd.to_datetime(["2026-01-15", "2026-03-02"])
    run = longest_missing_business_day_run(pd.Series(dates))
    assert run > 20


def test_longest_run_two_separate_gaps_picks_the_larger():
    dates = pd.to_datetime(["2026-01-01", "2026-01-08", "2026-03-01"])
    run = longest_missing_business_day_run(pd.Series(dates))
    # Jan 1 -> Jan 8: small gap; Jan 8 -> Mar 1: large gap -- the
    # function must report the LARGER of the two, not the first found.
    assert run > 20


def test_longest_run_empty_for_empty_input():
    assert longest_missing_business_day_run(pd.Series([], dtype="datetime64[ns]")) == 0


def test_longest_run_zero_for_a_single_date():
    assert longest_missing_business_day_run(pd.Series(pd.to_datetime(["2026-01-05"]))) == 0
