"""
tests/test_service_get_history_batch.py

Unit tests for database.service.get_history_batch() -- the batched,
many-RICs-at-once entry point added for the QuantHub-batching phase
(see strategy_engine.pricing.prewarm_leg_cache). Exercises provider
isolation for markets with NO QuantHub mapping (LSEG-only rics never
reach the QuantHub mock and vice versa), deduplication, the
MarketDataUnavailableError-omission contract for LSEG-only rics, and
that any other exception (e.g. QuantHubRateLimitError) propagates
uncaught.

QuantHub-MAPPED rics (CORRA/SONIA) now go through the cache -> LSEG ->
QuantHub fallback (see database.service's module docstring) instead of
skipping straight to QuantHub -- LSEG is mocked here to raise
MarketDataUnavailableError for these two specific rics (a deterministic
stand-in for "LSEG could not provide complete history", the same signal
CORRA's real, documented entitlement gap produces) so the QuantHub
fallback path is reached exactly as before this design existed. The
detailed completeness-decision matrix (LSEG complete/incomplete/
unavailable, batching preservation, cache-mixing prevention) lives in
tests/test_service_provider_fallback.py; this file keeps the original
routing/dedup/exception-propagation coverage, updated for the new flow.

Follows the same "mock the provider-facing download function, use a
tmp_path-backed SQLite cache" approach as tests/test_service.py and
tests/test_service_provider_routing.py.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from core.downloader import MarketDataUnavailableError
from core.quanthub import QuantHubRateLimitError
from core.ric import build_ric
from database import cache, service
from database.connection import get_session as _real_get_session

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]

_CORRA_RIC = build_ric("CORRA", 3, 2026)
_SONIA_RIC = build_ric("SONIA", 6, 2026)
_SOFR_H26 = build_ric("SOFR", 3, 2026)
_SOFR_M26 = build_ric("SOFR", 6, 2026)


@pytest.fixture(autouse=True)
def _route_service_sessions_to_test_engine(monkeypatch, db_engine):
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


def _lseg_unavailable_for_qh_mapped_rics(ric, interval, start, end):
    """download_history side_effect: raises MarketDataUnavailableError
    for the two QuantHub-mapped test rics (a deterministic stand-in for
    "LSEG could not provide complete history"), returns a valid frame
    for anything else (e.g. SOFR) -- so the QuantHub fallback path is
    reached for CORRA/SONIA exactly like it was reached unconditionally
    before this fallback design existed."""
    if ric in (_CORRA_RIC, _SONIA_RIC):
        raise MarketDataUnavailableError(ric, "The universe is not found")
    return _make_df(["2026-01-01"])


# ---------------------------------------------------------------------
# Provider isolation: LSEG rics and QuantHub rics in the SAME call
# ---------------------------------------------------------------------

def test_mixed_batch_routes_lseg_and_quanthub_rics_independently(mocker):
    # SOFR (no QuantHub mapping) resolves via LSEG directly, unchanged.
    # CORRA (QuantHub-mapped) now has its LSEG attempt tried FIRST too
    # (per the cache -> LSEG -> QuantHub fallback design) -- the mock
    # raises MarketDataUnavailableError for CORRA specifically, so it
    # still falls through to QuantHub exactly as before this design
    # existed, just via an extra LSEG attempt first.
    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=_lseg_unavailable_for_qh_mapped_rics,
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(["2026-01-01"]) for instr in instruments
        },
    )

    result = service.get_history_batch(
        [_SOFR_H26, _CORRA_RIC], "DAILY", "2026-01-01", "2026-01-01"
    )

    assert set(result) == {_SOFR_H26, _CORRA_RIC}
    lseg_rics_called = {c.args[0] for c in mock_lseg.call_args_list}
    assert lseg_rics_called == {_SOFR_H26, _CORRA_RIC}
    assert mock_qh.call_count == 1
    assert mock_qh.call_args[0][0] == ["CRAH26"]  # CORRA's QH instrument, never the LSEG RIC


def test_qh_mapped_ric_lseg_attempt_uses_the_lseg_ric_not_a_qh_instrument(mocker):
    # The LSEG attempt for a QuantHub-mapped market must still pass the
    # RIC string itself to core.downloader -- never a QH instrument
    # identifier -- even though this same contract may end up resolved
    # by QuantHub afterward.
    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=_lseg_unavailable_for_qh_mapped_rics,
    )
    mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(["2026-01-01"]) for instr in instruments
        },
    )

    service.get_history_batch([_CORRA_RIC], "DAILY", "2026-01-01", "2026-01-01")

    mock_lseg.assert_called_once_with(_CORRA_RIC, "DAILY", mocker.ANY, mocker.ANY)


def test_lseg_ric_never_sent_to_quanthub_batch(mocker):
    mocker.patch("database.service.download_history", return_value=_make_df(["2026-01-01"]))
    mock_qh = mocker.patch("database.service.download_history_quanthub_batch")

    service.get_history_batch([_SOFR_H26], "DAILY", "2026-01-01", "2026-01-01")

    mock_qh.assert_not_called()


# ---------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------

def test_duplicate_rics_fetched_once_each(mocker):
    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=_lseg_unavailable_for_qh_mapped_rics,
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(["2026-01-01"]) for instr in instruments
        },
    )

    result = service.get_history_batch(
        [_SOFR_H26, _SOFR_H26, _CORRA_RIC, _CORRA_RIC], "DAILY", "2026-01-01", "2026-01-01"
    )

    assert set(result) == {_SOFR_H26, _CORRA_RIC}
    # Each unique ric's LSEG attempt happens exactly once: SOFR (resolved
    # via LSEG) and CORRA (rejected, falls back to QuantHub) -- never
    # once per duplicate input entry.
    assert mock_lseg.call_count == 2
    assert mock_qh.call_count == 1
    assert mock_qh.call_args[0][0] == ["CRAH26"]


# ---------------------------------------------------------------------
# QuantHub half: cache-first behaviour (mirrors get_history()'s own
# cache-first contract, since _get_history_batch_quanthub is new code
# rather than a delegation to get_history()).
# ---------------------------------------------------------------------

def test_quanthub_fully_cached_ric_does_not_call_batch_downloader(db_session, mocker):
    seeded = _make_df(["2026-01-01", "2026-01-02"])
    cache.insert_bars(db_session, _CORRA_RIC, "DAILY", seeded)
    cache.record_sync_range(
        db_session, _CORRA_RIC, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 2, 23, 59, 59, 999999)
    )

    mock_qh = mocker.patch("database.service.download_history_quanthub_batch")

    result = service.get_history_batch([_CORRA_RIC], "DAILY", "2026-01-01", "2026-01-02")

    assert mock_qh.call_count == 0
    assert len(result[_CORRA_RIC]) == 2


def test_quanthub_batch_only_requests_rics_needing_a_fetch(mocker, db_session):
    # CORRA already fully cached, SONIA is not -- only SONIA's instrument
    # should reach download_history_quanthub_batch. CORRA being fully
    # cached means its LSEG attempt is never even made (see the "already
    # fully cached -- no provider attempt at all" branch), so no LSEG
    # mock is needed for it; SONIA's LSEG attempt is mocked to fail so
    # it reaches the QuantHub fallback deterministically.
    seeded = _make_df(["2026-01-01"])
    cache.insert_bars(db_session, _CORRA_RIC, "DAILY", seeded)
    cache.record_sync_range(
        db_session, _CORRA_RIC, "DAILY", datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59, 59, 999999)
    )

    mocker.patch(
        "database.service.download_history",
        side_effect=_lseg_unavailable_for_qh_mapped_rics,
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(["2026-01-01"]) for instr in instruments
        },
    )

    result = service.get_history_batch(
        [_CORRA_RIC, _SONIA_RIC], "DAILY", "2026-01-01", "2026-01-01"
    )

    assert mock_qh.call_count == 1
    assert mock_qh.call_args[0][0] == ["SONM26"]
    assert set(result) == {_CORRA_RIC, _SONIA_RIC}


def test_quanthub_batch_persists_results_into_the_cache(mocker, db_session):
    mocker.patch(
        "database.service.download_history",
        side_effect=_lseg_unavailable_for_qh_mapped_rics,
    )
    mocker.patch(
        "database.service.download_history_quanthub_batch",
        return_value={"CRAH26": _make_df(["2026-01-01", "2026-01-02"])},
    )

    service.get_history_batch([_CORRA_RIC], "DAILY", "2026-01-01", "2026-01-02")

    ranges = cache.get_sync_ranges(db_session, _CORRA_RIC, "DAILY")
    assert len(ranges) == 1


# ---------------------------------------------------------------------
# MarketDataUnavailableError (LSEG only): omitted, not raised
# ---------------------------------------------------------------------

def test_lseg_unavailable_ric_is_omitted_not_raised(mocker):
    mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(_SOFR_H26, "The universe is not found"),
    )

    result = service.get_history_batch([_SOFR_H26], "DAILY", "2026-01-01", "2026-01-01")

    assert result == {}


def test_lseg_unavailable_ric_does_not_prevent_other_rics_from_resolving(mocker):
    def _download(ric, interval, start, end):
        if ric == _SOFR_H26:
            raise MarketDataUnavailableError(ric, "The universe is not found")
        return _make_df(["2026-01-01"])

    mocker.patch("database.service.download_history", side_effect=_download)
    mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(["2026-01-01"]) for instr in instruments
        },
    )

    result = service.get_history_batch(
        [_SOFR_H26, _SOFR_M26, _CORRA_RIC], "DAILY", "2026-01-01", "2026-01-01"
    )

    assert set(result) == {_SOFR_M26, _CORRA_RIC}


# ---------------------------------------------------------------------
# Any other exception (e.g. a QuantHub rate-limit) propagates uncaught
# ---------------------------------------------------------------------

def test_quanthub_rate_limit_error_propagates_uncaught(mocker):
    mocker.patch(
        "database.service.download_history",
        side_effect=_lseg_unavailable_for_qh_mapped_rics,
    )
    mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=QuantHubRateLimitError("rate limited"),
    )

    with pytest.raises(QuantHubRateLimitError):
        service.get_history_batch([_CORRA_RIC], "DAILY", "2026-01-01", "2026-01-01")


def test_unrelated_lseg_exception_propagates_uncaught(mocker):
    mocker.patch("database.service.download_history", side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        service.get_history_batch([_SOFR_H26], "DAILY", "2026-01-01", "2026-01-01")


# ---------------------------------------------------------------------
# start > end validation, same contract as get_history()
# ---------------------------------------------------------------------

def test_get_history_batch_start_after_end_raises():
    with pytest.raises(ValueError, match="start .* must be <= end"):
        service.get_history_batch([_SOFR_H26], "DAILY", "2026-01-10", "2026-01-01")
