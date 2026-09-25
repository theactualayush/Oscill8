"""
tests/test_service_quanthub_incremental.py

Focused tests for QuantHub INCREMENTAL cache synchronization in
database/service.py -- the four cache states a request can find, proved
against the real production path (database.service.get_history /
get_history_batch, the real database.cache, the real
database.service._missing_ranges), with only the provider-facing
download functions mocked:

    A. EMPTY CACHE      -> one request for the whole requested range
    B. COMPLETE CACHE   -> ZERO provider requests
    C. TAIL GAP         -> one request for ONLY the missing tail
    D. INTERIOR HOLE    -> one request for ONLY the hole

Before QuantHub's start/end request shape existed, an established-
QuantHub (ric, interval) could only ever be refreshed by re-requesting
the caller's entire window, because count= means "the most recent N bars
ending now" and cannot express a historical sub-range. Cases C and D are
what that limitation made impossible; they are the point of these tests.

WHAT IS DELIBERATELY NOT MOCKED: _missing_ranges, database.cache's
upsert/coverage bookkeeping, and the SQLite schema. These tests assert
against the REAL gap detector and the REAL cache, so a passing result
means the production wiring works, not that a stub agreed with itself.

WHAT IS ASSERTED: the actual outgoing provider call arguments, not just
the returned DataFrame. A final frame can look correct while the
provider was asked for six months of history behind it -- which is
precisely the regression these tests exist to catch.

No live QuantHub access: core.quanthub's entry points are mocked at
database.service's own imported names, matching the convention in
tests/test_service_provider_fallback.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import requests
import pytest

from core.providers import Provider
from core.ric import build_ric
from database import cache, service
from database.connection import get_session as _real_get_session

_CORRA_H26 = build_ric("CORRA", 3, 2026)
_CORRA_M26 = build_ric("CORRA", 6, 2026)
_SONIA_H26 = build_ric("SONIA", 3, 2026)

# Every window here sits well in the past relative to any plausible run
# date, so _effective_request_end never trims it and the assertions stay
# deterministic. The currently-forming-bar behaviour is exercised
# separately, on purpose, at the bottom of this file.
_WINDOW_START = datetime(2026, 1, 1)
_WINDOW_END_DT = datetime(2026, 1, 10, 23, 59, 59, 999999)
_WINDOW_START_STR = "2026-01-01"
_WINDOW_END_STR = "2026-01-10"


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


def _daily(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="D")]


def _seed_quanthub(db_session, ric, interval, start, end, seed=1.0):
    """Put a (ric, interval) into the state a previous successful
    QuantHub-established call would have left: cached bars AND
    sync_ranges coverage for exactly [start, end], provider=QUANTHUB."""
    cache.insert_bars(
        db_session, ric, interval,
        _bars(_daily(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")), seed=seed),
    )
    cache.record_sync_range(
        db_session, ric, interval, start, end, provider=Provider.QUANTHUB.value
    )


def _mock_qh(mocker, frame=None, side_effect=None):
    """Mock the single-RIC QuantHub download at service's imported name."""
    kwargs = {}
    if side_effect is not None:
        kwargs["side_effect"] = side_effect
    else:
        kwargs["return_value"] = frame if frame is not None else _bars(["2026-01-01"])
    return mocker.patch("database.service.download_history_quanthub", **kwargs)


def _requested_range(mock_call) -> tuple[datetime, datetime]:
    """(start, end) a QuantHub download was actually asked for."""
    return mock_call.args[2], mock_call.args[3]


# =====================================================================
# CASE A -- EMPTY CACHE
# =====================================================================

def test_case_a_empty_cache_requests_the_whole_range_once(mocker, db_session):
    """Nothing cached: exactly one request, covering the full window,
    and every returned bar is persisted."""
    returned = _bars(_daily("2026-01-01", "2026-01-10"), seed=500.0)
    mock_qh = _mock_qh(mocker, returned)
    # LSEG must fail so establishment lands on QuantHub. An empty frame
    # is "incomplete" to _is_complete_history, the same signal a market
    # with no LSEG entitlement produces in production.
    mocker.patch(
        "database.service.download_history",
        return_value=pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"]),
    )

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_qh.call_count == 1
    start, end = _requested_range(mock_qh.call_args)
    assert start == _WINDOW_START
    assert end == _WINDOW_END_DT
    assert mock_qh.call_args.kwargs["use_date_range"] is True

    assert len(result) == 10
    assert cache.get_established_provider(db_session, _CORRA_H26, "DAILY") == Provider.QUANTHUB.value


