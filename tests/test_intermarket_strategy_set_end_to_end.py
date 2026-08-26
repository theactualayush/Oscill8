"""
tests/test_intermarket_strategy_set_end_to_end.py

Phase 2's most important integration test: the complete chain

    StrategySet JSON
        -> strategy_set_from_json()  (deserialize)
        -> StrategySet
        -> expand_strategy_set()
        -> intermarket instances generated (alongside single-market ones)
        -> pricing/cache (strategy_engine.pricing, database.service --
           real per-RIC provider routing/cache, tmp_path-backed SQLite)
        -> template_scanner.scanner.run_scan_on_instances()
        -> range_analytics (via analyze_histories()/analyze_multi_lookback())
        -> ScanCandidateResult

using a GENUINELY MIXED StrategySet (>= 1 single-market entry, >= 1
intermarket entry) built from real markets already in core.config.MARKETS
-- used here purely as test data, not because the engine has any
special-cased logic for them. Swapping in any other pair of registered
markets would exercise the exact same code paths.

Follows the same "mock the provider-facing download function, use a
tmp_path-backed SQLite cache" approach as tests/test_intermarket_pricing_
compatibility.py (Phase 1) and tests/test_service_provider_fallback.py.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from core.downloader import MarketDataUnavailableError
from database import cache, service
from database.connection import get_session as _real_get_session

from strategy_engine.combinations import StrategyInstance
from strategy_engine.intermarket_combinations import IntermarketStrategyInstance
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.serialization import strategy_set_from_json
from template_scanner.scanner import run_scan_on_instances

# Contract/price windows are deliberately in the past relative to the
# sandbox's current date (2026-08-26) so this test never interacts with
# Module 8's "currently-forming-bar" effective-request-end capping --
# orthogonal to what this test proves. Contract window covers exactly
# two quarterly months (March/June 2026) for both markets used.
_CONTRACT_START, _CONTRACT_END = "2026-01-01", "2026-06-30"
_PRICE_START, _PRICE_END = "2026-02-02", "2026-02-13"
_PRICE_DATES = pd.date_range(_PRICE_START, _PRICE_END, freq="D").strftime("%Y-%m-%d").tolist()

_SOFR_H26, _SOFR_M26 = "SRAH26", "SRAM26"
_CORRA_H6, _CORRA_M6 = "CRAH6", "CRAM6"

# A MIXED StrategySet: one single-market entry (an ordinary SOFR
# outright) and one intermarket entry (a SOFR/CORRA basis, both legs
# anchored at offset=0) -- exactly the "at least one of each" shape
# required, expressed as the actual on-disk JSON schema.
_MIXED_STRATEGY_SET_JSON = json.dumps(
    {
        "schema_version": 1,
        "name": "Mixed End To End Set",
        "description": "",
        "entries": [
            {
                "name": "SOFR Outright",
                "enabled": True,
                "market_key": "SOFR",
                "offsets": [0],
                "weights": [1.0],
                "interval": "DAILY",
                "price_field": "Close",
                "expansion": {"max_curve_position": None, "eligible_rics": None},
            },
            {
                "name": "SOFR vs CORRA basis",
                "enabled": True,
                "legs": [
                    {"market_key": "SOFR", "offset": 0, "weight": 1.0},
                    {"market_key": "CORRA", "offset": 0, "weight": -1.0},
                ],
                "interval": "DAILY",
                "price_field": "Close",
                "bp_per_point": 100.0,
                "expansion": {"max_curve_position": None, "eligible_rics": None},
            },
        ],
    }
)


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


def _lseg_side_effect(ric, interval, start, end):
    # SOFR (no QuantHub mapping) resolves via LSEG directly. CORRA
    # (QuantHub-mapped) has its LSEG attempt fail -- a deterministic
    # stand-in for "LSEG could not provide complete history", the same
    # convention used throughout tests/test_service_provider_fallback.py --
    # so it falls back to QuantHub exactly as the real entitlement gap does.
    if ric in (_SOFR_H26, _SOFR_M26):
        return _make_df(_PRICE_DATES, close=96.80 if ric == _SOFR_H26 else 96.90)
    if ric in (_CORRA_H6, _CORRA_M6):
        raise MarketDataUnavailableError(ric, "User does not have permission for this universe")
    raise AssertionError(f"unexpected ric requested from LSEG: {ric}")


def test_strategy_set_json_to_scan_candidate_result_end_to_end(mocker, db_session):
    # --- StrategySet JSON -> deserialize -> StrategySet ---
    strategy_set = strategy_set_from_json(_MIXED_STRATEGY_SET_JSON)
    assert len(strategy_set.entries) == 1
    assert len(strategy_set.intermarket_entries) == 1

    # --- expand_strategy_set() -> instances generated (both types) ---
    instances = expand_strategy_set(strategy_set, _CONTRACT_START, _CONTRACT_END)
    single_market = [i for i in instances if isinstance(i, StrategyInstance)]
    intermarket = [i for i in instances if isinstance(i, IntermarketStrategyInstance)]
    assert {i.rics for i in single_market} == {(_SOFR_H26,), (_SOFR_M26,)}
    assert {i.rics for i in intermarket} == {(_SOFR_H26, _CORRA_H6), (_SOFR_M26, _CORRA_M6)}

    # --- pricing/cache: real per-RIC provider routing, tmp_path SQLite ---
    mock_lseg = mocker.patch("database.service.download_history", side_effect=_lseg_side_effect)
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _make_df(_PRICE_DATES, close=1.10) for instr in instruments
        },
    )

    # --- scanner -> range analytics -> ScanCandidateResult ---
    report = run_scan_on_instances(instances, _PRICE_START, _PRICE_END, lookbacks=(5,))

    assert report.skipped == ()
    assert len(report.results) == len(instances) == 4

    # SOFR's own two RICs were fetched via LSEG exactly once each,
    # regardless of appearing in both a single-market AND an
    # intermarket instance -- prewarm_leg_cache() dedupes across the
    # WHOLE combined list, single-market and intermarket instances alike.
    lseg_rics_called = [c.args[0] for c in mock_lseg.call_args_list]
    assert lseg_rics_called.count(_SOFR_H26) == 1
    assert lseg_rics_called.count(_SOFR_M26) == 1
    assert set(lseg_rics_called) == {_SOFR_H26, _SOFR_M26, _CORRA_H6, _CORRA_M6}

    # CORRA fell back to QuantHub, batched into ONE call carrying both
    # CORRA instruments (never a per-strategy/composite-label decision).
    assert mock_qh.call_count == 1

    # --- Result correctness, by instance type ---
    single_market_results = {r.rics: r for r in report.results if r.market_key == "SOFR"}
    intermarket_results = {r.rics: r for r in report.results if r.market_key == "SOFR/CORRA"}
    assert set(single_market_results) == {(_SOFR_H26,), (_SOFR_M26,)}
    assert set(intermarket_results) == {(_SOFR_H26, _CORRA_H6), (_SOFR_M26, _CORRA_M6)}

    basis_result = intermarket_results[(_SOFR_H26, _CORRA_H6)]
    assert basis_result.weights == (1.0, -1.0)
    assert basis_result.offsets == (0, 0)
    headline = basis_result.multi_lookback.per_lookback[0]
    # SOFR leg (96.80) minus CORRA leg (1.10), per the definition's weights.
    assert headline.current_price == pytest.approx(96.80 - 1.10)
    assert headline.bp_per_point == pytest.approx(100.0)  # the entry's explicit override, used

    # --- Cache/provenance independence, at the persistence layer ---
    assert len(cache.get_sync_ranges(db_session, _SOFR_H26, "DAILY")) == 1
    assert len(cache.get_sync_ranges(db_session, _CORRA_H6, "DAILY")) == 1
    assert cache.get_established_provider(db_session, _SOFR_H26, "DAILY") is None
    assert cache.get_established_provider(db_session, _CORRA_H6, "DAILY") == "QUANTHUB"


def test_mixed_strategy_set_expansion_and_scan_both_produce_both_types(mocker):
    """Requirement check, isolated from the fuller assertions above: a
    genuinely mixed StrategySet's expansion is one combined collection,
    and the scanner processes it as one combined ScanReport -- never two
    separate scans the caller has to run and merge itself."""
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    strategy_set = strategy_set_from_json(_MIXED_STRATEGY_SET_JSON)
    instances = expand_strategy_set(strategy_set, _CONTRACT_START, _CONTRACT_END)
    assert any(isinstance(i, StrategyInstance) for i in instances)
    assert any(isinstance(i, IntermarketStrategyInstance) for i in instances)

    report = run_scan_on_instances(instances, _PRICE_START, _PRICE_END, lookbacks=(5,))

    assert len(report.results) == len(instances)
    market_keys = {r.market_key for r in report.results}
    assert market_keys == {"SOFR", "SOFR/CORRA"}


def _batch_leg_df(rics, interval, start, end):
    return {ric: _make_df(_PRICE_DATES, close=100.0) for ric in rics}
