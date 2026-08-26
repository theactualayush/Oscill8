"""
tests/test_service_provider_fallback.py

Focused tests for the provider-PROVENANCE design in database/service.py
(_establish_provider_and_fetch / _fetch_established_quanthub /
_get_history_batch_with_provenance / _is_complete_history) and its
persistence in database/cache.py (record_sync_range's `provider` column,
get_established_provider):

    - A QuantHub-mapped market's (ric, interval) provider is decided
      EXACTLY ONCE ("establishment"): LSEG is tried first for the full
      requested window; if its result is complete, LSEG is established
      as the permanent provider; otherwise LSEG's result is discarded
      entirely and QuantHub is established instead.
    - Once established, the decision is NEVER re-evaluated. An
      established-LSEG (ric, interval) fetches ONLY its missing
      sub-range(s) from LSEG on every later call -- this is the fix for
      the regression where a QuantHub-mapped market's incomplete cache
      re-downloaded the ENTIRE requested window from LSEG instead of
      just the gap. An established-QuantHub (ric, interval) always
      re-requests the FULL window from QuantHub whenever anything is
      missing -- a QuantHub API limitation (no start/end/pagination),
      not a design choice.
    - NEVER a mix of LSEG and QuantHub bars for one (ric, interval).
    - Provider is keyed on (ric, interval) together -- the same
      contract can have different established providers at different
      intervals.
    - cache.delete_bars_and_sync_ranges() is an administrative/reset
      utility: it clears provider provenance too, so the next request
      performs fresh LSEG-first discovery again.
    - QuantHub batching (QUANTHUB_BATCH_SIZE / QUANTHUB_MAX_ROWS_PER_
      REQUEST) is fully preserved, and get_history_batch() applies this
      exact same per-(ric, interval) provenance decision to many RICs.
    - A market with no QuantHub mapping is completely unaffected.

Follows the same "mock the provider-facing download function, use a
tmp_path-backed SQLite cache" approach as tests/test_service.py.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from core.config import BarInterval
from core.downloader import MarketDataUnavailableError
from core.providers import Provider
from core.quanthub import QuantHubRateLimitError, QUANTHUB_BATCH_SIZE, QUANTHUB_MAX_ROWS_PER_REQUEST
from core.ric import build_ric
from database import cache, service
from database.connection import get_session as _real_get_session

_CORRA_H26 = build_ric("CORRA", 3, 2026)
_CORRA_M26 = build_ric("CORRA", 6, 2026)
_CORRA_U26 = build_ric("CORRA", 9, 2026)
_SONIA_H26 = build_ric("SONIA", 3, 2026)
_SOFR_H26 = build_ric("SOFR", 3, 2026)


@pytest.fixture(autouse=True)
def _route_service_sessions_to_test_engine(monkeypatch, db_engine):
    monkeypatch.setattr(service, "get_session", lambda: _real_get_session(db_engine))
    yield


def _bars(dates: list[str], seed: float = 100.0) -> pd.DataFrame:
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


def _daily_dates(start: str, n: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, periods=n, freq="D")]


def _seed_established(db_session, ric: str, interval: str, start: datetime, end: datetime, provider: str):
    """Seed a (ric, interval) as already-established for `provider`,
    with cached bars covering exactly [start, end] -- the state a real
    prior get_history() call would have left behind."""
    dates = pd.date_range(start, end, freq="D").strftime("%Y-%m-%d").tolist()
    cache.insert_bars(db_session, ric, interval, _bars(dates, seed=1.0))
    cache.record_sync_range(db_session, ric, interval, start, end, provider=provider)


def _qh_batch_side_effect(instruments, interval, start, end):
    return {instr: _bars(["2026-01-01", "2026-01-02"], seed=500.0) for instr in instruments}


# ---------------------------------------------------------------------
# A. Establishment -- LSEG complete on the first-ever request
# ---------------------------------------------------------------------

def test_a_first_request_lseg_complete_establishes_lseg(mocker, db_session):
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(_daily_dates("2026-01-01", 5)),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-05")

    assert mock_lseg.call_count == 1
    mock_qh.assert_not_called()
    assert len(result) == 5
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.LSEG.value

    reread = cache.read_bars(
        db_session, _CORRA_H26, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5, 23, 59, 59, 999999)
    )
    assert len(reread) == 5


# ---------------------------------------------------------------------
# B. Establishment -- LSEG incomplete/unavailable on the first-ever
#    request establishes QuantHub; the incomplete LSEG data is NEVER
#    persisted.
# ---------------------------------------------------------------------

def test_b_first_request_lseg_unavailable_establishes_quanthub(mocker, db_session):
    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(_CORRA_H26, "The universe is not found"),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-01-01", 3), seed=500.0),
    )

    result = service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-03")

    assert mock_lseg.call_count == 1
    assert mock_qh.call_count == 1
    assert all(close >= 500.0 for close in result["Close"])
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.QUANTHUB.value

    reread = cache.read_bars(
        db_session, _CORRA_H26, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999)
    )
    assert all(close >= 500.0 for close in reread["Close"])
    assert len(reread) == 3


def test_b_first_request_lseg_incomplete_frame_establishes_quanthub_lseg_data_discarded(mocker, db_session):
    gappy = pd.concat(
        [_bars(["2026-01-01"], seed=1.0), _bars(["2026-03-01"], seed=2.0)], ignore_index=True
    )
    mocker.patch("database.service.download_history", return_value=gappy)
    mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-01-01", 3), seed=500.0),
    )

    result = service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-03-01")

    # Every returned value came from the QH mock's seed (500.x), never
    # LSEG's (1.0 or 2.0) -- LSEG's incomplete result was discarded
    # entirely, not merged with QuantHub's.
    assert all(close >= 500.0 for close in result["Close"])
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.QUANTHUB.value


# ---------------------------------------------------------------------
# B2. Establishment completeness edge cases (_is_complete_history)
# ---------------------------------------------------------------------

def test_b2_short_holiday_style_gap_stays_complete_establishes_lseg(mocker, db_session):
    dates = _daily_dates("2026-01-01", 5) + _daily_dates("2026-01-12", 5)
    mock_lseg = mocker.patch("database.service.download_history", return_value=_bars(dates))
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-16")

    assert mock_lseg.call_count == 1
    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.LSEG.value


def test_b2_expired_contract_trailing_shortfall_still_establishes_lseg(mocker, db_session):
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(_daily_dates("2026-01-01", 5)),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-06-30")

    assert mock_lseg.call_count == 1
    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.LSEG.value


def test_b2_newly_listed_contract_leading_shortfall_still_establishes_lseg(mocker):
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(_daily_dates("2026-05-01", 5)),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-06-30")

    assert mock_lseg.call_count == 1
    mock_qh.assert_not_called()


def test_b2_empty_lseg_result_is_incomplete_establishes_quanthub(mocker, db_session):
    mocker.patch(
        "database.service.download_history",
        return_value=pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"]),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(["2026-01-01"], seed=500.0),
    )

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-01")

    assert mock_qh.call_count == 1
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.QUANTHUB.value


def test_b2_lseg_interior_gap_wider_than_threshold_establishes_quanthub(mocker):
    gappy = pd.concat(
        [_bars(["2026-01-15"], seed=1.0), _bars(["2026-03-02"], seed=2.0)], ignore_index=True
    )
    mocker.patch("database.service.download_history", return_value=gappy)
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(["2026-01-15"], seed=500.0),
    )

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-03-02")

    assert mock_qh.call_count == 1


# ---------------------------------------------------------------------
# C. THE CRITICAL REGRESSION TEST -- established LSEG must fetch ONLY
#    the missing sub-range, never the full requested window again.
# ---------------------------------------------------------------------

def test_c_established_lseg_incremental_fetch_uses_only_the_missing_range(mocker, db_session):
    # Provider already established LSEG, with Jan 1 -> Jan 28 cached
    # (28 of the 30 requested days).
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 28, 23, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )

    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(["2026-01-29", "2026-01-30"], seed=999.0),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-30")

    # Exactly ONE LSEG call, for ONLY the missing 2-day range -- never
    # the full Jan1->Jan30 window.
    assert mock_lseg.call_count == 1
    called_ric, _called_interval, called_start, called_end = mock_lseg.call_args[0]
    assert called_ric == _CORRA_H26
    assert called_start == datetime(2026, 1, 29)
    assert called_end == datetime(2026, 1, 30, 23, 59, 59, 999999)

    mock_qh.assert_not_called()

    # The full 30-day window is present in the final result and in the
    # persisted cache -- the pre-existing 28 days plus the 2 newly
    # fetched days, not a re-fetch of the whole window.
    assert len(result) == 30
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.LSEG.value

    reread = cache.read_bars(
        db_session, _CORRA_H26, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 30, 23, 59, 59, 999999)
    )
    assert len(reread) == 30


# ---------------------------------------------------------------------
# D. Established LSEG, multiple missing sub-ranges -- each gap is
#    fetched independently, never collapsed into one full-window call.
# ---------------------------------------------------------------------

def test_d_established_lseg_multiple_gaps_each_fetched_independently(mocker, db_session):
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 5, 23, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    cache.record_sync_range(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 10), datetime(2026, 1, 15, 23, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    cache.insert_bars(
        db_session, _CORRA_H26, "DAILY", _bars(_daily_dates("2026-01-10", 6), seed=2.0)
    )

    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=lambda ric, interval, start, end: _bars(
            [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end.date(), freq="D")], seed=999.0
        ),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-20")

    assert mock_lseg.call_count == 2
    ranges_called = sorted((c[0][2], c[0][3]) for c in mock_lseg.call_args_list)
    # _missing_ranges() reports the gap's upper bound as the next
    # covered range's own start (Jan 10 00:00:00) rather than one
    # epsilon before it -- an existing, unmodified property of that
    # pure function, not something this design changed.
    assert ranges_called[0] == (datetime(2026, 1, 6), datetime(2026, 1, 10))
    assert ranges_called[1] == (datetime(2026, 1, 16), datetime(2026, 1, 20, 23, 59, 59, 999999))
    mock_qh.assert_not_called()


# ---------------------------------------------------------------------
# E. Established QuantHub -- LSEG is never consulted again; QuantHub's
#    unavoidable full-window re-fetch is used whenever anything is
#    missing.
# ---------------------------------------------------------------------

def test_e_established_quanthub_never_calls_lseg_again(mocker, db_session):
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )

    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-01-01", 5), seed=500.0),
    )

    result = service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-05")

    mock_lseg.assert_not_called()
    assert mock_qh.call_count == 1
    # QuantHub cannot request just the missing 2 days -- the full
    # window is requested, matching its own API limitation.
    called_start, called_end = mock_qh.call_args[0][2], mock_qh.call_args[0][3]
    assert called_start == datetime(2026, 1, 1)
    assert called_end == datetime(2026, 1, 5, 23, 59, 59, 999999)

    # The pre-existing Jan1-Jan3 bars are untouched (insert_bars upserts
    # with ON CONFLICT DO NOTHING) -- only the genuinely missing Jan4/
    # Jan5 bars come from this QuantHub full-window re-fetch.
    assert len(result) == 5
    by_date = result.set_index(result["Date"].dt.strftime("%Y-%m-%d"))["Close"]
    assert by_date["2026-01-04"] >= 500.0
    assert by_date["2026-01-05"] >= 500.0
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.QUANTHUB.value


# ---------------------------------------------------------------------
# F. Provider provenance is keyed on (ric, interval), not ric alone --
#    the same contract can have different established providers at
#    different intervals.
# ---------------------------------------------------------------------

def test_f_provider_isolation_by_interval_for_the_same_contract(mocker, db_session):
    def _lseg(ric, interval, start, end):
        if interval == "DAILY":
            return _bars(_daily_dates("2026-01-01", 5))  # complete -> LSEG
        raise MarketDataUnavailableError(ric, "The universe is not found")  # HOURLY -> QH

    mocker.patch("database.service.download_history", side_effect=_lseg)
    mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(["2026-01-01"], seed=500.0),
    )

    service.get_history(_SONIA_H26, "DAILY", "2026-01-01", "2026-01-05")
    service.get_history(_SONIA_H26, "HOURLY", "2026-01-01", "2026-01-01")

    assert cache.get_established_provider(db_session, _SONIA_H26, "DAILY") == Provider.LSEG.value
    assert cache.get_established_provider(db_session, _SONIA_H26, "HOURLY") == Provider.QUANTHUB.value


# ---------------------------------------------------------------------
# G. No mixing -- once established, the OTHER provider is never
#    consulted, even if it would technically be able to satisfy the
#    request.
# ---------------------------------------------------------------------

def test_g_established_lseg_never_calls_quanthub_even_if_it_could_serve(mocker, db_session):
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 5, 23, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    mocker.patch(
        "database.service.download_history",
        return_value=_bars(["2026-01-06"], seed=999.0),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(["2026-01-06"], seed=500.0),
    )

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-06")

    mock_qh.assert_not_called()


def test_g_established_quanthub_never_calls_lseg_even_if_it_could_serve(mocker, db_session):
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 5, 23, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(_daily_dates("2026-01-01", 6)),
    )
    mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-01-01", 6), seed=500.0),
    )

    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-06")

    mock_lseg.assert_not_called()


# ---------------------------------------------------------------------
# H. Reset -- delete_bars_and_sync_ranges() clears provenance, and the
#    next request performs fresh LSEG-first discovery again.
# ---------------------------------------------------------------------

def test_h_reset_clears_provenance_and_next_request_re_establishes(mocker, db_session):
    # First request establishes QuantHub (LSEG fails).
    mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(_CORRA_H26, "The universe is not found"),
    )
    mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-01-01", 3), seed=500.0),
    )
    service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-03")
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.QUANTHUB.value

    # Administrative reset.
    cache.delete_bars_and_sync_ranges(
        db_session, _CORRA_H26, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999)
    )
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") is None
    assert len(cache.get_sync_ranges(db_session, _CORRA_H26, "DAILY")) == 0

    # A fresh request now performs LSEG-first discovery again -- this
    # time LSEG succeeds, so the (ric, interval) is re-established as
    # LSEG rather than being stuck on the prior QuantHub decision.
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(_daily_dates("2026-01-01", 3), seed=1.0),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-03")

    assert mock_lseg.call_count == 1
    mock_qh.assert_not_called()
    assert all(close < 500.0 for close in result["Close"])
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.LSEG.value


# ---------------------------------------------------------------------
# M. LEGACY/UNKNOWN provenance -- provider=NULL with EXISTING cached/
#    sync-range coverage (e.g. a pre-provider-provenance-column
#    database, migrated with provider left NULL). Must NEVER be
#    conflated with "genuinely new" (which would run the full-window
#    LSEG-first establishment test against, and then relabel, the
#    entire historical span -- a real bug this section guards against).
# ---------------------------------------------------------------------

_YBA_H28 = build_ric("YBA", 3, 2028)  # YBAH28


def _seed_legacy(db_session, ric: str, interval: str, start: datetime, end: datetime, seed: float = 1.0):
    """Seed a (ric, interval) exactly as a pre-migration database would
    look: cached bars + sync_ranges coverage, but provider left NULL --
    never established, never touched by the provider-provenance design
    at all."""
    dates = pd.date_range(start, end, freq="D").strftime("%Y-%m-%d").tolist()
    cache.insert_bars(db_session, ric, interval, _bars(dates, seed=seed))
    cache.record_sync_range(db_session, ric, interval, start, end, provider=None)


def test_m_a_legacy_cache_missing_tail_fetches_only_the_gap_provider_stays_null(mocker, db_session):
    # The exact scenario from the investigation: YBAH28/HOURLY, legacy
    # Jan1->Aug20 cache with provider=NULL, new request Jul25->Aug24.
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 8, 20, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end, seed=1.0)

    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(_daily_dates("2026-08-21", 4), seed=999.0),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_YBA_H28, "DAILY", "2026-07-25", "2026-08-24")

    # Only the genuinely missing range (Aug21->Aug24) is requested --
    # NEVER the full Jul25->Aug24 window.
    assert mock_lseg.call_count == 1
    called_start, called_end = mock_lseg.call_args[0][2], mock_lseg.call_args[0][3]
    assert called_start.date().isoformat() == "2026-08-21"
    assert called_end.date().isoformat() == "2026-08-24"
    mock_qh.assert_not_called()

    # Provider is still NOT established -- never fabricated.
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None

    # sync_ranges now covers Jan1 -> Aug24 (the legacy row absorbed the
    # new coverage), still with provider=None.
    ranges = cache.get_sync_ranges(db_session, _YBA_H28, "DAILY")
    assert len(ranges) == 1
    assert ranges[0][0] == legacy_start
    assert ranges[0][1].date().isoformat() == "2026-08-24"

    # The requested 31-day window (Jul25 -> Aug24) is returned in full:
    # the pre-existing legacy portion plus the 4 newly-fetched days.
    assert len(result) == 31

    # Pre-existing legacy bars are completely untouched -- still their
    # original (seed=1.0) values, not overwritten by the new fetch.
    reread = cache.read_bars(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end)
    assert all(close < 500.0 for close in reread["Close"])
    assert len(reread) == 232  # Jan1 -> Aug20 inclusive, unchanged


def test_m_b_legacy_cache_fully_covering_the_request_calls_no_provider(mocker, db_session):
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 8, 20, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end, seed=1.0)

    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_YBA_H28, "DAILY", "2026-02-01", "2026-02-05")

    mock_lseg.assert_not_called()
    mock_qh.assert_not_called()
    assert len(result) == 5
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None


def test_m_c_legacy_cache_lseg_unavailable_falls_back_to_quanthub_provider_stays_null(mocker, db_session):
    # Corrected design (see database.service._fetch_legacy_unknown_provider):
    # a confirmed-unavailable LSEG response for a legacy/unknown-
    # provenance ric's missing range now falls back to QuantHub for
    # JUST that sub-range, rather than aborting the scan -- but the
    # provider is STILL never established either way.
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 8, 20, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end, seed=1.0)

    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(_YBA_H28, "The universe is not found"),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-08-21", 4), seed=500.0),
    )

    # No exception -- the scan does not abort.
    result = service.get_history(_YBA_H28, "DAILY", "2026-07-25", "2026-08-24")

    assert mock_lseg.call_count == 1
    assert mock_qh.call_count == 1

    # Provider is STILL not established -- neither LSEG nor QuantHub.
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None

    # The pre-existing legacy cache is untouched, and the newly-fetched
    # QuantHub bars are appended under the SAME provider=None row.
    ranges = cache.get_sync_ranges(db_session, _YBA_H28, "DAILY")
    assert len(ranges) == 1
    assert ranges[0][0] == legacy_start
    assert ranges[0][1].date().isoformat() == "2026-08-24"

    reread = cache.read_bars(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end)
    assert all(close < 500.0 for close in reread["Close"])  # untouched legacy values
    by_date = result.set_index(result["Date"].dt.strftime("%Y-%m-%d"))["Close"]
    assert by_date["2026-08-21"] >= 500.0  # the QuantHub-fallback bars


def test_m_c2_legacy_cache_real_intraday_92000_message_falls_back_to_quanthub_no_retry_storm(mocker, db_session):
    # Uses the EXACT production error text (TS.Intraday.UserNotPermission.
    # 92000) as the MarketDataUnavailableError's message -- the actual
    # LDError -> MarketDataUnavailableError CONVERSION (and its own
    # no-retry-storm guarantee at the core.downloader/tenacity layer) is
    # separately, rigorously proven in tests/test_downloader.py's
    # test_is_confirmed_no_intraday_permission_* and
    # test_download_history_confirmed_no_intraday_permission_raises_typed_error_not_retried.
    # This test proves the SERVICE-layer consequence once that typed
    # error reaches _fetch_legacy_unknown_provider: exactly one call
    # (no internal retry-storm at this layer either), QuantHub fallback,
    # no exception, provider stays NULL.
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 8, 20, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "4H", legacy_start, legacy_end, seed=1.0)

    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(
            _YBA_H28,
            "No data to return, please check errors: ERROR: No successful response. "
            "(TS.Intraday.UserNotPermission.92000, User has no permission)",
        ),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-08-21", 4), seed=500.0),
    )

    result = service.get_history(_YBA_H28, "4H", "2026-07-25", "2026-08-24")

    assert mock_lseg.call_count == 1  # no retry storm at this layer
    assert mock_qh.call_count == 1
    assert cache.get_established_provider(db_session, _YBA_H28, "4H") is None
    assert len(result) > 0


def test_m_d_legacy_cache_lseg_empty_dataframe_falls_back_to_quanthub_not_marked_synced(mocker, db_session):
    # SONM8/SONZ7's live-observed case: LSEG succeeds (no exception) but
    # returns 0 bars. Must NOT be silently accepted as "confirmed empty,
    # mark synced" -- _is_complete_history treats an empty frame as
    # incomplete, triggering the SAME QuantHub fallback as an exception.
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 8, 20, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end, seed=1.0)

    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"]),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-08-21", 4), seed=500.0),
    )

    result = service.get_history(_YBA_H28, "DAILY", "2026-07-25", "2026-08-24")

    assert mock_lseg.call_count == 1
    assert mock_qh.call_count == 1  # the empty LSEG result triggered fallback, not a no-op
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None

    # The missing range is filled by QuantHub's bars, not left as a
    # false "confirmed empty" gap.
    by_date = result.set_index(result["Date"].dt.strftime("%Y-%m-%d"))["Close"]
    assert by_date["2026-08-21"] >= 500.0
    assert len(result) == 31  # the full requested window, gap-free


def test_m_e_legacy_cache_lseg_incomplete_interior_gap_falls_back_to_quanthub(mocker, db_session):
    # LSEG returns SOME data but with a genuine interior gap (far wider
    # than any real holiday cluster) -- _is_complete_history rejects it,
    # same fallback as empty/unavailable.
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 8, 20, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end, seed=1.0)

    # A ~25-day interior gap (Aug21 -> Sep15), far beyond any real
    # holiday cluster -- unambiguously an interior failure, not a
    # trailing/leading contract-lifecycle shortfall.
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=pd.concat(
            [_bars(["2026-08-21"], seed=1.0), _bars(["2026-09-15"], seed=2.0)], ignore_index=True
        ),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(_daily_dates("2026-08-21", 10), seed=500.0),
    )

    service.get_history(_YBA_H28, "DAILY", "2026-07-25", "2026-09-15")

    assert mock_lseg.call_count == 1
    assert mock_qh.call_count == 1
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None


def test_m_f_batch_legacy_ric_uses_lseg_first_quanthub_fallback_never_joins_qh_batch(mocker, db_session):
    # The batch path must apply the SAME LSEG-first/QuantHub-fallback
    # logic to a legacy ric as the single-RIC path -- and that ric must
    # NEVER be folded into the shared QuantHub BATCH call queued for
    # established/newly-establishing rics, even when its own fallback
    # also happens to use QuantHub.
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 1, 3, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end, seed=1.0)
    # A second, genuinely-new QH-mapped ric in the SAME batch call, whose
    # own establishment ALSO ends up on QuantHub (via the real batched
    # QH call) -- proves the legacy ric's per-sub-range QuantHub
    # fallback stays completely separate from that batch queue.
    _CORRA_FRESH = build_ric("CORRA", 6, 2026)

    def _lseg(ric, interval, start, end):
        if ric == _YBA_H28:
            return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        raise MarketDataUnavailableError(ric, "The universe is not found")

    mocker.patch("database.service.download_history", side_effect=_lseg)
    mock_qh_single = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_bars(["2026-01-04", "2026-01-05"], seed=500.0),
    )
    mock_qh_batch = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=_qh_batch_side_effect,
    )

    result = service.get_history_batch(
        [_YBA_H28, _CORRA_FRESH], "DAILY", "2026-01-01", "2026-01-05"
    )

    assert set(result) == {_YBA_H28, _CORRA_FRESH}
    # The legacy ric's fallback goes through the single-instrument
    # QuantHub call, NOT the batch call.
    assert mock_qh_single.call_count == 1
    # The batch call carries only the genuinely-new ric's instrument.
    assert mock_qh_batch.call_count == 1
    assert len(mock_qh_batch.call_args[0][0]) == 1

    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None
    assert cache.get_established_provider(db_session, _CORRA_FRESH, "DAILY") == Provider.QUANTHUB.value


def test_m_g_legacy_unrelated_exception_still_propagates_not_silently_swallowed(mocker, db_session):
    # Only MarketDataUnavailableError (and an empty/incomplete frame)
    # trigger the QuantHub fallback -- a genuine network/auth/programming
    # error must still propagate and abort, exactly as everywhere else
    # in the codebase's exception policy. No broad LDError/Exception
    # catch was introduced.
    legacy_start = datetime(2026, 1, 1)
    legacy_end = datetime(2026, 8, 20, 23, 59, 59, 999999)
    _seed_legacy(db_session, _YBA_H28, "DAILY", legacy_start, legacy_end, seed=1.0)

    mocker.patch("database.service.download_history", side_effect=RuntimeError("network boom"))
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    with pytest.raises(RuntimeError, match="network boom"):
        service.get_history(_YBA_H28, "DAILY", "2026-07-25", "2026-08-24")

    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None


def test_m_h_migration_then_get_history_recognizes_legacy_not_fresh(mocker, db_session, db_engine):
    # Simulate the real end-to-end migration story: a database with an
    # OLD sync_ranges row (no provider column at all, as
    # tests/test_connection.py's migration test also proves), migrated
    # via init_db(), then queried through get_history() -- must be
    # recognized as LEGACY/UNKNOWN, not as a fresh (ric, interval).
    from sqlalchemy import text

    from database.connection import init_db

    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS sync_ranges"))
        conn.execute(
            text(
                """
                CREATE TABLE sync_ranges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ric VARCHAR(32) NOT NULL,
                    interval VARCHAR(8) NOT NULL,
                    start_datetime DATETIME NOT NULL,
                    end_datetime DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO sync_ranges "
                "(ric, interval, start_datetime, end_datetime, updated_at) "
                f"VALUES ('{_YBA_H28}', 'DAILY', '2026-01-01', '2026-08-20 23:59:59.999999', '2026-01-01')"
            )
        )
    cache.insert_bars(
        db_session, _YBA_H28, "DAILY",
        _bars(_daily_dates("2026-01-01", 233), seed=1.0),  # Jan1 -> Aug20-ish
    )

    init_db(db_engine)  # runs the additive provider-column migration

    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(["2026-08-21"], seed=999.0),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_YBA_H28, "DAILY", "2026-01-01", "2026-08-21")

    # Recognized as LEGACY, not genuinely-new: only the missing single
    # day is requested, never the whole Jan1->Aug21 window, and
    # QuantHub is never consulted.
    assert mock_lseg.call_count == 1
    called_start = mock_lseg.call_args[0][2]
    assert called_start.date().isoformat() == "2026-08-21"
    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None


