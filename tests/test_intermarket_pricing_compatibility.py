"""
tests/test_intermarket_pricing_compatibility.py

The load-bearing integration test for the intermarket domain-model
phase: proves that a manually-generated IntermarketStrategyInstance
(SOFR leg + SONIA leg) flows through the EXISTING, COMPLETELY UNMODIFIED
strategy_engine.pricing.prewarm_leg_cache()/build_history() pipeline
with no code change needed, exactly as the engineering assessment
predicted (build_history never reads market_key; leg_cache is keyed
purely on (ric, interval, price_start, price_end)).

Follows the same "mock the provider-facing download function, use a
tmp_path-backed SQLite cache" approach as tests/test_service_get_
history_batch.py -- SOFR (no QuantHub mapping) resolves via LSEG
directly; SONIA (QuantHub-mapped) has its LSEG attempt fail (a
deterministic stand-in for "LSEG could not provide complete history",
same convention used throughout tests/test_service_provider_
fallback.py) and falls back to QuantHub -- so the two legs of ONE
intermarket instance are proven to route to two DIFFERENT providers
independently, through the existing, unmodified provider-provenance
machinery, with no strategy-level provider decision anywhere.

Price dates are deliberately in the past relative to the sandbox's
current date (2026-08-26) so this test never interacts with Module 8's
"currently-forming-bar" effective-request-end capping -- that is
orthogonal to what this test is proving.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import BarInterval
from core.downloader import MarketDataUnavailableError
from database import cache
from database.connection import get_session as _real_get_session
from database import service
from strategy_engine.intermarket_combinations import generate_intermarket_instances
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec
from strategy_engine.pricing import build_history, prewarm_leg_cache

_SOFR_RIC = "SRAH26"
_SONIA_RIC = "SONH6"


@pytest.fixture(autouse=True)
def _route_service_sessions_to_test_engine(monkeypatch, db_engine):
    monkeypatch.setattr(service, "get_session", lambda: _real_get_session(db_engine))
    yield


def _make_df(dates: list[str], close: float) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": [close] * n,
            "High": [close] * n,
            "Low": [close] * n,
            "Close": [close] * n,
            "Volume": [1000.0] * n,
        }
    )


def _sofr_vs_sonia_instance():
    definition = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("SONIA", 0, -1.0)),
        interval=BarInterval.DAILY,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-03-31")
    assert instances and instances[0].rics == (_SOFR_RIC, _SONIA_RIC)
    return instances[0]


def _lseg_side_effect(ric, interval, start, end):
    if ric == _SOFR_RIC:
        return _make_df(["2026-02-02"], close=96.80)
    if ric == _SONIA_RIC:
        raise MarketDataUnavailableError(ric, "The universe is not found")
    raise AssertionError(f"unexpected ric requested from LSEG: {ric}")


def test_intermarket_instance_routes_each_leg_to_its_own_provider_and_combines_correctly(mocker):
    instance = _sofr_vs_sonia_instance()

    mock_lseg = mocker.patch("database.service.download_history", side_effect=_lseg_side_effect)
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(["2026-02-02"], close=1.15) for instr in instruments
        },
    )
    mock_get_history = mocker.patch("strategy_engine.pricing.get_history")

    # 1/2/5: both RICs fetched, each independently provider-routed, via
    # ONE prewarm call -- never a strategy-level provider decision.
    leg_cache = prewarm_leg_cache([instance], "2026-02-01", "2026-02-03")

    lseg_rics_called = {c.args[0] for c in mock_lseg.call_args_list}
    assert lseg_rics_called == {_SOFR_RIC, _SONIA_RIC}
    assert mock_qh.call_count == 1  # SONIA fell back to QuantHub, exactly once, batched

    # 6: build_history/_fetch_leg require no modification -- the
    # pre-warmed leg_cache is consumed by the totally unmodified function.
    history = build_history(instance, "2026-02-01", "2026-02-03", leg_cache=leg_cache)

    # 5 (continued): no duplicate provider fetch -- build_history never
    # falls back to the single-ric get_history() path since prewarm
    # already populated both keys.
    mock_get_history.assert_not_called()

    # 4: Strategy = SOFR price - SONIA price (weights +1/-1).
    assert history.history["Leg_1"].tolist() == [96.80]
    assert history.history["Leg_2"].tolist() == [1.15]
    assert history.history["Strategy"].tolist() == pytest.approx([96.80 - 1.15])


def test_intermarket_legs_persist_independent_cache_and_provenance(mocker, db_session):
    instance = _sofr_vs_sonia_instance()
    mocker.patch("database.service.download_history", side_effect=_lseg_side_effect)
    mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(["2026-02-02"], close=1.15) for instr in instruments
        },
    )

    prewarm_leg_cache([instance], "2026-02-01", "2026-02-03")

    # 3: cache keys (sync_ranges/price_bars) remain fully independent
    # per (ric, interval) -- confirmed at the persistence layer, not
    # just the in-memory leg_cache dict.
    sofr_ranges = cache.get_sync_ranges(db_session, _SOFR_RIC, "DAILY")
    sonia_ranges = cache.get_sync_ranges(db_session, _SONIA_RIC, "DAILY")
    assert len(sofr_ranges) == 1
    assert len(sonia_ranges) == 1

    # Established provider differs per leg -- proves there is no
    # strategy-level/instance-level provider decision anywhere. SOFR has
    # no QuantHub mapping at all (core.providers.PROVIDER_ROUTING), so
    # the provenance state machine is skipped entirely for it (provider
    # stays None forever, per Module 8's documented behavior for any
    # LSEG-only market); SONIA IS QuantHub-mapped, so its (ric, interval)
    # goes through the establishment flow and records "QUANTHUB" once
    # LSEG proves incomplete. This asymmetry -- one leg tracked, one
    # not -- is itself proof that provenance is a pure per-(ric,
    # interval) decision with no awareness of the strategy either leg
    # belongs to.
    assert cache.get_established_provider(db_session, _SOFR_RIC, "DAILY") is None
    assert cache.get_established_provider(db_session, _SONIA_RIC, "DAILY") == "QUANTHUB"
