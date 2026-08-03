"""
tests/test_cache.py

Unit tests for database/cache.py: bar upsert/dedup, range reads, and
sync-range bookkeeping (including bar-frequency-aware merging).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from database import cache

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def _make_df(dates: list[str], seed: float = 100.0) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": [seed + i for i in range(n)],
            "High": [seed + i + 1 for i in range(n)],
            "Low": [seed + i - 1 for i in range(n)],
            "Close": [seed + i + 0.5 for i in range(n)],
            "Volume": [1000 + i for i in range(n)],
        }
    )


# ---------------------------------------------------------------------
# insert_bars
# ---------------------------------------------------------------------

def test_insert_bars_persists_all_rows(db_session):
    df = _make_df(["2026-01-01", "2026-01-02", "2026-01-03"])
    n = cache.insert_bars(db_session, "SRAZ26", "DAILY", df)
    assert n == 3

    out = cache.read_bars(
        db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 3)
    )
    assert list(out.columns) == _CANONICAL_COLUMNS
    assert len(out) == 3
    assert out["Close"].tolist() == df["Close"].tolist()


def test_insert_bars_skips_duplicates_silently(db_session):
    df = _make_df(["2026-01-01", "2026-01-02"])
    cache.insert_bars(db_session, "SRAZ26", "DAILY", df)
    n_second = cache.insert_bars(db_session, "SRAZ26", "DAILY", df)
    assert n_second == 0

    out = cache.read_bars(
        db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert len(out) == 2  # no duplicates


def test_insert_bars_partial_overlap_only_inserts_new_rows(db_session):
    first = _make_df(["2026-01-01", "2026-01-02"])
    cache.insert_bars(db_session, "SRAZ26", "DAILY", first)

    second = _make_df(["2026-01-02", "2026-01-03"], seed=200.0)
    n = cache.insert_bars(db_session, "SRAZ26", "DAILY", second)
    assert n == 1  # only 2026-01-03 is new

    out = cache.read_bars(
        db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 3)
    )
    assert len(out) == 3


def test_insert_bars_empty_dataframe_is_noop(db_session):
    empty = pd.DataFrame(columns=_CANONICAL_COLUMNS)
    n = cache.insert_bars(db_session, "SRAZ26", "DAILY", empty)
    assert n == 0


# ---------------------------------------------------------------------
# read_bars
# ---------------------------------------------------------------------

def test_read_bars_filters_by_ric_interval_and_date_range(db_session):
    cache.insert_bars(db_session, "SRAZ26", "DAILY", _make_df(["2026-01-01", "2026-01-05", "2026-01-10"]))
    cache.insert_bars(db_session, "SRAH27", "DAILY", _make_df(["2026-01-05"]))
    cache.insert_bars(db_session, "SRAZ26", "HOURLY", _make_df(["2026-01-05"]))

    out = cache.read_bars(
        db_session, "SRAZ26", "DAILY", datetime(2026, 1, 2), datetime(2026, 1, 10)
    )
    assert len(out) == 2  # only 2026-01-05 and 2026-01-10 for SRAZ26/DAILY
    assert out["Date"].is_monotonic_increasing


def test_read_bars_returns_canonical_empty_frame_when_no_match(db_session):
    out = cache.read_bars(
        db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert out.empty
    assert list(out.columns) == _CANONICAL_COLUMNS


def test_read_bars_output_matches_downloader_canonical_schema(db_session):
    cache.insert_bars(db_session, "SRAZ26", "DAILY", _make_df(["2026-01-01"]))
    out = cache.read_bars(
        db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 1)
    )
    assert str(out["Date"].dtype) == "datetime64[ns]"
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert str(out[col].dtype) == "float64"


# ---------------------------------------------------------------------
# get_sync_ranges / record_sync_range
# ---------------------------------------------------------------------

def test_record_sync_range_inserts_new_range(db_session):
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 10))
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert ranges == [(datetime(2026, 1, 1), datetime(2026, 1, 10))]


def test_record_sync_range_merges_overlapping_ranges(db_session):
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 10))
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 5), datetime(2026, 1, 20))
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert ranges == [(datetime(2026, 1, 1), datetime(2026, 1, 20))]


def test_record_sync_range_merges_ranges_within_one_bar_interval(db_session):
    # DAILY bar_delta is 1 day; a 1-day gap has no room for a missing bar.
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 10))
    cache.record_sync_range(
        db_session, "SRAZ26", "DAILY", datetime(2026, 1, 11), datetime(2026, 1, 20)
    )
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert ranges == [(datetime(2026, 1, 1), datetime(2026, 1, 20))]


def test_record_sync_range_keeps_disjoint_ranges_separate(db_session):
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5))
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 2, 1), datetime(2026, 2, 5))
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert len(ranges) == 2


def test_record_sync_range_large_gap_for_intraday_not_merged(db_session):
    # HOURLY bar_delta is 1 hour; a 2-day gap clearly has room for
    # un-fetched bars and must not be silently merged away.
    cache.record_sync_range(
        db_session, "SRAZ26", "HOURLY", datetime(2026, 1, 1, 0), datetime(2026, 1, 1, 12)
    )
    cache.record_sync_range(
        db_session, "SRAZ26", "HOURLY", datetime(2026, 1, 3, 0), datetime(2026, 1, 3, 12)
    )
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "HOURLY")
    assert len(ranges) == 2


def test_record_sync_range_chained_merge_across_three_ranges(db_session):
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5))
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 16), datetime(2026, 1, 20))
    # This range bridges the two existing ones (each gap <= 1 day).
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 6), datetime(2026, 1, 15))
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert ranges == [(datetime(2026, 1, 1), datetime(2026, 1, 20))]


# ---------------------------------------------------------------------
# bar_delta
# ---------------------------------------------------------------------

def test_bar_delta_known_intervals():
    assert cache.bar_delta("DAILY") == timedelta(days=1)
    assert cache.bar_delta("HOURLY") == timedelta(hours=1)
    assert cache.bar_delta("4H") == timedelta(hours=4)


def test_bar_delta_unknown_interval_raises():
    with pytest.raises(ValueError, match="Unknown interval"):
        cache.bar_delta("WEEKLY")