# ---------------------------------------------------------------------
# I. Batch path -- get_history_batch() respects established provenance
#    per-ric: established-LSEG rics fetch incrementally and individually;
#    established/newly-established-QuantHub rics are combined into one
#    batched QuantHub call.
# ---------------------------------------------------------------------

def test_i_batch_respects_per_ric_provenance_and_batches_quanthub_group(mocker, db_session):
    # _CORRA_H26: established LSEG, 3 of 5 days cached -- must fetch
    # only the missing 2-day range from LSEG.
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    # _CORRA_U26: already established QuantHub, nothing cached yet --
    # must join the QuantHub batch call, never call LSEG.
    # _CORRA_M26: not yet established -- LSEG trial fails, so it also
    # joins the QuantHub batch call and becomes established QuantHub.
    cache.record_sync_range(
        db_session, _CORRA_U26, "DAILY",
        datetime(2020, 1, 1), datetime(2020, 1, 1),
        provider=Provider.QUANTHUB.value,
    )
    # _YBA_H28: LEGACY -- existing coverage but provider=NULL -- must
    # fetch only its own missing 2-day gap from LSEG, individually,
    # never join the QuantHub batch, never establish a provider, and
    # never be affected by _CORRA_M26/_CORRA_U26 establishing QuantHub
    # in this SAME batch call.
    _seed_legacy(
        db_session, _YBA_H28, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999),
    )

    def _lseg(ric, interval, start, end):
        if ric == _CORRA_H26:
            assert start == datetime(2026, 1, 4)
            assert end == datetime(2026, 1, 5, 23, 59, 59, 999999)
            return _bars(["2026-01-04", "2026-01-05"], seed=999.0)
        if ric == _CORRA_M26:
            raise MarketDataUnavailableError(ric, "The universe is not found")
        if ric == _YBA_H28:
            assert start == datetime(2026, 1, 4)
            assert end == datetime(2026, 1, 5, 23, 59, 59, 999999)
            return _bars(["2026-01-04", "2026-01-05"], seed=777.0)
        raise AssertionError(f"LSEG must not be called for {ric} (already established QuantHub)")

    mock_lseg = mocker.patch("database.service.download_history", side_effect=_lseg)
    mock_qh_batch = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=_qh_batch_side_effect,
    )

    result = service.get_history_batch(
        [_CORRA_H26, _CORRA_M26, _CORRA_U26, _YBA_H28], "DAILY", "2026-01-01", "2026-01-05"
    )

    assert set(result) == {_CORRA_H26, _CORRA_M26, _CORRA_U26, _YBA_H28}
    assert mock_lseg.call_count == 3  # H26's gap, M26's failed trial, YBA_H28's gap
    assert mock_qh_batch.call_count == 1  # M26 and U26 combined into ONE batch call
    # Exactly 2 instruments in that one call -- proves YBA_H28 (legacy)
    # never joined the QuantHub batch, not just that call_count stayed 1
    # (a bug that silently added YBA_H28 as a 3rd instrument to the SAME
    # single call would otherwise be invisible to a call_count check).
    assert len(mock_qh_batch.call_args[0][0]) == 2

    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.LSEG.value
    assert cache.get_established_provider(db_session, _CORRA_M26, "DAILY") == Provider.QUANTHUB.value
    assert cache.get_established_provider(db_session, _CORRA_U26, "DAILY") == Provider.QUANTHUB.value
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None  # never fabricated

    h26_bars = cache.read_bars(
        db_session, _CORRA_H26, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5, 23, 59, 59, 999999)
    )
    assert len(h26_bars) == 5
    yba_bars = cache.read_bars(
        db_session, _YBA_H28, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 5, 23, 59, 59, 999999)
    )
    assert len(yba_bars) == 5