def test_case_a_no_second_full_history_request(mocker, db_session):
    """A cold start must not fetch, then re-fetch. One request, total."""
    mocker.patch(
        "database.service.download_history",
        return_value=pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"]),
    )
    mock_qh = _mock_qh(mocker, _bars(_daily("2026-01-01", "2026-01-10"), seed=500.0))

    service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_qh.call_count == 1


# =====================================================================
# CASE B -- COMPLETE CACHE (the critical one)
# =====================================================================

def test_case_b_complete_cache_makes_zero_quanthub_requests(mocker, db_session):
    """A warm, fully-covered request must not contact QuantHub at all."""
    _seed_quanthub(db_session, _CORRA_H26, "DAILY", _WINDOW_START, _WINDOW_END_DT)

    mock_qh = _mock_qh(mocker)
    mock_lseg = mocker.patch("database.service.download_history")

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    mock_qh.assert_not_called()
    mock_lseg.assert_not_called()
    assert len(result) == 10  # served entirely from SQLite


def test_case_b_repeated_identical_requests_stay_at_zero(mocker, db_session):
    """Re-running the same request many times must not drift into
    re-downloading -- the scan-refresh case."""
    _seed_quanthub(db_session, _CORRA_H26, "DAILY", _WINDOW_START, _WINDOW_END_DT)
    mock_qh = _mock_qh(mocker)

    for _ in range(3):
        service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    mock_qh.assert_not_called()


def test_case_b_warm_batch_makes_zero_quanthub_requests(mocker, db_session):
    _seed_quanthub(db_session, _CORRA_H26, "DAILY", _WINDOW_START, _WINDOW_END_DT)
    _seed_quanthub(db_session, _CORRA_M26, "DAILY", _WINDOW_START, _WINDOW_END_DT)
    mock_qh_batch = mocker.patch("database.service.download_history_quanthub_batch")

    result = service.get_history_batch(
        [_CORRA_H26, _CORRA_M26], "DAILY", _WINDOW_START_STR, _WINDOW_END_STR
    )

    mock_qh_batch.assert_not_called()
    assert len(result[_CORRA_H26]) == 10
    assert len(result[_CORRA_M26]) == 10


# =====================================================================
# CASE C -- TAIL / INCREMENTAL UPDATE
# =====================================================================

def test_case_c_tail_gap_requests_only_the_missing_tail(mocker, db_session):
    """Cached through Jan 8; asked for Jan 1-10. QuantHub must be asked
    for Jan 9-10 and nothing else.

    This is the 98.8%-reduction case: before start/end support, the
    request below was for the entire Jan 1-10 window.
    """
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 8, 23, 59, 59, 999999),
    )
    mock_qh = _mock_qh(mocker, _bars(["2026-01-09", "2026-01-10"], seed=900.0))
    mock_lseg = mocker.patch("database.service.download_history")

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    mock_lseg.assert_not_called()
    assert mock_qh.call_count == 1
    start, end = _requested_range(mock_qh.call_args)
    assert start == datetime(2026, 1, 9)
    assert end == _WINDOW_END_DT
    # Explicitly NOT the full window.
    assert start != _WINDOW_START

    assert len(result) == 10
    by_date = result.set_index(result["Date"].dt.strftime("%Y-%m-%d"))["Close"]
    assert by_date["2026-01-01"] < 100.0   # original cached bar, untouched
    assert by_date["2026-01-09"] >= 900.0  # newly fetched


def test_case_c_single_bar_tail_gap_requests_only_that_bar(mocker, db_session):
    """The narrowest realistic incremental update: one missing day."""
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 9, 23, 59, 59, 999999),
    )
    mock_qh = _mock_qh(mocker, _bars(["2026-01-10"], seed=900.0))

    service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    start, end = _requested_range(mock_qh.call_args)
    assert start == datetime(2026, 1, 10)
    assert end == _WINDOW_END_DT


