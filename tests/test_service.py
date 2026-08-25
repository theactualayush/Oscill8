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
from core.downloader import MarketDataUnavailableError
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


def test_get_history_persists_and_returns_a_partial_ohlc_bar(mocker, db_session):
    # Regression, end-to-end: a bar with a legitimately missing field
    # (e.g. an HOURLY bar with no trade printed) must not blow up
    # persistence, and must still be marked as synced so it isn't
    # re-downloaded forever.
    partial = _make_df(["2020-01-01"])
    partial.loc[0, "Open"] = float("nan")
    mock_download = mocker.patch("database.service.download_history", return_value=partial)

    result = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-01")

    assert mock_download.call_count == 1
    assert len(result) == 1
    assert pd.isna(result["Open"].iloc[0])
    assert result["Close"].iloc[0] == pytest.approx(partial["Close"].iloc[0])

    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "DAILY")
    assert len(ranges) == 1  # still marked as synced despite the missing field

    mock_download.reset_mock()
    cached = service.get_history("SRAZ26", "DAILY", "2020-01-01", "2020-01-01")
    assert mock_download.call_count == 0  # served from cache, not re-downloaded
    assert pd.isna(cached["Open"].iloc[0])


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


def test_get_history_propagates_market_data_unavailable_error_unchanged(mocker, db_session):
    # No Module 2 handling for this exception -- it's typed and raised
    # entirely inside core.downloader (Module 5B.1); get_history must
    # propagate it exactly like any other download_history exception.
    mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError("SRAH26", "The universe is not found"),
    )

    with pytest.raises(MarketDataUnavailableError) as exc_info:
        service.get_history("SRAH26", "DAILY", "2020-01-01", "2020-01-10")

    assert exc_info.value.ric == "SRAH26"
    ranges = cache.get_sync_ranges(db_session, "SRAH26", "DAILY")
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
# get_history: only fully closed bars are ever fetched, cached, or
# returned -- the still-forming period is excluded from the request
# entirely, and a second identical call during the same still-forming
# period makes zero further provider requests (see
# database.service._effective_request_end).
# ---------------------------------------------------------------------

def test_get_history_in_progress_bar_never_requested_cached_or_returned(mocker, db_session):
    frozen_now = datetime(2026, 6, 15, 10, 30)

    class _FrozenDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return frozen_now

    mocker.patch.object(service, "datetime", _FrozenDateTime)

    # A plain date request (matching the real-world scan pattern that
    # produced the live-observed bug) -- _coerce_end -> day-end
    # 23:59:59.999999, uncapped until _effective_request_end narrows it.
    #
    # The provider mock deliberately still hands back an in-progress bar
    # (Date=10:00, boundary is 10:00) EVEN THOUGH the request itself
    # should be capped below it -- this proves both halves of the fix:
    # the outbound request is narrower, AND anything that sneaks past
    # that (e.g. QuantHub, which cannot be asked to exclude a same-day
    # in-progress bar at all) is still filtered out before caching/
    # returning.
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

    result = service.get_history("SRAZ26", "HOURLY", "2026-06-15", "2026-06-15")

    # The request itself is capped BELOW the boundary -- never all the
    # way to day-end, always 00:00:00->09:59:59.999999.
    assert mock_download.call_count == 1
    called_start, called_end = mock_download.call_args[0][2], mock_download.call_args[0][3]
    assert called_start == datetime(2026, 6, 15, 0, 0)
    assert called_end == datetime(2026, 6, 15, 9, 59, 59, 999999)

    # The in-progress bar is NOT returned to the caller -- only the 2
    # completed bars.
    assert len(result) == 2
    assert result["Date"].max() == datetime(2026, 6, 15, 9, 0)

    # And only the 2 completed bars were actually persisted.
    persisted = cache.read_bars(
        db_session, "SRAZ26", "HOURLY", datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 23, 59, 59)
    )
    assert len(persisted) == 2

    # Coverage was only marked synced up through the boundary.
    ranges = cache.get_sync_ranges(db_session, "SRAZ26", "HOURLY")
    assert len(ranges) == 1
    assert ranges[0][1] <= datetime(2026, 6, 15, 10, 0)

    # THE CORE OF THE FIX: a second identical call during the SAME
    # still-forming period makes ZERO further provider requests -- the
    # already-synced [00:00, 09:59:59.999999] fully covers the capped
    # effective window, so there is nothing left to fetch.
    service.get_history("SRAZ26", "HOURLY", "2026-06-15", "2026-06-15")
    assert mock_download.call_count == 1


def test_get_history_newly_closed_bar_fetched_exactly_once_after_period_closes(mocker, db_session):
    """Once the previously-forming period closes, the NEXT call fetches
    exactly that newly-completed bar -- and only that bar. Uses a plain
    date (not a specific datetime) for `end`, matching the real-world
    scan pattern that produced the live-observed bug (_coerce_end ->
    day-end 23:59:59.999999), so the boundary cap is always the binding
    constraint regardless of what hour "now" happens to be."""
    hourly_bars_before_close = pd.DataFrame(
        {
            "Date": [datetime(2026, 6, 15, 8, 0), datetime(2026, 6, 15, 9, 0)],
            "Open": [100.0, 101.0], "High": [101.0, 102.0],
            "Low": [99.0, 100.0], "Close": [100.5, 101.5], "Volume": [10, 11],
        }
    )
    mocker.patch("database.service.download_history", return_value=hourly_bars_before_close)

    class _At1030(datetime):
        @classmethod
        def utcnow(cls):
            return datetime(2026, 6, 15, 10, 30)

    mocker.patch.object(service, "datetime", _At1030)
    service.get_history("SRAZ26", "HOURLY", "2026-06-15", "2026-06-15")
    # Cached through 09:59:59.999999 -- the 10:00 hour hasn't closed yet.
    assert cache.get_sync_ranges(db_session, "SRAZ26", "HOURLY")[0][1] <= datetime(2026, 6, 15, 10, 0)

    # Now the 10:00 hour has closed (it's 11:05); the provider returns
    # just that one newly-closed bar.
    newly_closed_bar = pd.DataFrame(
        {
            "Date": [datetime(2026, 6, 15, 10, 0)],
            "Open": [102.0], "High": [103.0], "Low": [101.0], "Close": [102.5], "Volume": [12],
        }
    )
    mock_download = mocker.patch(
        "database.service.download_history", return_value=newly_closed_bar
    )

    class _At1105(datetime):
        @classmethod
        def utcnow(cls):
            return datetime(2026, 6, 15, 11, 5)

    mocker.patch.object(service, "datetime", _At1105)
    result = service.get_history("SRAZ26", "HOURLY", "2026-06-15", "2026-06-15")

    # Exactly one request, for exactly the newly-closed 10:00 bar.
    assert mock_download.call_count == 1
    called_start, called_end = mock_download.call_args[0][2], mock_download.call_args[0][3]
    assert called_start == datetime(2026, 6, 15, 10, 0)
    assert called_end == datetime(2026, 6, 15, 10, 59, 59, 999999)

    assert len(result) == 3
    assert result["Date"].max() == datetime(2026, 6, 15, 10, 0)