def test_i_all_lseg_established_batch_makes_zero_quanthub_calls(mocker, db_session):
    for ric in (_CORRA_H26, _CORRA_M26):
        _seed_established(
            db_session, ric, "DAILY",
            datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999),
            provider=Provider.LSEG.value,
        )

    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars(["2026-01-04", "2026-01-05"]),
    )
    mock_qh_batch = mocker.patch("database.service.download_history_quanthub_batch")

    result = service.get_history_batch(
        [_CORRA_H26, _CORRA_M26], "DAILY", "2026-01-01", "2026-01-05"
    )

    assert set(result) == {_CORRA_H26, _CORRA_M26}
    assert mock_lseg.call_count == 2
    mock_qh_batch.assert_not_called()


# ---------------------------------------------------------------------
# I2. Full call-chain integration: strategy_engine.pricing.
#     prewarm_leg_cache() -> database.get_history_batch() -> per-(ric,
#     interval) provider logic, followed by build_history()/_fetch_leg()
#     consuming the SAME leg_cache. Proves no RIC is ever fetched twice
#     across the prewarm/build_history boundary, for all four provider
#     states (fresh, established LSEG, established QuantHub, legacy/
#     unknown) present in the SAME scan at once -- the exact run_scan()
#     -> run_scan_on_instances() -> prewarm_leg_cache() -> build_history()
#     -> _fetch_leg() call chain, minus only the analytics/filtering
#     layers on top (which don't touch market data at all).
# ---------------------------------------------------------------------