def test_case_c_batch_tail_gap_requests_only_the_missing_tail(mocker, db_session):
    """Same incremental guarantee through the batched entry point."""
    for ric in (_CORRA_H26, _CORRA_M26):
        _seed_quanthub(
            db_session, ric, "DAILY",
            _WINDOW_START, datetime(2026, 1, 8, 23, 59, 59, 999999),
        )

    mock_qh_batch = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end, use_date_range=False: {
            i: _bars(["2026-01-09", "2026-01-10"], seed=900.0) for i in instruments
        },
    )

    result = service.get_history_batch(
        [_CORRA_H26, _CORRA_M26], "DAILY", _WINDOW_START_STR, _WINDOW_END_STR
    )

    # Identical coverage -> both instruments share ONE request.
    assert mock_qh_batch.call_count == 1
    instruments, _interval, start, end = mock_qh_batch.call_args.args
    assert len(instruments) == 2
    assert start == datetime(2026, 1, 9)
    assert end == _WINDOW_END_DT
    assert mock_qh_batch.call_args.kwargs["use_date_range"] is True

    assert len(result[_CORRA_H26]) == 10
    assert len(result[_CORRA_M26]) == 10


# =====================================================================
# CASE D -- INTERIOR HOLE
# =====================================================================

def _seed_with_interior_hole(db_session, ric):
    """Coverage Jan 1-3 and Jan 8-10, with Jan 4-7 genuinely missing.

    Earliest/latest cached timestamps alone (Jan 1 .. Jan 10) cannot see
    this gap -- only sync_ranges coverage can, which is exactly why
    _missing_ranges is reused rather than an extent check.
    """
    cache.insert_bars(db_session, ric, "DAILY", _bars(_daily("2026-01-01", "2026-01-03"), seed=1.0))
    cache.record_sync_range(
        db_session, ric, "DAILY", _WINDOW_START, datetime(2026, 1, 3, 23, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )
    cache.insert_bars(db_session, ric, "DAILY", _bars(_daily("2026-01-08", "2026-01-10"), seed=1.0))
    cache.record_sync_range(
        db_session, ric, "DAILY", datetime(2026, 1, 8), _WINDOW_END_DT,
        provider=Provider.QUANTHUB.value,
    )


def test_case_d_interior_hole_requests_only_the_hole(mocker, db_session):
    _seed_with_interior_hole(db_session, _CORRA_H26)

    mock_qh = _mock_qh(mocker, _bars(_daily("2026-01-04", "2026-01-07"), seed=700.0))
    mock_lseg = mocker.patch("database.service.download_history")

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    mock_lseg.assert_not_called()
    assert mock_qh.call_count == 1  # one request for the one hole
    start, end = _requested_range(mock_qh.call_args)
    assert start == datetime(2026, 1, 4)
    # A gap ends where the NEXT coverage row begins -- _missing_ranges'
    # own long-standing boundary convention (gaps.append((cursor,
    # min(range_start, end)))), shared with the LSEG path. Asserted as
    # the literal value rather than recomputed, so a change to that
    # convention shows up here instead of being silently mirrored.
    assert end == datetime(2026, 1, 8)

    assert len(result) == 10
    assert int(result["Date"].duplicated().sum()) == 0
    by_date = result.set_index(result["Date"].dt.strftime("%Y-%m-%d"))["Close"]
    assert by_date["2026-01-05"] >= 700.0  # hole filled from the fetch
    assert by_date["2026-01-01"] < 100.0   # surrounding cache untouched


def test_case_d_hole_is_repaired_so_the_next_request_is_free(mocker, db_session):
    """After a repair the coverage must be genuinely complete, not
    merely appear complete in the returned frame."""
    _seed_with_interior_hole(db_session, _CORRA_H26)
    mock_qh = _mock_qh(mocker, _bars(_daily("2026-01-04", "2026-01-07"), seed=700.0))

    service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)
    assert mock_qh.call_count == 1

    service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)
    assert mock_qh.call_count == 1  # still 1 -- second request touched nothing

    remaining = service._missing_ranges(
        cache.get_sync_ranges(db_session, _CORRA_H26, "DAILY"), _WINDOW_START, _WINDOW_END_DT
    )
    assert remaining == []


