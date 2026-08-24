"""
tests/test_multimarket_cache_key_independence.py

Task 6 of the multi-market data-path audit: confirm the EXISTING
database.cache/database.service implementation already guarantees that
(a) the same RIC under two different intervals can never collide, and
(b) two different RICs (e.g. one SOFR, one CORRA) can never collide --
at the real, persistent SQLite layer, not just the in-memory leg_cache
dict in strategy_engine.pricing.

This file changes nothing about the cache: every assertion below
exercises database.service.get_history/database.cache exactly as they
exist today, against an isolated tmp_path-backed SQLite engine (the
same fixture convention as tests/test_service.py), with the relevant
provider-facing download function mocked out so no real network call
is ever attempted. If any assertion here ever fails, it means a real
regression was introduced into the cache layer -- these tests exist to
catch that, not to change today's (already-correct) behavior.

CORRA (core.providers.PROVIDER_ROUTING) now routes to QuantHub rather
than LSEG -- CORRA-RIC calls below mock
database.service.download_history_quanthub instead of
database.service.download_history accordingly. This is a routing-target
change only; the cache-key-independence behavior under test is itself
provider-agnostic and unaffected.
"""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import pytest

from database import cache, service
from database.connection import get_session as _real_get_session

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


@pytest.fixture(autouse=True)
def _route_service_sessions_to_test_engine(monkeypatch, db_engine):
    """Same convention as tests/test_service.py: point database.service
    at the isolated tmp_path test engine instead of the real
    data/oscill8.db, so this file can never touch real cached data."""
    monkeypatch.setattr(service, "get_session", lambda: _real_get_session(db_engine))
    yield


def _df(dates: list[str], level: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": [level] * len(dates),
            "High": [level] * len(dates),
            "Low": [level] * len(dates),
            "Close": [level] * len(dates),
            "Volume": [1000.0] * len(dates),
        }
    )


# ---------------------------------------------------------------------
# (a) Same RIC, different interval -> independent cache entries.
# ---------------------------------------------------------------------

def test_same_ric_different_interval_do_not_share_sync_ranges(mocker, db_session):
    mock_download = mocker.patch(
        "database.service.download_history",
        side_effect=[_df(["2020-01-01", "2020-01-02"], 100.0), _df(["2020-01-01", "2020-01-02"], 999.0)],
    )

    service.get_history("SRAU26", "DAILY", "2020-01-01", "2020-01-02")
    service.get_history("SRAU26", "HOURLY", "2020-01-01", "2020-01-02")

    assert mock_download.call_count == 2
    daily_ranges = cache.get_sync_ranges(db_session, "SRAU26", "DAILY")
    hourly_ranges = cache.get_sync_ranges(db_session, "SRAU26", "HOURLY")
    assert len(daily_ranges) == 1
    assert len(hourly_ranges) == 1
    # Marking DAILY as synced must not also mark HOURLY (or vice versa)
    # for the identical RIC -- each interval's coverage is tracked
    # completely independently.
    assert cache.get_sync_ranges(db_session, "SRAU26", "FOUR_HOUR") == []


def test_same_ric_different_interval_do_not_share_price_bars(mocker, db_session):
    mocker.patch(
        "database.service.download_history",
        side_effect=[_df(["2020-01-01"], 100.0), _df(["2020-01-01"], 999.0)],
    )

    daily = service.get_history("SRAU26", "DAILY", "2020-01-01", "2020-01-01")
    hourly = service.get_history("SRAU26", "HOURLY", "2020-01-01", "2020-01-01")

    # Same RIC, same calendar date, but each interval got its OWN
    # distinct price -- proving the two rows never collided/overwrote
    # each other in price_bars, despite sharing (ric, datetime).
    assert daily.iloc[0]["Close"] == pytest.approx(100.0)
    assert hourly.iloc[0]["Close"] == pytest.approx(999.0)

    # A second read of DAILY must come straight from the cache (no new
    # download) and still return DAILY's own value, not HOURLY's.
    daily_again = service.get_history("SRAU26", "DAILY", "2020-01-01", "2020-01-01")
    assert daily_again.iloc[0]["Close"] == pytest.approx(100.0)


