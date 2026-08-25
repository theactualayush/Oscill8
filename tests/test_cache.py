"""
tests/test_cache.py

Unit tests for database/cache.py: bar upsert/dedup, range reads, and
sync-range bookkeeping (including bar-frequency-aware merging).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from database import cache
from database.models import PriceBar

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
# insert_bars: missing OHLCV fields (regression for the real HOURLY
# SRAZ26 bug -- TypeError: float() argument ... not 'NAType')
# ---------------------------------------------------------------------

def test_insert_bars_persists_np_nan_as_sql_null(db_session):
    df = _make_df(["2026-07-01"])
    df.loc[0, "Open"] = np.nan

    n = cache.insert_bars(db_session, "SRAZ26", "HOURLY", df)
    assert n == 1

    row = db_session.execute(select(PriceBar).where(PriceBar.ric == "SRAZ26")).scalar_one()
    assert row.open is None  # SQL NULL, not a stored NaN float


def test_insert_bars_persists_pd_na_as_sql_null(db_session):
    df = _make_df(["2026-07-01"])
    df["High"] = pd.array([pd.NA], dtype="Float64")

    n = cache.insert_bars(db_session, "SRAZ26", "HOURLY", df)
    assert n == 1

    row = db_session.execute(select(PriceBar).where(PriceBar.ric == "SRAZ26")).scalar_one()
    assert row.high is None


def test_insert_bars_persists_none_as_sql_null(db_session):
    df = _make_df(["2026-07-01"])
    df["Low"] = df["Low"].astype(object)
    df.loc[0, "Low"] = None

    n = cache.insert_bars(db_session, "SRAZ26", "HOURLY", df)
    assert n == 1

    row = db_session.execute(select(PriceBar).where(PriceBar.ric == "SRAZ26")).scalar_one()
    assert row.low is None


def test_insert_bars_real_number_still_persists_normally(db_session):
    df = _make_df(["2026-07-01"], seed=100.0)
    cache.insert_bars(db_session, "SRAZ26", "HOURLY", df)

    row = db_session.execute(select(PriceBar).where(PriceBar.ric == "SRAZ26")).scalar_one()
    assert row.open == pytest.approx(100.0)


def test_insert_bars_partial_ohlc_bar_with_valid_close_is_kept(db_session):
    # The exact real-world shape from the bug report: an hourly bar with
    # no trade printed (Open/High/Low missing) but a valid Close.
    df = _make_df(["2026-07-01"])
    df.loc[0, ["Open", "High", "Low"]] = np.nan

    n = cache.insert_bars(db_session, "SRAZ26", "HOURLY", df)
    assert n == 1  # the bar is kept, not dropped

    out = cache.read_bars(
        db_session, "SRAZ26", "HOURLY", datetime(2026, 7, 1), datetime(2026, 7, 1)
    )
    assert len(out) == 1
    assert np.isnan(out["Open"].iloc[0])
    assert np.isnan(out["High"].iloc[0])
    assert np.isnan(out["Low"].iloc[0])
    assert out["Close"].iloc[0] == pytest.approx(df["Close"].iloc[0])


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


def test_read_bars_sql_null_round_trips_to_np_nan_float64(db_session):
    # Insert a row with a genuine SQL NULL directly via the ORM (bypassing
    # insert_bars entirely) so this isolates read_bars' own reconstruction
    # behavior: SQL NULL must come back as np.nan in a float64 column, not
    # None/object dtype/pd.NA.
    db_session.add(
        PriceBar(
            ric="SRAZ26",
            interval="HOURLY",
            datetime=datetime(2026, 7, 1, 9, 0),
            open=None,
            high=100.5,
            low=None,
            close=100.2,
            volume=None,
        )
    )
    db_session.commit()

    out = cache.read_bars(
        db_session, "SRAZ26", "HOURLY", datetime(2026, 7, 1), datetime(2026, 7, 1, 23, 59, 59)
    )

    assert len(out) == 1
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert out[col].dtype == np.float64
    assert np.isnan(out["Open"].iloc[0])
    assert np.isnan(out["Low"].iloc[0])
    assert np.isnan(out["Volume"].iloc[0])
    assert out["High"].iloc[0] == pytest.approx(100.5)
    assert out["Close"].iloc[0] == pytest.approx(100.2)


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
# Provider provenance: record_sync_range(provider=...) / get_established_provider
# ---------------------------------------------------------------------

def test_record_sync_range_without_provider_defaults_to_none(db_session):
    # Every existing caller (a market with no QuantHub mapping) omits
    # `provider` -- must remain fully backward compatible.
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5))
    assert cache.get_established_provider(db_session, "SRAZ26", "DAILY") is None


def test_record_sync_range_persists_provider_lseg(db_session):
    cache.record_sync_range(
        db_session, "SONH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5), provider="LSEG"
    )
    assert cache.get_established_provider(db_session, "SONH26", "DAILY") == "LSEG"


def test_record_sync_range_persists_provider_quanthub(db_session):
    cache.record_sync_range(
        db_session, "SONH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5), provider="QUANTHUB"
    )
    assert cache.get_established_provider(db_session, "SONH26", "DAILY") == "QUANTHUB"


def test_get_established_provider_returns_none_for_untouched_ric_interval(db_session):
    assert cache.get_established_provider(db_session, "NEVER26", "DAILY") is None


def test_provider_provenance_is_keyed_by_ric_and_interval_together(db_session):
    # The SAME contract can legitimately have different established
    # providers at different intervals.
    cache.record_sync_range(
        db_session, "SONH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5), provider="LSEG"
    )
    cache.record_sync_range(
        db_session, "SONH26", "HOURLY", datetime(2026, 1, 1), datetime(2026, 1, 1, 5), provider="QUANTHUB"
    )
    assert cache.get_established_provider(db_session, "SONH26", "DAILY") == "LSEG"
    assert cache.get_established_provider(db_session, "SONH26", "HOURLY") == "QUANTHUB"


def test_record_sync_range_merge_keeps_the_incoming_calls_provider(db_session):
    cache.record_sync_range(
        db_session, "SONH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5), provider="LSEG"
    )
    # Adjacent range, same provider (as it must always be, by design) --
    # merges into one row, provider still LSEG.
    cache.record_sync_range(
        db_session, "SONH26", "DAILY", datetime(2026, 1, 6), datetime(2026, 1, 10), provider="LSEG"
    )
    ranges = cache.get_sync_ranges(db_session, "SONH26", "DAILY")
    assert ranges == [(datetime(2026, 1, 1), datetime(2026, 1, 10))]
    assert cache.get_established_provider(db_session, "SONH26", "DAILY") == "LSEG"


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


# ---------------------------------------------------------------------
# delete_bars_and_sync_ranges: cache invalidation ahead of a provider
# switch (cache -> LSEG -> QuantHub fallback, database/service.py)
# ---------------------------------------------------------------------

def test_delete_bars_removes_bars_in_range(db_session):
    df = _make_df(["2026-01-01", "2026-01-02", "2026-01-03"])
    cache.insert_bars(db_session, "CRAH26", "DAILY", df)

    deleted = cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59)
    )

    assert deleted == 3
    remaining = cache.read_bars(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59)
    )
    assert remaining.empty


def test_delete_bars_leaves_bars_outside_the_range_untouched(db_session):
    df = _make_df(["2026-01-01", "2026-01-05", "2026-01-10"])
    cache.insert_bars(db_session, "CRAH26", "DAILY", df)

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 4), datetime(2026, 1, 6)
    )

    remaining = cache.read_bars(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 10, 23, 59, 59)
    )
    assert sorted(remaining["Date"].dt.strftime("%Y-%m-%d")) == ["2026-01-01", "2026-01-10"]


def test_delete_bars_never_touches_a_different_ric(db_session):
    cache.insert_bars(db_session, "CRAH26", "DAILY", _make_df(["2026-01-01"]))
    cache.insert_bars(db_session, "SRAH26", "DAILY", _make_df(["2026-01-01"]))

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59, 59)
    )

    assert cache.read_bars(
        db_session, "SRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59, 59)
    ).shape[0] == 1


def test_delete_bars_never_touches_a_different_interval(db_session):
    cache.insert_bars(db_session, "CRAH26", "DAILY", _make_df(["2026-01-01"]))
    cache.insert_bars(db_session, "CRAH26", "HOURLY", _make_df(["2026-01-01"]))

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59, 59)
    )

    assert cache.read_bars(
        db_session, "CRAH26", "HOURLY", datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59, 59)
    ).shape[0] == 1


def test_delete_sync_range_fully_inside_window_is_removed(db_session):
    cache.record_sync_range(db_session, "CRAH26", "DAILY", datetime(2026, 1, 5), datetime(2026, 1, 10))

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 20)
    )

    assert cache.get_sync_ranges(db_session, "CRAH26", "DAILY") == []


def test_delete_sync_range_not_overlapping_window_is_untouched(db_session):
    cache.record_sync_range(db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 3))

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 2, 1), datetime(2026, 2, 5)
    )

    ranges = cache.get_sync_ranges(db_session, "CRAH26", "DAILY")
    assert ranges == [(datetime(2026, 1, 1), datetime(2026, 1, 3))]


def test_delete_sync_range_trims_the_left_overlap(db_session):
    # Existing range starts before the deletion window and ends inside
    # it -- must be trimmed to stop just before the deletion window.
    cache.record_sync_range(db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 10))

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 5), datetime(2026, 1, 20)
    )

    ranges = cache.get_sync_ranges(db_session, "CRAH26", "DAILY")
    assert len(ranges) == 1
    assert ranges[0][0] == datetime(2026, 1, 1)
    assert ranges[0][1] < datetime(2026, 1, 5)


def test_delete_sync_range_trims_the_right_overlap(db_session):
    # Existing range starts inside the deletion window and ends after
    # it -- must be trimmed to start just after the deletion window.
    cache.record_sync_range(db_session, "CRAH26", "DAILY", datetime(2026, 1, 5), datetime(2026, 1, 20))

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 10)
    )

    ranges = cache.get_sync_ranges(db_session, "CRAH26", "DAILY")
    assert len(ranges) == 1
    assert ranges[0][0] > datetime(2026, 1, 10)
    assert ranges[0][1] == datetime(2026, 1, 20)


def test_delete_sync_range_splits_a_row_that_fully_contains_the_window(db_session):
    # Existing range fully contains the deletion window -- must survive
    # as TWO separate rows, one on each side, never deleted wholesale.
    cache.record_sync_range(db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 31))

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 10), datetime(2026, 1, 20)
    )

    ranges = sorted(cache.get_sync_ranges(db_session, "CRAH26", "DAILY"))
    assert len(ranges) == 2
    assert ranges[0][0] == datetime(2026, 1, 1)
    assert ranges[0][1] < datetime(2026, 1, 10)
    assert ranges[1][0] > datetime(2026, 1, 20)
    assert ranges[1][1] == datetime(2026, 1, 31)


def test_delete_sync_range_split_preserves_provider_on_both_surviving_fragments(db_session):
    # A partial reset must not erase provenance for the portion of
    # coverage NOT being reset.
    cache.record_sync_range(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 31), provider="LSEG"
    )

    cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 10), datetime(2026, 1, 20)
    )

    ranges = cache.get_sync_ranges(db_session, "CRAH26", "DAILY")
    assert len(ranges) == 2
    assert cache.get_established_provider(db_session, "CRAH26", "DAILY") == "LSEG"


def test_delete_bars_and_sync_ranges_returns_deleted_bar_count(db_session):
    cache.insert_bars(db_session, "CRAH26", "DAILY", _make_df(["2026-01-01", "2026-01-02"]))

    deleted = cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 2, 23, 59, 59)
    )

    assert deleted == 2


def test_delete_bars_and_sync_ranges_on_empty_cache_is_a_safe_no_op(db_session):
    deleted = cache.delete_bars_and_sync_ranges(
        db_session, "CRAH26", "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 2)
    )
    assert deleted == 0
    assert cache.get_sync_ranges(db_session, "CRAH26", "DAILY") == []