def test_case_d_two_holes_are_fetched_as_two_separate_requests(mocker, db_session):
    """Two disjoint gaps must not be merged into one wide request that
    would re-download the cached stretch between them."""
    for span in (("2026-01-01", "2026-01-02"), ("2026-01-05", "2026-01-06"), ("2026-01-09", "2026-01-10")):
        cache.insert_bars(db_session, _CORRA_H26, "DAILY", _bars(_daily(*span), seed=1.0))
    for lo, hi in (
        (_WINDOW_START, datetime(2026, 1, 2, 23, 59, 59, 999999)),
        (datetime(2026, 1, 5), datetime(2026, 1, 6, 23, 59, 59, 999999)),
        (datetime(2026, 1, 9), _WINDOW_END_DT),
    ):
        cache.record_sync_range(
            db_session, _CORRA_H26, "DAILY", lo, hi, provider=Provider.QUANTHUB.value
        )

    mock_qh = _mock_qh(mocker, side_effect=lambda *a, **k: _bars(["2026-01-03"], seed=700.0))

    service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_qh.call_count == 2
    ranges = sorted(_requested_range(c) for c in mock_qh.call_args_list)
    # Each gap runs from just after one coverage row to the start of the
    # next (see the boundary-convention note above). Crucially, the
    # cached Jan 5-6 stretch BETWEEN the two gaps is never re-requested.
    assert ranges == [
        (datetime(2026, 1, 3), datetime(2026, 1, 5)),
        (datetime(2026, 1, 7), datetime(2026, 1, 9)),
    ]


# =====================================================================
# MULTIPLE INSTRUMENTS WITH DIFFERENT COVERAGE
# =====================================================================

def test_instruments_with_different_coverage_are_not_collapsed_into_one_range(mocker, db_session):
    """Two RICs, two different gaps. Neither may be widened to the
    other's -- that would re-download exactly the history this design
    exists to stop re-downloading.
    """
    # H26 is missing only the last two days...
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 8, 23, 59, 59, 999999),
    )
    # ...M26 is missing everything after Jan 2.
    _seed_quanthub(
        db_session, _CORRA_M26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 2, 23, 59, 59, 999999),
    )

    mock_qh_batch = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end, use_date_range=False: {
            i: _bars(_daily(start.strftime("%Y-%m-%d"), "2026-01-10"), seed=900.0)
            for i in instruments
        },
    )

    result = service.get_history_batch(
        [_CORRA_H26, _CORRA_M26], "DAILY", _WINDOW_START_STR, _WINDOW_END_STR
    )

    # Different signatures -> one request each, each with ONE instrument.
    assert mock_qh_batch.call_count == 2
    by_start = {c.args[2]: c for c in mock_qh_batch.call_args_list}
    assert set(by_start) == {datetime(2026, 1, 9), datetime(2026, 1, 3)}
    for call in mock_qh_batch.call_args_list:
        assert len(call.args[0]) == 1

    assert len(result[_CORRA_H26]) == 10
    assert len(result[_CORRA_M26]) == 10


def test_instruments_across_different_markets_keep_independent_coverage(mocker, db_session):
    """CORRA and SONIA are separate markets with separate calendars --
    one being warm must never suppress the other's fetch."""
    _seed_quanthub(db_session, _CORRA_H26, "DAILY", _WINDOW_START, _WINDOW_END_DT)  # fully warm
    _seed_quanthub(
        db_session, _SONIA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 8, 23, 59, 59, 999999),
    )

    mock_qh_batch = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end, use_date_range=False: {
            i: _bars(["2026-01-09", "2026-01-10"], seed=900.0) for i in instruments
        },
    )

    result = service.get_history_batch(
        [_CORRA_H26, _SONIA_H26], "DAILY", _WINDOW_START_STR, _WINDOW_END_STR
    )

    assert mock_qh_batch.call_count == 1
    instruments, _interval, start, _end = mock_qh_batch.call_args.args
    assert len(instruments) == 1          # only SONIA needed anything
    assert start == datetime(2026, 1, 9)
    assert len(result[_CORRA_H26]) == 10
    assert len(result[_SONIA_H26]) == 10