def test_downloading_one_interval_does_not_mark_another_interval_as_synced(mocker, db_session):
    """A cache-miss download for SRAU26/DAILY must never cause a later
    SRAU26/HOURLY request to be (incorrectly) treated as already
    covered -- each interval's sync_ranges row is independent."""
    mocker.patch("database.service.download_history", return_value=_df(["2020-01-01"], 100.0))
    service.get_history("SRAU26", "DAILY", "2020-01-01", "2020-01-01")

    mock_download_hourly = mocker.patch(
        "database.service.download_history", return_value=_df(["2020-01-01"], 999.0)
    )
    service.get_history("SRAU26", "HOURLY", "2020-01-01", "2020-01-01")

    # If HOURLY had incorrectly inherited DAILY's sync_range, this
    # second download would never have been attempted.
    assert mock_download_hourly.call_count == 1


# ---------------------------------------------------------------------
# (b) Different RICs (SOFR vs CORRA) -> independent cache entries.
# ---------------------------------------------------------------------

def test_different_rics_same_interval_do_not_share_sync_ranges_or_bars(mocker, db_session):
    mocker.patch("database.service.download_history", return_value=_df(["2020-01-01"], 100.0))
    mocker.patch(
        "database.service.download_history_quanthub", return_value=_df(["2020-01-01"], 200.0)
    )

    sofr = service.get_history("SRAU26", "DAILY", "2020-01-01", "2020-01-01")
    corra = service.get_history("CRAU6", "DAILY", "2020-01-01", "2020-01-01")

    assert sofr.iloc[0]["Close"] == pytest.approx(100.0)
    assert corra.iloc[0]["Close"] == pytest.approx(200.0)

    assert len(cache.get_sync_ranges(db_session, "SRAU26", "DAILY")) == 1
    assert len(cache.get_sync_ranges(db_session, "CRAU6", "DAILY")) == 1
    # Requesting SOFR's own cached range must never surface CORRA's rows.
    window_start = datetime.combine(datetime(2020, 1, 1).date(), time.min)
    window_end = datetime.combine(datetime(2020, 1, 1).date(), time.max)
    sofr_bars = cache.read_bars(db_session, "SRAU26", "DAILY", window_start, window_end)
    corra_bars = cache.read_bars(db_session, "CRAU6", "DAILY", window_start, window_end)
    assert len(sofr_bars) == 1 and sofr_bars.iloc[0]["Close"] == pytest.approx(100.0)
    assert len(corra_bars) == 1 and corra_bars.iloc[0]["Close"] == pytest.approx(200.0)


def test_caching_corra_does_not_satisfy_a_later_sofr_request(mocker, db_session):
    """Direct regression for the audit's exact concern: caching CORRA
    history must never be mistaken for cached SOFR coverage, or vice
    versa -- each RIC always triggers its own download."""
    mock_download = mocker.patch(
        "database.service.download_history_quanthub", return_value=_df(["2020-01-01"], 200.0)
    )
    service.get_history("CRAU6", "DAILY", "2020-01-01", "2020-01-01")
    assert mock_download.call_count == 1

    mock_download_sofr = mocker.patch(
        "database.service.download_history", return_value=_df(["2020-01-01"], 100.0)
    )
    service.get_history("SRAU26", "DAILY", "2020-01-01", "2020-01-01")
    assert mock_download_sofr.call_count == 1  # a fresh download, not a cache hit off CORRA's range


# ---------------------------------------------------------------------
# Combined: RIC x interval matrix -- four independent cells (2 RICs x
# 2 intervals) must never cross-populate each other.
# ---------------------------------------------------------------------

def test_ric_interval_matrix_is_fully_independent(mocker, db_session):
    matrix = {
        ("SRAU26", "DAILY"): 1.0,
        ("SRAU26", "HOURLY"): 2.0,
        ("CRAU6", "DAILY"): 3.0,
        ("CRAU6", "HOURLY"): 4.0,
    }
    mocker.patch(
        "database.service.download_history",
        side_effect=[_df(["2020-01-01"], matrix[("SRAU26", "DAILY")]), _df(["2020-01-01"], matrix[("SRAU26", "HOURLY")])],
    )
    mocker.patch(
        "database.service.download_history_quanthub",
        side_effect=[_df(["2020-01-01"], matrix[("CRAU6", "DAILY")]), _df(["2020-01-01"], matrix[("CRAU6", "HOURLY")])],
    )

    results = {
        key: service.get_history(key[0], key[1], "2020-01-01", "2020-01-01")
        for key in matrix
    }

    for key, expected_level in matrix.items():
        assert results[key].iloc[0]["Close"] == pytest.approx(expected_level), (
            f"{key} returned the wrong level -- possible cache-key collision"
        )

    # 4 distinct sync_ranges rows -- one per (ric, interval) cell, none merged.
    for ric, interval in matrix:
        assert len(cache.get_sync_ranges(db_session, ric, interval)) == 1