def test_i2_prewarm_then_build_history_never_double_fetches_any_provider_state(mocker, db_session):
    from strategy_engine.combinations import StrategyInstance
    from strategy_engine.definitions import StrategyDefinition
    from strategy_engine.pricing import build_history, prewarm_leg_cache

    # Same four states as test_i above: established LSEG, established
    # QuantHub, legacy/unknown, and genuinely new (_CORRA_M26, seeded
    # with nothing here).
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    cache.record_sync_range(
        db_session, _CORRA_U26, "DAILY",
        datetime(2020, 1, 1), datetime(2020, 1, 1),
        provider=Provider.QUANTHUB.value,
    )
    _seed_legacy(
        db_session, _YBA_H28, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 3, 23, 59, 59, 999999),
    )

    def _lseg(ric, interval, start, end):
        if ric == _CORRA_H26:
            return _bars(["2026-01-04", "2026-01-05"], seed=999.0)
        if ric == _CORRA_M26:
            raise MarketDataUnavailableError(ric, "The universe is not found")
        if ric == _YBA_H28:
            return _bars(["2026-01-04", "2026-01-05"], seed=777.0)
        raise AssertionError(f"LSEG must not be called for {ric}")

    mock_lseg = mocker.patch("database.service.download_history", side_effect=_lseg)
    mock_qh_batch = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=_qh_batch_side_effect,
    )

    def _outright(market_key: str, ric: str) -> StrategyInstance:
        definition = StrategyDefinition(
            market_key=market_key, offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
        )
        return StrategyInstance(definition=definition, rics=(ric,))

    instances = [
        _outright("CORRA", _CORRA_H26),
        _outright("CORRA", _CORRA_M26),
        _outright("CORRA", _CORRA_U26),
        _outright("YBA", _YBA_H28),
    ]

    leg_cache = prewarm_leg_cache(instances, "2026-01-01", "2026-01-05")

    prewarm_lseg_calls = mock_lseg.call_count
    prewarm_qh_calls = mock_qh_batch.call_count
    assert prewarm_lseg_calls == 3  # H26's gap, M26's failed trial, YBA_H28's gap
    assert prewarm_qh_calls == 1  # M26 (newly establishing) + U26 (established) combined

    for instance in instances:
        build_history(instance, "2026-01-01", "2026-01-05", leg_cache=leg_cache)

    # NOT ONE additional provider call happened while building histories
    # -- every leg required by these instances was already present in
    # leg_cache from the prewarm alone. This is the literal assertion
    # the investigation was asked to make: no
    #     get_history_batch() -> fetch LSEG
    # followed by a SEPARATE
    #     build_history() -> _fetch_leg() -> fetch LSEG again
    # for the same (ric, interval, price_start, price_end).
    assert mock_lseg.call_count == prewarm_lseg_calls
    assert mock_qh_batch.call_count == prewarm_qh_calls

    # And the provider states themselves are exactly as expected after
    # the whole chain runs, independent of one another.
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.LSEG.value
    assert cache.get_established_provider(db_session, _CORRA_M26, "DAILY") == Provider.QUANTHUB.value
    assert cache.get_established_provider(db_session, _CORRA_U26, "DAILY") == Provider.QUANTHUB.value
    assert cache.get_established_provider(db_session, _YBA_H28, "DAILY") is None