# =====================================================================
# INTERVALS: DAILY / HOURLY / FOUR_HOUR
# =====================================================================

def _hourly_bars(start: str, periods: int, seed=100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq="1h")
    return pd.DataFrame(
        {
            "Date": idx,
            "Open": [seed + i for i in range(periods)],
            "High": [seed + i + 1 for i in range(periods)],
            "Low": [seed + i - 1 for i in range(periods)],
            "Close": [seed + i + 0.5 for i in range(periods)],
            "Volume": [10 + i for i in range(periods)],
        }
    )


@pytest.mark.parametrize("interval", ["DAILY", "HOURLY", "4H"])
def test_tail_gap_is_incremental_at_every_interval(mocker, db_session, interval):
    """The incremental contract is interval-independent."""
    cache.record_sync_range(
        db_session, _CORRA_H26, interval,
        _WINDOW_START, datetime(2026, 1, 8, 23, 59, 59, 999999),
        provider=Provider.QUANTHUB.value,
    )
    mock_qh = _mock_qh(mocker, _hourly_bars("2026-01-09", 24, seed=900.0))

    service.get_history(_CORRA_H26, interval, _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_qh.call_count == 1
    start, end = _requested_range(mock_qh.call_args)
    assert start == datetime(2026, 1, 9)
    assert end == _WINDOW_END_DT


def test_four_hour_missing_range_still_spans_whole_days(mocker, db_session):
    """4H bars are synthesized by resampling hourly bars, so a request
    that began mid-bucket could produce a partial, wrong 4H bar.

    core.quanthub.download_history_batch collapses a date-range request
    to whole DAYS, and a day boundary is always a 4H boundary (24 % 4
    == 0), so every resampled bucket stays complete no matter where the
    gap itself started. This test pins the property that guarantee
    rests on: what reaches the provider still covers whole days.
    """
    cache.record_sync_range(
        db_session, _CORRA_H26, "4H",
        _WINDOW_START, datetime(2026, 1, 8, 10, 17, 33),  # deliberately mid-bucket
        provider=Provider.QUANTHUB.value,
    )
    captured = {}

    def _capture(instrument, interval_value, start, end, use_date_range=False):
        from core.utils import to_date
        captured["start_date"] = to_date(start)
        captured["end_date"] = to_date(end)
        captured["use_date_range"] = use_date_range
        return _hourly_bars("2026-01-08", 72, seed=900.0)

    mocker.patch("database.service.download_history_quanthub", side_effect=_capture)

    service.get_history(_CORRA_H26, "4H", _WINDOW_START_STR, _WINDOW_END_STR)

    assert captured["use_date_range"] is True
    # The provider receives whole-day bounds, which core.quanthub expands
    # to 00:00:00 -> 23:59:59 -- always 4H-aligned.
    assert captured["start_date"] == datetime(2026, 1, 8).date()
    assert captured["end_date"] == datetime(2026, 1, 10).date()


# =====================================================================
# EFFECTIVE REQUEST END -- currently-forming bars stay excluded
# =====================================================================

def test_incremental_fetch_never_reaches_into_the_forming_bar(mocker, db_session):
    """The missing range handed to QuantHub must respect
    _effective_request_end exactly as before: a request through "now"
    stops at the last CLOSED bar."""
    now = datetime.utcnow()
    boundary = service._last_completed_boundary(service.BarInterval.DAILY, now)
    start = (now - timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0)

    mocker.patch(
        "database.service.download_history",
        return_value=pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"]),
    )
    mock_qh = _mock_qh(mocker, _bars([start.strftime("%Y-%m-%d")], seed=500.0))

    service.get_history(_CORRA_H26, "DAILY", start, now)

    _req_start, req_end = _requested_range(mock_qh.call_args)
    assert req_end < boundary  # never into the bar still forming now


def test_second_request_during_the_same_forming_period_makes_no_call(mocker, db_session):
    """Existing behaviour, re-proved on the incremental path: repeating
    a request before the current bar closes adds zero provider calls."""
    now = datetime.utcnow()
    start = (now - timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = service._last_completed_boundary(service.BarInterval.DAILY, now)
    cache.record_sync_range(
        db_session, _CORRA_H26, "DAILY", start, boundary - timedelta(microseconds=1),
        provider=Provider.QUANTHUB.value,
    )
    mock_qh = _mock_qh(mocker)

    service.get_history(_CORRA_H26, "DAILY", start, now)
    service.get_history(_CORRA_H26, "DAILY", start, now)

    mock_qh.assert_not_called()


# =====================================================================
# OVERLAP / DUPLICATES
# =====================================================================

def test_overlapping_refetch_creates_no_duplicate_timestamps(mocker, db_session):
    """A provider that returns more than the gap asked for -- which a
    whole-day-expanded request routinely does -- must not duplicate
    already-cached bars. The database-level UniqueConstraint plus
    ON CONFLICT DO NOTHING is what absorbs it."""
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 8, 23, 59, 59, 999999),
    )
    # Deliberately overlaps the cached Jan 1-8 as well as the Jan 9-10 gap.
    overlapping = _bars(_daily("2026-01-01", "2026-01-10"), seed=900.0)
    _mock_qh(mocker, overlapping)

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert len(result) == 10
    assert int(result["Date"].duplicated().sum()) == 0
    # Pre-existing values win: the upsert skips conflicts, it does not
    # overwrite. A cached bar is never silently rewritten by an overlap.
    by_date = result.set_index(result["Date"].dt.strftime("%Y-%m-%d"))["Close"]
    assert by_date["2026-01-01"] < 100.0


