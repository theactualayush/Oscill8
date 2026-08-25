"""
tests/test_service_effective_request_end.py

Focused tests for database.service._effective_request_end -- the fix
for a real, live-observed issue: a plain-date scan request stayed
uncapped all the way to day-end, so a currently-forming interval (most
visibly a 4H bar) was re-requested from the provider on every identical
re-scan during the same still-forming period, since nothing in that
wide tail could ever be marked synced.

Covers:
    - The pure boundary-capping logic itself, across DAILY/HOURLY/4H.
    - get_history(): a repeated scan during an open period makes zero
      additional provider requests; the newly-closed bar is fetched
      exactly once after the period closes -- for DAILY, HOURLY, AND
      4H (not just 4H in isolation).
    - Composition with the provider-provenance state machine
      (established LSEG / established QuantHub / legacy-unknown /
      genuinely-new establishment): the effective-end cap applies
      identically regardless of provider state, and never causes
      provider mixing or a false establishment decision.
    - get_history_batch()/prewarm_leg_cache(): the same guarantee holds
      across many RICs in one batched call.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from core.config import BarInterval
from core.downloader import MarketDataUnavailableError
from core.providers import Provider
from core.ric import build_ric
from database import cache, service
from database.connection import get_session as _real_get_session

_SOFR_RIC = "SRAZ26"  # no QuantHub mapping
_CORRA_RIC = build_ric("CORRA", 3, 2026)  # QuantHub-mapped


@pytest.fixture(autouse=True)
def _route_service_sessions_to_test_engine(monkeypatch, db_engine):
    monkeypatch.setattr(service, "get_session", lambda: _real_get_session(db_engine))
    yield


def _freeze_now(mocker, frozen: datetime):
    class _Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return frozen

    mocker.patch.object(service, "datetime", _Frozen)


def _bars(dates: list[datetime], seed: float = 100.0) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": [seed + i for i in range(n)],
            "High": [seed + i + 1 for i in range(n)],
            "Low": [seed + i - 1 for i in range(n)],
            "Close": [seed + i + 0.5 for i in range(n)],
            "Volume": [1000 + i for i in range(n)],
        }
    )


# ---------------------------------------------------------------------
# A. Pure boundary-capping logic (no I/O), across DAILY/HOURLY/4H.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "interval,now,expected_ceiling",
    [
        (BarInterval.DAILY, datetime(2026, 6, 15, 14, 0), datetime(2026, 6, 14, 23, 59, 59, 999999)),
        (BarInterval.HOURLY, datetime(2026, 6, 15, 10, 30), datetime(2026, 6, 15, 9, 59, 59, 999999)),
        (BarInterval.FOUR_HOUR, datetime(2026, 6, 15, 15, 47), datetime(2026, 6, 15, 11, 59, 59, 999999)),
        (BarInterval.FOUR_HOUR, datetime(2026, 6, 15, 16, 5), datetime(2026, 6, 15, 15, 59, 59, 999999)),
    ],
)
def test_effective_request_end_caps_to_last_closed_bar(interval, now, expected_ceiling):
    boundary = service._last_completed_boundary(interval, now)
    far_future_end = datetime(2026, 12, 31, 23, 59, 59, 999999)
    assert service._effective_request_end(far_future_end, boundary) == expected_ceiling


def test_effective_request_end_leaves_an_already_narrower_end_unchanged():
    boundary = datetime(2026, 6, 15, 12, 0)
    narrow_end = datetime(2026, 6, 15, 10, 0)
    assert service._effective_request_end(narrow_end, boundary) == narrow_end


# ---------------------------------------------------------------------
# B. get_history(): repeated scan during an open period -> zero extra
#    requests; newly-closed bar fetched exactly once after it closes.
#    Parametrized across DAILY, HOURLY, and FOUR_HOUR -- the exact
#    scenario reported live for 4H must not be fixed for 4H alone.
# ---------------------------------------------------------------------

_CASES = [
    pytest.param(
        BarInterval.DAILY,
        "2026-06-13", "2026-06-16",  # a multi-day window: DAILY's own
        # bar is one whole calendar day, so a single-day request would
        # have nothing fetchable at all while that day is still open --
        # widened here so the two already-closed days ARE fetchable
        # while today (06-15, still forming at now1) is correctly
        # excluded.
        datetime(2026, 6, 15, 14, 0),
        [datetime(2026, 6, 13), datetime(2026, 6, 14)],
        datetime(2026, 6, 13), datetime(2026, 6, 14, 23, 59, 59, 999999),
        datetime(2026, 6, 16, 10, 0),
        [datetime(2026, 6, 15)],
        datetime(2026, 6, 15), datetime(2026, 6, 15, 23, 59, 59, 999999),
        id="daily",
    ),
    pytest.param(
        BarInterval.HOURLY,
        "2026-06-15", "2026-06-15",
        datetime(2026, 6, 15, 10, 30),
        [datetime(2026, 6, 15, 8, 0), datetime(2026, 6, 15, 9, 0)],
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 9, 59, 59, 999999),
        datetime(2026, 6, 15, 11, 5),
        [datetime(2026, 6, 15, 10, 0)],
        datetime(2026, 6, 15, 10, 0), datetime(2026, 6, 15, 10, 59, 59, 999999),
        id="hourly",
    ),
    pytest.param(
        BarInterval.FOUR_HOUR,
        "2026-06-15", "2026-06-15",
        datetime(2026, 6, 15, 15, 47),
        [datetime(2026, 6, 15, 4, 0), datetime(2026, 6, 15, 8, 0)],
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        datetime(2026, 6, 15, 16, 5),
        [datetime(2026, 6, 15, 12, 0)],
        datetime(2026, 6, 15, 12, 0), datetime(2026, 6, 15, 15, 59, 59, 999999),
        id="four_hour",
    ),
]


@pytest.mark.parametrize(
    "interval,req_start,req_end,now1,first_bars_dates,exp1_start,exp1_end,"
    "now2,second_bars_dates,exp2_start,exp2_end",
    _CASES,
)
def test_repeated_scan_during_open_period_then_bar_closes(
    mocker, db_session,
    interval, req_start, req_end, now1, first_bars_dates, exp1_start, exp1_end,
    now2, second_bars_dates, exp2_start, exp2_end,
):
    _freeze_now(mocker, now1)
    mock_lseg = mocker.patch(
        "database.service.download_history", return_value=_bars(first_bars_dates)
    )

    service.get_history(_SOFR_RIC, interval, req_start, req_end)

    assert mock_lseg.call_count == 1
    called_start, called_end = mock_lseg.call_args[0][2], mock_lseg.call_args[0][3]
    assert called_start == exp1_start
    assert called_end == exp1_end

    # Repeated identical scan, SAME still-forming period -> zero
    # additional provider requests.
    service.get_history(_SOFR_RIC, interval, req_start, req_end)
    assert mock_lseg.call_count == 1

    # The period closes; the next scan fetches EXACTLY the newly-closed
    # bar, nothing more.
    _freeze_now(mocker, now2)
    mock_lseg2 = mocker.patch(
        "database.service.download_history", return_value=_bars(second_bars_dates)
    )
    service.get_history(_SOFR_RIC, interval, req_start, req_end)

    assert mock_lseg2.call_count == 1
    called_start2, called_end2 = mock_lseg2.call_args[0][2], mock_lseg2.call_args[0][3]
    assert called_start2 == exp2_start
    assert called_end2 == exp2_end

    # A third identical scan for the same (now again fully-synced-
    # through-the-cap) window makes zero further requests.
    service.get_history(_SOFR_RIC, interval, req_start, req_end)
    assert mock_lseg2.call_count == 1


# ---------------------------------------------------------------------
# C. Composition with provider-provenance -- the cap applies identically
#    regardless of established provider, and never causes mixing or a
#    false establishment.
# ---------------------------------------------------------------------

def test_established_lseg_during_open_4h_period_makes_zero_extra_calls(mocker, db_session):
    cache.record_sync_range(
        db_session, _CORRA_RIC, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    _freeze_now(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_RIC, "4H", "2026-06-15", "2026-06-15")

    mock_lseg.assert_not_called()
    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _CORRA_RIC, "4H") == Provider.LSEG.value


def test_established_quanthub_during_open_4h_period_makes_zero_extra_calls(mocker, db_session):
    cache.record_sync_range(
        db_session, _CORRA_RIC, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )
    _freeze_now(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_RIC, "4H", "2026-06-15", "2026-06-15")

    mock_lseg.assert_not_called()
    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _CORRA_RIC, "4H") == Provider.QUANTHUB.value


def test_legacy_unknown_during_open_4h_period_makes_zero_extra_calls(mocker, db_session):
    cache.record_sync_range(
        db_session, _CORRA_RIC, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=None,
    )
    _freeze_now(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_RIC, "4H", "2026-06-15", "2026-06-15")

    mock_lseg.assert_not_called()
    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _CORRA_RIC, "4H") is None


def test_genuinely_new_establishment_trial_itself_excludes_the_forming_bar(mocker, db_session):
    """The one-time LSEG-first establishment test must ALSO never probe
    into the still-forming period -- establishment is not exempt from
    the cap."""
    _freeze_now(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars([datetime(2026, 6, 15, 4, 0), datetime(2026, 6, 15, 8, 0)]),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    service.get_history(_CORRA_RIC, "4H", "2026-06-15", "2026-06-15")

    assert mock_lseg.call_count == 1
    called_start, called_end = mock_lseg.call_args[0][2], mock_lseg.call_args[0][3]
    assert called_start == datetime(2026, 6, 15, 0, 0)
    assert called_end == datetime(2026, 6, 15, 11, 59, 59, 999999)
    mock_qh.assert_not_called()
    assert cache.get_established_provider(db_session, _CORRA_RIC, "4H") == Provider.LSEG.value


# ---------------------------------------------------------------------
# D. get_history_batch() -- the same guarantee across many RICs in one
#    batched call.
# ---------------------------------------------------------------------

def test_batch_repeated_scan_during_open_4h_period_makes_zero_extra_calls(mocker, db_session):
    _SOFR_2 = "SRAU26"
    cache.record_sync_range(
        db_session, _CORRA_RIC, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    _freeze_now(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_bars([datetime(2026, 6, 15, 8, 0)]),
    )
    mock_qh_batch = mocker.patch("database.service.download_history_quanthub_batch")

    result = service.get_history_batch(
        [_SOFR_2, _CORRA_RIC], "4H", "2026-06-15", "2026-06-15"
    )
    first_call_count = mock_lseg.call_count
    assert first_call_count >= 1  # SOFR_2 (no cache yet) required a fetch
    assert set(result) == {_SOFR_2, _CORRA_RIC}
    mock_qh_batch.assert_not_called()

    # Repeated identical batch call, same still-forming period -> zero
    # additional requests for EITHER ric.
    service.get_history_batch([_SOFR_2, _CORRA_RIC], "4H", "2026-06-15", "2026-06-15")
    assert mock_lseg.call_count == first_call_count
    mock_qh_batch.assert_not_called()


# ---------------------------------------------------------------------
# E. effective_end <= start_dt -- the requested window falls ENTIRELY
#    within the currently-forming period (nothing could possibly be
#    fetchable yet). get_history()/get_history_batch() must return
#    cleanly, with zero LSEG/QuantHub requests and no exception, for
#    every provider state.
# ---------------------------------------------------------------------

def _window_wholly_inside_forming_period(mocker, now: datetime):
    """A [start, end] window strictly inside the still-forming period at
    `now` (for FOUR_HOUR, whose boundary at 15:47 is 12:00 -- 13:00/15:00
    both fall inside [12:00, 16:00), so effective_end (11:59:59.999999)
    < start_dt (13:00))."""
    _freeze_now(mocker, now)
    return service.datetime(2026, 6, 15, 13, 0), service.datetime(2026, 6, 15, 15, 0)


def test_get_history_window_wholly_inside_forming_period_non_qh_market_makes_no_request(mocker, db_session):
    win_start, win_end = _window_wholly_inside_forming_period(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")

    result = service.get_history(_SOFR_RIC, "4H", win_start, win_end)

    mock_lseg.assert_not_called()
    assert result.empty
    assert list(result.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_get_history_window_wholly_inside_forming_period_fresh_qh_mapped_ric_makes_no_request(mocker, db_session):
    """No sync_ranges, no established provider -- would normally trigger
    the LSEG-first establishment trial -- but the whole window is
    unfetchable, so establishment must not even be attempted."""
    win_start, win_end = _window_wholly_inside_forming_period(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_CORRA_RIC, "4H", win_start, win_end)

    mock_lseg.assert_not_called()
    mock_qh.assert_not_called()
    assert result.empty
    assert cache.get_established_provider(db_session, _CORRA_RIC, "4H") is None


def test_get_history_window_wholly_inside_forming_period_established_lseg_makes_no_request(mocker, db_session):
    cache.record_sync_range(
        db_session, _CORRA_RIC, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    win_start, win_end = _window_wholly_inside_forming_period(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")

    result = service.get_history(_CORRA_RIC, "4H", win_start, win_end)

    mock_lseg.assert_not_called()
    assert result.empty


def test_get_history_window_wholly_inside_forming_period_established_quanthub_makes_no_request(mocker, db_session):
    cache.record_sync_range(
        db_session, _CORRA_RIC, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )
    win_start, win_end = _window_wholly_inside_forming_period(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_CORRA_RIC, "4H", win_start, win_end)

    mock_lseg.assert_not_called()
    mock_qh.assert_not_called()
    assert result.empty


def test_get_history_window_wholly_inside_forming_period_legacy_unknown_makes_no_request(mocker, db_session):
    cache.record_sync_range(
        db_session, _CORRA_RIC, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=None,
    )
    win_start, win_end = _window_wholly_inside_forming_period(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")

    result = service.get_history(_CORRA_RIC, "4H", win_start, win_end)

    mock_lseg.assert_not_called()
    assert result.empty
    assert cache.get_established_provider(db_session, _CORRA_RIC, "4H") is None


def test_get_history_batch_window_wholly_inside_forming_period_makes_no_requests(mocker, db_session):
    """Mixed batch: a non-QH ric, a fresh QH-mapped ric, an established-
    LSEG ric, and an established-QuantHub ric, all in ONE call whose
    entire requested window is still-forming -- zero provider requests
    for any of them, clean empty results, no exception."""
    _CORRA_ESTABLISHED_LSEG = build_ric("CORRA", 6, 2026)
    _CORRA_ESTABLISHED_QH = build_ric("CORRA", 9, 2026)
    cache.record_sync_range(
        db_session, _CORRA_ESTABLISHED_LSEG, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=Provider.LSEG.value,
    )
    cache.record_sync_range(
        db_session, _CORRA_ESTABLISHED_QH, "4H",
        datetime(2026, 6, 15, 0, 0), datetime(2026, 6, 15, 11, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )
    win_start, win_end = _window_wholly_inside_forming_period(mocker, datetime(2026, 6, 15, 15, 47))
    mock_lseg = mocker.patch("database.service.download_history")
    mock_qh_batch = mocker.patch("database.service.download_history_quanthub_batch")

    result = service.get_history_batch(
        [_SOFR_RIC, _CORRA_RIC, _CORRA_ESTABLISHED_LSEG, _CORRA_ESTABLISHED_QH],
        "4H", win_start, win_end,
    )

    mock_lseg.assert_not_called()
    mock_qh_batch.assert_not_called()
    assert set(result) == {_SOFR_RIC, _CORRA_RIC, _CORRA_ESTABLISHED_LSEG, _CORRA_ESTABLISHED_QH}
    assert all(df.empty for df in result.values())
    assert cache.get_established_provider(db_session, _CORRA_RIC, "4H") is None