# ---------------------------------------------------------------------
# J. Non-QH markets -- existing LSEG-only behavior completely unchanged.
# ---------------------------------------------------------------------

def test_j_non_qh_market_never_attempts_completeness_check_or_qh(mocker):
    # A SOFR-style RIC with a real interior gap (that WOULD trigger a
    # fallback for a QuantHub-mapped market) must still be accepted
    # as-is -- SOFR has no QuantHub mapping, so provider provenance
    # does not apply to it at all; behavior is byte-identical to before.
    gappy = pd.concat(
        [_bars(["2026-01-01"], seed=1.0), _bars(["2026-03-01"], seed=2.0)], ignore_index=True
    )
    mock_lseg = mocker.patch("database.service.download_history", return_value=gappy)
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_SOFR_H26, "DAILY", "2026-01-01", "2026-03-01")

    assert mock_lseg.call_count == 1
    mock_qh.assert_not_called()
    assert len(result) == 2


def test_j_non_qh_market_incremental_sub_range_fetch_still_used(mocker, db_session):
    seeded = _bars(["2026-01-01"], seed=1.0)
    cache.insert_bars(db_session, _SOFR_H26, "DAILY", seeded)
    cache.record_sync_range(
        db_session, _SOFR_H26, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59, 59, 999999)
    )

    mock_lseg = mocker.patch(
        "database.service.download_history", return_value=_bars(["2026-01-02"], seed=2.0)
    )

    service.get_history(_SOFR_H26, "DAILY", "2026-01-01", "2026-01-02")

    assert mock_lseg.call_count == 1
    called_start, called_end = mock_lseg.call_args[0][2], mock_lseg.call_args[0][3]
    assert called_start.date().isoformat() == "2026-01-02"  # only the missing day, not Jan 1

    # A non-QH market's sync_ranges rows carry no provider at all.
    assert cache.get_established_provider(db_session, _SOFR_H26, "DAILY") is None