def test_repeated_incremental_cycles_do_not_accumulate_rows(mocker, db_session):
    """Three successive tail updates must leave exactly one row per
    timestamp, not three."""
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 8, 23, 59, 59, 999999),
    )
    _mock_qh(mocker, _bars(_daily("2026-01-01", "2026-01-10"), seed=900.0))

    for _ in range(3):
        result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert len(result) == 10
    assert int(result["Date"].duplicated().sum()) == 0


# =====================================================================
# THE 10,000-ROW CEILING IS NOT WORKED AROUND HERE
# =====================================================================

def test_row_limit_error_from_a_wide_gap_propagates(mocker, db_session):
    """A gap too wide for QuantHub's response ceiling surfaces the
    provider's own error. Automatic chunking is deliberately NOT part
    of this layer -- swallowing or splitting the request here would
    hide the condition a later chunking task needs to see."""
    import requests

    _seed_quanthub(
        db_session, _CORRA_H26, "HOURLY",
        _WINDOW_START, datetime(2026, 1, 2, 23, 59, 59, 999999),
    )
    mocker.patch(
        "database.service.download_history_quanthub",
        side_effect=requests.exceptions.HTTPError('400 {"error": "Max row limit exceeded (10000)"}'),
    )

    with pytest.raises(requests.exceptions.HTTPError, match="Max row limit exceeded"):
        service.get_history(_CORRA_H26, "HOURLY", _WINDOW_START_STR, _WINDOW_END_STR)


# =====================================================================
# TASK 4: automatic chunking underneath the incremental cache
#
# These tests mock the QuantHub HTTP layer (core.quanthub.requests.get)
# rather than database.service's imported download function, so the REAL
# chunker runs inside the REAL cache flow. That is the point: chunking
# lives in the provider, beneath _missing_ranges, and the cache must be
# unaware of it beyond receiving complete data.
#
# The rate limiter is neutralised the same way tests/test_quanthub.py
# does -- kept active, but with free waits.
# =====================================================================

