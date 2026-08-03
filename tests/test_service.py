"""
tests/test_service.py

Unit tests for database/service.py: the pure _missing_ranges helper, and
integration-level tests of get_history against a real (tmp_path-backed)
SQLite cache with core.downloader.download_history mocked out --
following the same "fake the LSEG-facing call" approach as
tests/test_downloader.py, just at the download_history boundary instead
of lseg.data itself, since download_history is a plain importable
function here rather than a lazily-imported submodule.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from core.config import BarInterval
from database import cache, service
from database.connection import get_session as _real_get_session

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


@pytest.fixture(autouse=True)
def _route_service_sessions_to_test_engine(monkeypatch, db_engine):
    """Make service.get_history use the tmp_path test engine, not the
    real default (core.config.SQLITE_DB_PATH) database."""
    monkeypatch.setattr(service, "get_session", lambda: _real_get_session(db_engine))
    yield


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
# _missing_ranges: pure function, no DB/mocking needed
# ---------------------------------------------------------------------

S = datetime(2026, 1, 1)
E = datetime(2026, 1, 20)


def test_missing_ranges_no_existing_coverage():
    assert service._missing_ranges([], S, E) == [(S, E)]


def test_missing_ranges_fully_covered():
    assert service._missing_ranges([(S, E)], S, E) == []


def test_missing_ranges_leading_gap():
    result = service._missing_ranges([(S + timedelta(days=5), E)], S, E)
    assert result == [(S, S + timedelta(days=5))]


def test_missing_ranges_trailing_gap():
    result = service._missing_ranges([(S, E - timedelta(days=5))], S, E)
    assert len(result) == 1
    assert result[0][1] == E


def test_missing_ranges_gap_in_the_middle():
    existing = [(S, S + timedelta(days=3)), (S + timedelta(days=10), E)]
    result = service._missing_ranges(existing, S, E)
    assert len(result) == 1
    assert result[0][1] == S + timedelta(days=10)


def test_missing_ranges_wider_than_all_known_ranges():
    existing = [(S + timedelta(days=5), E - timedelta(days=5))]
    result = service._missing_ranges(existing, S, E)
    assert len(result) == 2
    assert result[0][0] == S
    assert result[1][1] == E


def test_missing_ranges_exact_boundary_touching_no_gap():
    assert service._missing_ranges([(S, E)], S, E) == []


# ---------------------------------------------------------------------
# get_history: no cache
# ---------------------------------------------------------------------

def test_get_history_no_cache_downloads_full_range_and_persists(mocker, db_session):
    mock_download = mocker.patch(
        "database.service.download_history",
        return_value=_make_df(["2020-01-01", "2020-01-02", "2020-01-03"]),
    )

    result = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-03")

    assert mock_download.call_count == 1
    assert list(result.columns) == _CANONICAL_COLUMNS
    assert len(result) == 3

    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert len(ranges) == 1


# ---------------------------------------------------------------------
# get_history: fully cached
# ---------------------------------------------------------------------

def test_get_history_fully_cached_does_not_call_downloader(mocker, db_session):
    seeded = _make_df(["2020-01-01", "2020-01-02", "2020-01-03"])
    cache.insert_bars(db_session, "SRAZ26", "DAILY", seeded)
    cache.record_sync_range(
        db_session, "SRAZ26", "DAILY", datetime(2020, 1, 1), datetime(2020, 1, 3, 23, 59, 59, 999999)
    )

    mock_download = mocker.patch("database.service.download_history")

    result = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-03")

    assert mock_download.call_count == 0
    assert len(result) == 3


# ---------------------------------------------------------------------
# get_history: partial coverage (leading / trailing / both gaps)
# ---------------------------------------------------------------------

def test_get_history_leading_gap_downloads_only_missing_prefix(mocker, db_session):
    cache.insert_bars(db_session, "SRAZ26", "DAILY", _make_df(["2020-01-10"]))
    cache.record_sync_range(
        db_session, "SRAZ26", "DAILY", datetime(2020, 1, 10), datetime(2020, 1, 20, 23, 59, 59, 999999)
    )

    mock_download = mocker.patch(
        "database.service.download_history",
        return_value=_make_df(["2020-01-01", "2020-01-05"]),
    )

    result = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-20")

    assert mock_download.call_count == 1
    call_args = mock_download.call_args
    assert call_args[0][0] == "SRAZ26"
    assert call_args[0][2] == datetime(2020, 1, 1)  # sub-range start == requested start
    assert len(result) == 3  # 2 newly downloaded + 1 previously cached


def test_get_history_trailing_gap_downloads_only_missing_suffix(mocker, db_session):
    cache.insert_bars(db_session, "SRAZ26", "DAILY", _make_df(["2020-01-01"]))
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2020, 1, 1), datetime(2020, 1, 10))

    mock_download = mocker.patch(
        "database.service.download_history",
        return_value=_make_df(["2020-01-15", "2020-01-20"]),
    )

    result = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-20")

    assert mock_download.call_count == 1
    assert len(result) == 3


def test_get_history_both_gaps_downloads_two_subranges(mocker, db_session):
    cache.insert_bars(db_session, "SRAZ26", "DAILY", _make_df(["2020-01-10"]))
    cache.record_sync_range(db_session, "SRAZ26", "DAILY", datetime(2020, 1, 10), datetime(2020, 1, 10))

    mock_download = mocker.patch(
        "database.service.download_history",
        side_effect=[
            _make_df(["2020-01-01"]),
            _make_df(["2020-01-20"]),
        ],
    )

    result = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-20")

    assert mock_download.call_count == 2
    assert len(result) == 3


# ---------------------------------------------------------------------
# get_history: failure handling
# ---------------------------------------------------------------------

def test_get_history_downloader_failure_does_not_record_partial_sync_range(mocker, db_session):
    mocker.patch("database.service.download_history", side_effect=ConnectionError("boom"))

    with pytest.raises(ConnectionError):
        service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-10")

    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert ranges == []


def test_get_history_empty_downloader_result_still_records_sync_range(mocker, db_session):
    mocker.patch(
        "database.service.download_history",
        return_value=pd.DataFrame(columns=_CANONICAL_COLUMNS),
    )

    result = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-10")

    assert result.empty
    assert list(result.columns) == _CANONICAL_COLUMNS
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert len(ranges) == 1  # known-empty history should not be re-fetched every call


# ---------------------------------------------------------------------
# get_history: flexible inputs
# ---------------------------------------------------------------------

def test_get_history_accepts_str_or_barinterval_and_datelike_inputs(mocker):
    mocker.patch(
        "database.service.download_history",
        return_value=_make_df(["2020-01-01"]),
    )
    df1 = service.get_history("SRAZ26", BarInterval.DAILY, "2020-01-01", "2020-01-01")
    df2 = service.get_history("SRAZ27", "DAILY", datetime(2020, 1, 1), datetime(2020, 1, 1))
    assert len(df1) == 1
    assert len(df2) == 1


# ---------------------------------------------------------------------
# get_history: in-progress bar is never cached, but is returned; the
# still-open period is re-fetched on the next call.
# ---------------------------------------------------------------------

def test_get_history_in_progress_bar_not_cached_but_still_returned(mocker, db_session):
    frozen_now = datetime(2026, 6, 15, 10, 30)

    class _FrozenDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return frozen_now

    mocker.patch.object(service, "datetime", _FrozenDateTime)

    hourly_bars = pd.DataFrame(
        {
            "Date": [
                datetime(2026, 6, 15, 8, 0),
                datetime(2026, 6, 15, 9, 0),
                datetime(2026, 6, 15, 10, 0),  # in progress: boundary is 10:00
            ],
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [10, 11, 12],
        }
    )
    mock_download = mocker.patch(
        "database.service.download_history", return_value=hourly_bars
    )

    result = service.get_history(
        "SRAZ26", "HOURLY", datetime(2026, 6, 15, 8, 0), datetime(2026, 6, 15, 10, 59)
    )

    # All 3 bars (including the in-progress one) are returned to the caller.
    assert len(result) == 3

    # But only the 2 completed bars were actually persisted.
    persisted = cache.read_bars(
        db_session, "SRAZ26", "HOURLY", datetime(2026, 6, 15, 8, 0), datetime(2026, 6, 15, 10, 59)
    )
    assert len(persisted) == 2

    # And coverage was only marked synced up through the boundary.
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "HOURLY")
    assert len(ranges) == 1
    assert ranges[0][1] <= datetime(2026, 6, 15, 10, 0)

    # A second call for the same window must re-fetch the still-open tail.
    service.get_history(
        "SRAZ26", "HOURLY", datetime(2026, 6, 15, 8, 0), datetime(2026, 6, 15, 10, 59)
    )
    assert mock_download.call_count == 2