def test_j_non_qh_market_cache_complete_calls_no_provider(mocker, db_session):
    seeded = _bars(["2026-01-01", "2026-01-02"])
    cache.insert_bars(db_session, _SOFR_H26, "DAILY", seeded)
    cache.record_sync_range(
        db_session, _SOFR_H26, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 2, 23, 59, 59, 999999)
    )

    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_SOFR_H26, "DAILY", "2026-01-01", "2026-01-02")

    mock_lseg.assert_not_called()
    mock_qh.assert_not_called()
    assert len(result) == 2


# ---------------------------------------------------------------------
# K. Exception handling -- unchanged from before this design existed.
# ---------------------------------------------------------------------

def test_k_quanthub_rate_limit_error_propagates_during_establishment(mocker):
    mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(_CORRA_H26, "The universe is not found"),
    )
    mocker.patch(
        "database.service.download_history_quanthub",
        side_effect=QuantHubRateLimitError("rate limited"),
    )

    with pytest.raises(QuantHubRateLimitError):
        service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-01")


def test_k_quanthub_rate_limit_error_propagates_for_established_quanthub(mocker, db_session):
    _seed_established(
        db_session, _CORRA_H26, "DAILY",
        datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )
    mocker.patch(
        "database.service.download_history_quanthub",
        side_effect=QuantHubRateLimitError("rate limited"),
    )

    with pytest.raises(QuantHubRateLimitError):
        service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-02")


def test_k_unrelated_lseg_exception_is_not_broadened_into_qh_fallback(mocker):
    # Only MarketDataUnavailableError triggers the QH fallback --
    # authentication/network/programming errors must still propagate
    # and abort, exactly as before this design existed.
    mocker.patch("database.service.download_history", side_effect=RuntimeError("network boom"))
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    with pytest.raises(RuntimeError, match="network boom"):
        service.get_history(_CORRA_H26, "DAILY", "2026-01-01", "2026-01-01")

    mock_qh.assert_not_called()


# ---------------------------------------------------------------------
# L. QuantHub's 10,000-row limit / batch size remain enforced/unmodified.
# ---------------------------------------------------------------------

def test_l_quanthub_batch_size_and_row_limit_constants_unchanged():
    # This design must not have touched QuantHub's own request-shaping
    # constants at all -- live-verified values from a separate
    # investigation (see core/quanthub.py), locked here as a guard
    # against an accidental edit while modifying database/service.py.
    assert QUANTHUB_BATCH_SIZE == 10
    assert QUANTHUB_MAX_ROWS_PER_REQUEST == 10_000