def _qh_records(product: str, start: str, end: str, seed: float = 100.0):
    """Daily records in QuantHub's raw wire shape (unix-ms `time`)."""
    idx = pd.date_range(start, end, freq="D")
    return [
        {
            "product": product,
            "time": int(ts.value // 10**6),
            "open": seed + i, "high": seed + i + 1, "low": seed + i - 1,
            "close": seed + i + 0.5, "volume": 10 + i,
        }
        for i, ts in enumerate(idx)
    ]


def _http_response(*, json_body=None, status_code=200, text=""):
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.headers = {}
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


def _row_limit_http_response():
    return _http_response(
        status_code=400, text='{"error": "Max row limit exceeded (10000)"}'
    )


@pytest.fixture
def free_rate_limiter(monkeypatch):
    """Keep the Task 3 limiter active but make its waits free, so these
    tests exercise real pacing logic without real delays."""
    from core import quanthub

    monkeypatch.setattr(
        quanthub,
        "_RATE_LIMITER",
        quanthub.QuantHubRateLimiter(
            quanthub.QUANTHUB_REQUESTS_PER_MINUTE, sleep=lambda _s: None
        ),
    )


def _requested_http_windows(mock_get):
    return [
        (
            pd.to_datetime(c.kwargs["params"]["start"], unit="s"),
            pd.to_datetime(c.kwargs["params"]["end"], unit="s"),
        )
        for c in mock_get.call_args_list
    ]


def test_oversized_missing_range_is_filled_through_multiple_qh_requests(
    mocker, db_session, free_rate_limiter
):
    """Cache integration end to end: one missing range, several HTTP
    requests underneath, complete coverage afterwards."""
    mocker.patch(
        "core.quanthub._auth_headers", return_value={"Authorization": "Bearer test"}
    )
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 2, 23, 59, 59, 999999),
    )

    # The gap is Jan 3 -> Jan 10. The first request is rejected for
    # exceeding the row ceiling, so the provider splits it.
    mock_get = mocker.patch(
        "core.quanthub.requests.get",
        side_effect=[
            _row_limit_http_response(),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-03", "2026-01-06", seed=900.0)),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-07", "2026-01-10", seed=900.0)),
        ],
    )

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_get.call_count == 3  # 1 rejected + 2 successful halves
    assert len(result) == 10
    assert int(result["Date"].duplicated().sum()) == 0

    # Coverage is complete afterwards -- the cache, not just the frame.
    assert service._missing_ranges(
        cache.get_sync_ranges(db_session, _CORRA_H26, "DAILY"),
        _WINDOW_START, _WINDOW_END_DT,
    ) == []


def test_chunking_only_covers_the_missing_range_not_the_cached_part(
    mocker, db_session, free_rate_limiter
):
    """Chunking operates UNDERNEATH _missing_ranges: already-cached days
    are never re-requested, however the missing part gets split."""
    mocker.patch(
        "core.quanthub._auth_headers", return_value={"Authorization": "Bearer test"}
    )
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 6, 23, 59, 59, 999999),
    )

    mock_get = mocker.patch(
        "core.quanthub.requests.get",
        side_effect=[
            _row_limit_http_response(),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-07", "2026-01-08", seed=900.0)),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-09", "2026-01-10", seed=900.0)),
        ],
    )

    service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    windows = _requested_http_windows(mock_get)
    # Nothing before Jan 7 is ever requested -- Jan 1-6 stays cached.
    assert min(w[0] for w in windows) >= pd.Timestamp("2026-01-07")


def test_interior_hole_is_chunked_without_touching_surrounding_cache(
    mocker, db_session, free_rate_limiter
):
    """Case D from Task 2, now with an oversized hole."""
    mocker.patch(
        "core.quanthub._auth_headers", return_value={"Authorization": "Bearer test"}
    )
    _seed_with_interior_hole(db_session, _CORRA_H26)  # gap is Jan 4 -> Jan 8

    mock_get = mocker.patch(
        "core.quanthub.requests.get",
        side_effect=[
            _row_limit_http_response(),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-04", "2026-01-05", seed=700.0)),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-06", "2026-01-08", seed=700.0)),
        ],
    )

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    windows = _requested_http_windows(mock_get)
    assert min(w[0] for w in windows) >= pd.Timestamp("2026-01-04")
    assert max(w[1] for w in windows) <= pd.Timestamp("2026-01-08 23:59:59")

    assert len(result) == 10
    assert int(result["Date"].duplicated().sum()) == 0


