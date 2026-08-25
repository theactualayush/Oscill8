"""
tests/test_service_provider_routing.py

Integration-level tests of database.service.get_history's provider
dispatch (core.providers.resolve_provider, keyed off core.ric.parse_ric
applied to the RIC): a market with NO QuantHub mapping (e.g. SOFR) must
keep using LSEG exactly as before and remain fully usable with no
QuantHub credentials configured at all. A QuantHub-MAPPED market
(CORRA/SONIA) now goes through the cache -> LSEG -> QuantHub fallback
(see database.service's module docstring) -- LSEG is tried FIRST, and
QuantHub is only used when LSEG cannot provide complete history for the
contract (see tests/test_service_provider_fallback.py for the detailed
completeness-decision test matrix; this file keeps only the routing-
level smoke tests).

Follows the same "mock the provider-facing download function, use a
tmp_path-backed SQLite cache" approach as tests/test_service.py.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core import config
from core.quanthub import QuantHubCredentialsMissingError
from core.ric import build_ric
from database import service
from database.connection import get_session as _real_get_session

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]

# CORRA/SONIA both use ric_year_digits=1 -- build via core.ric.build_ric
# so these RICs are always valid regardless of the current year.
_CORRA_RIC = build_ric("CORRA", 3, 2026)
_SONIA_RIC = build_ric("SONIA", 3, 2026)
_SOFR_RIC = build_ric("SOFR", 3, 2026)


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


def test_corra_falls_back_to_quanthub_when_lseg_unavailable(mocker):
    # Reproduces CORRA's real, documented entitlement gap: LSEG raises
    # MarketDataUnavailableError -- the fallback design tries LSEG
    # first, then falls back to QuantHub for the FULL requested window.
    from core.downloader import MarketDataUnavailableError

    mock_lseg = mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(_CORRA_RIC, "User does not have permission"),
    )
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_make_df(["2026-01-01", "2026-01-02"]),
    )

    result = service.get_history(_CORRA_RIC, "DAILY", "2026-01-01", "2026-01-02")

    assert mock_lseg.call_count == 1
    assert mock_qh.call_count == 1
    assert list(result.columns) == _CANONICAL_COLUMNS
    # QuantHub instrument is built from the QH root ("CRA", identical to
    # CORRA's LSEG root here) + the same month/year -- never the RIC
    # string itself.
    assert mock_qh.call_args[0][0] == "CRAH26"


def test_sonia_falls_back_to_quanthub_when_lseg_incomplete(mocker):
    # LSEG returns a genuinely incomplete frame (a real interior gap, far
    # wider than any normal holiday) -- the fallback design tries LSEG
    # first, rejects it, and falls back to QuantHub for the FULL window.
    gappy = pd.concat(
        [_make_df(["2026-01-01"]), _make_df(["2026-03-01"], seed=200.0)], ignore_index=True
    )
    mock_lseg = mocker.patch("database.service.download_history", return_value=gappy)
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub",
        return_value=_make_df(["2026-01-01"]),
    )

    service.get_history(_SONIA_RIC, "DAILY", "2026-01-01", "2026-03-01")

    assert mock_lseg.call_count == 1
    assert mock_qh.call_count == 1
    # SONIA's QH root is "SON" -- the QuantHub instrument uses a 2-digit
    # year (verified convention) even though the SONIA RIC itself
    # ("SONH6") uses LSEG's 1-digit-year convention.
    assert mock_qh.call_args[0][0] == "SONH26"


def test_sofr_still_routes_to_lseg_unchanged(mocker):
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_make_df(["2026-01-01"]),
    )
    mock_qh = mocker.patch("database.service.download_history_quanthub")

    result = service.get_history(_SOFR_RIC, "DAILY", "2026-01-01", "2026-01-01")

    assert mock_lseg.call_count == 1
    assert mock_qh.call_count == 0
    assert mock_lseg.call_args[0][0] == _SOFR_RIC
    assert len(result) == 1


def test_lseg_routed_market_unaffected_by_missing_quanthub_credentials(mocker, monkeypatch):
    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "")
    mock_lseg = mocker.patch(
        "database.service.download_history",
        return_value=_make_df(["2026-01-01"]),
    )

    result = service.get_history(_SOFR_RIC, "DAILY", "2026-01-01", "2026-01-01")

    assert mock_lseg.call_count == 1
    assert len(result) == 1


def test_quanthub_fallback_raises_clear_error_with_no_credentials(mocker, monkeypatch):
    from core.downloader import MarketDataUnavailableError

    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "")
    mocker.patch(
        "database.service.download_history",
        side_effect=MarketDataUnavailableError(_CORRA_RIC, "User does not have permission"),
    )
    mocker.patch("core.quanthub.requests.get")  # must never be reached

    with pytest.raises(QuantHubCredentialsMissingError):
        service.get_history(_CORRA_RIC, "DAILY", "2026-01-01", "2026-01-01")