def test_warm_cache_still_makes_zero_requests_with_chunking_present(
    mocker, db_session, free_rate_limiter
):
    """Chunking must not cause any request when nothing is missing."""
    _seed_quanthub(db_session, _CORRA_H26, "DAILY", _WINDOW_START, _WINDOW_END_DT)
    mock_get = mocker.patch("core.quanthub.requests.get")

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    mock_get.assert_not_called()
    assert len(result) == 10


def test_each_missing_range_is_chunked_independently(
    mocker, db_session, free_rate_limiter
):
    """Two separate gaps: _missing_ranges still yields two ranges, and
    each is fetched (and split) on its own -- never merged into one wide
    request spanning the cached stretch between them."""
    mocker.patch(
        "core.quanthub._auth_headers", return_value={"Authorization": "Bearer test"}
    )
    for span in (("2026-01-01", "2026-01-02"), ("2026-01-05", "2026-01-06"), ("2026-01-09", "2026-01-10")):
        cache.insert_bars(db_session, _CORRA_H26, "DAILY", _bars(_daily(*span), seed=1.0))
    for lo, hi in (
        (_WINDOW_START, datetime(2026, 1, 2, 23, 59, 59, 999999)),
        (datetime(2026, 1, 5), datetime(2026, 1, 6, 23, 59, 59, 999999)),
        (datetime(2026, 1, 9), _WINDOW_END_DT),
    ):
        cache.record_sync_range(
            db_session, _CORRA_H26, "DAILY", lo, hi, provider=Provider.QUANTHUB.value
        )

    mock_get = mocker.patch(
        "core.quanthub.requests.get",
        side_effect=[
            _http_response(json_body=_qh_records("CRAH26", "2026-01-03", "2026-01-04", seed=700.0)),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-07", "2026-01-08", seed=700.0)),
        ],
    )

    result = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_get.call_count == 2
    windows = sorted(_requested_http_windows(mock_get))
    # Two distinct gaps; the cached Jan 5-6 between them is never fetched.
    assert windows[0][0] == pd.Timestamp("2026-01-03")
    assert windows[1][0] == pd.Timestamp("2026-01-07")
    assert len(result) == 10


def test_a_non_row_limit_400_from_the_cache_path_is_not_chunked(
    mocker, db_session, free_rate_limiter
):
    """An unrelated deterministic rejection must surface once, not turn
    into a burst of split requests."""
    mocker.patch(
        "core.quanthub._auth_headers", return_value={"Authorization": "Bearer test"}
    )
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 2, 23, 59, 59, 999999),
    )
    mock_get = mocker.patch(
        "core.quanthub.requests.get",
        return_value=_http_response(status_code=400, text='{"error": "Invalid instrument"}'),
    )

    with pytest.raises(requests.exceptions.HTTPError):
        service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_get.call_count == 1


def test_chunked_cache_fill_is_persisted_and_the_next_request_is_free(
    mocker, db_session, free_rate_limiter
):
    """Whatever chunking did underneath, the cache ends up complete, so
    an identical follow-up request contacts nobody."""
    mocker.patch(
        "core.quanthub._auth_headers", return_value={"Authorization": "Bearer test"}
    )
    _seed_quanthub(
        db_session, _CORRA_H26, "DAILY",
        _WINDOW_START, datetime(2026, 1, 2, 23, 59, 59, 999999),
    )
    mock_get = mocker.patch(
        "core.quanthub.requests.get",
        side_effect=[
            _row_limit_http_response(),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-03", "2026-01-06", seed=900.0)),
            _http_response(json_body=_qh_records("CRAH26", "2026-01-07", "2026-01-10", seed=900.0)),
        ],
    )

    first = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)
    calls_after_first = mock_get.call_count

    second = service.get_history(_CORRA_H26, "DAILY", _WINDOW_START_STR, _WINDOW_END_STR)

    assert mock_get.call_count == calls_after_first  # zero further requests
    pd.testing.assert_frame_equal(first, second)
