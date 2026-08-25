"""
tests/test_intermarket_strategy_set_provider_routing.py

Verifies, end-to-end, exactly what the trader's market-expansion task
asked to confirm: a 7-market intermarket StrategySet (SOFR + EURIBOR +
SARON + YBA + SONIA + CORRA + ESTR_ICE) generates the expected
contracts, and each market's data requests route to the correct
provider within the SAME scan -- LSEG for SOFR, QuantHub for the other
six -- with zero cross-provider leakage:
    - a SOFR RIC is never sent to QuantHub;
    - an EURIBOR/SARON/YBA/SONIA/CORRA/ESTR_ICE RIC is never sent to
      LSEG for the QuantHub historical-data path;
    - FEIM6 (an LSEG-style RIC) is never used as a QuantHub instrument;
    - ERH26 (a QuantHub instrument) is never treated as an LSEG RIC.

All seven markets now have core.config.MARKETS entries (trader-
confirmed RIC root/year-digits/bp_per_point -- see each
MarketDefinition's own description for what is and isn't
live-LSEG-verified), so this file supersedes the earlier version of
itself which proved EURIBOR/SARON/YBA failed at construction time
before those entries existed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import BarInterval
from core.downloader import MarketDataUnavailableError
from core.ric import parse_ric
from core.providers import Provider, qh_root_for_market, resolve_provider
from core.quanthub import build_instrument
from database import service
from database.connection import get_session as _real_get_session
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import StrategySet, StrategySetEntry
from template_scanner.scanner import run_scan_on_instances

ALL_SEVEN_MARKETS = ["SOFR", "EURIBOR", "SARON", "YBA", "SONIA", "CORRA", "ESTR_ICE"]

EXPECTED_PROVIDER = {
    "SOFR": Provider.LSEG,
    "EURIBOR": Provider.QUANTHUB,
    "SARON": Provider.QUANTHUB,
    "YBA": Provider.QUANTHUB,
    "SONIA": Provider.QUANTHUB,
    "CORRA": Provider.QUANTHUB,
    "ESTR_ICE": Provider.QUANTHUB,
}


@pytest.fixture(autouse=True)
def _route_service_sessions_to_test_engine(monkeypatch, db_engine):
    """Same convention as tests/test_service.py: point database.service
    at the isolated tmp_path test engine, never the real data/oscill8.db
    -- this test writes real (mocked-provider) price bars via
    run_scan_on_instances and must not touch a shared/real database."""
    monkeypatch.setattr(service, "get_session", lambda: _real_get_session(db_engine))
    yield


def _outright(market_key: str) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0,), weights=(1.0,), interval=BarInterval.DAILY
    )


def _seven_market_set() -> StrategySet:
    return StrategySet(
        name="Full Intermarket Provider Routing Check",
        entries=tuple(
            StrategySetEntry(name=f"{mk} outright", definition=_outright(mk))
            for mk in ALL_SEVEN_MARKETS
        ),
    )


def test_seven_market_set_constructs_without_error():
    # The originally-requested 7-market set (SOFR + EURIBOR + SARON +
    # YBA + SONIA + CORRA + ESTR_ICE) now builds cleanly -- no KeyError,
    # unlike before these four MarketDefinitions existed.
    strategy_set = _seven_market_set()
    assert {e.definition.market_key for e in strategy_set.entries} == set(ALL_SEVEN_MARKETS)


def test_expand_generates_instances_for_all_seven_markets():
    instances = expand_strategy_set(_seven_market_set(), "2026-01-01", "2026-12-31")

    market_keys = {inst.definition.market_key for inst in instances}
    assert market_keys == set(ALL_SEVEN_MARKETS)
    assert len(instances) > 0


def test_each_generated_ric_routes_to_the_correct_provider():
    instances = expand_strategy_set(_seven_market_set(), "2026-01-01", "2026-12-31")

    for inst in instances:
        for ric in inst.rics:
            parsed = parse_ric(ric)
            assert parsed.market_key == inst.definition.market_key
            assert resolve_provider(parsed.market_key) == EXPECTED_PROVIDER[inst.definition.market_key]


def test_lseg_style_ric_never_used_as_quanthub_instrument():
    # Direct reproduction of the trader's named example, both for the
    # SAME contract (March/"H" 2026) so the comparison is meaningful:
    # the LSEG RIC (build_ric, root "FEI") and the QuantHub instrument
    # (build_instrument, root "ER" -- the verified ERH26 example) must
    # never be confused for one another.
    from core.ric import build_ric

    lseg_ric = build_ric("EURIBOR", 3, 2026)
    qh_instrument = build_instrument(qh_root_for_market("EURIBOR"), 3, 2026)
    assert lseg_ric == "FEIH6"
    assert qh_instrument == "ERH26"
    assert qh_instrument != lseg_ric
    assert "FEI" not in qh_instrument


@pytest.mark.parametrize(
    "market_key, lseg_root, qh_root",
    [
        ("EURIBOR", "FEI", "ER"),
        ("SARON", "SARO3", "FSR"),
        ("YBA", "YBA", "YBA"),
        ("SONIA", "SON", "SON"),
        ("CORRA", "CRA", "CRA"),
        ("ESTR_ICE", "EON3", "FER"),
    ],
)
def test_lseg_root_and_quanthub_root_resolved_independently(market_key, lseg_root, qh_root):
    from core.config import get_market

    assert get_market(market_key).ric_root == lseg_root
    assert qh_root_for_market(market_key) == qh_root


def test_mixed_provider_scan_routes_each_market_independently_in_one_run(mocker):
    """The critical mixed-market scenario: LSEG (SOFR) and QuantHub (the
    other six) candidates in the SAME scan, neither provider's mock ever
    seeing the other's RIC/instrument, and every market contributes
    results to the same ScanReport.

    run_scan_on_instances() now pre-warms its leg_cache via
    strategy_engine.pricing.prewarm_leg_cache() -> database.
    get_history_batch() BEFORE the per-instance pricing loop (see the
    QuantHub-batching phase). LSEG-routed RICs are still fetched one at
    a time through the completely unmodified database.service.
    download_history (via get_history()); the six QuantHub-mapped
    markets now go through the cache -> LSEG -> QuantHub fallback (see
    database.service's module docstring) instead of skipping LSEG
    entirely -- the LSEG mock below raises MarketDataUnavailableError
    for anything that isn't a SOFR-style RIC (a deterministic stand-in
    for a real LSEG gap, e.g. CORRA's documented entitlement error) so
    those six still resolve via QuantHub exactly as before this design
    existed, via database.service.download_history_quanthub_batch
    (core.quanthub.download_history_batch) -- ONE call carrying every
    QuantHub instrument needing a fetch for this scan's single DAILY
    interval group, not one call per instrument.
    """
    instances = expand_strategy_set(_seven_market_set(), "2026-01-01", "2026-03-31")
    assert {i.definition.market_key for i in instances} == set(ALL_SEVEN_MARKETS)

    def _bars(dates):
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(dates),
                "Open": [100.0] * len(dates),
                "High": [100.5] * len(dates),
                "Low": [99.5] * len(dates),
                "Close": [100.2] * len(dates),
                "Volume": [10.0] * len(dates),
            }
        )

    def _lseg(ric, interval, start, end):
        if ric.startswith("SRA"):
            return _bars(["2026-01-05", "2026-01-06"])
        raise MarketDataUnavailableError(ric, "The universe is not found")

    mock_lseg = mocker.patch("database.service.download_history", side_effect=_lseg)
    mock_qh = mocker.patch(
        "database.service.download_history_quanthub_batch",
        side_effect=lambda instruments, interval, start, end: {
            instr: _bars(["2026-01-05", "2026-01-06"]) for instr in instruments
        },
    )

    report = run_scan_on_instances(instances, "2026-01-01", "2026-01-31")

    assert len(report.skipped) == 0
    assert len(report.results) == len(instances)
    assert {r.market_key for r in report.results} == set(ALL_SEVEN_MARKETS)

    quanthub_instance_count = sum(1 for i in instances if i.definition.market_key != "SOFR")

    # Every candidate's LSEG attempt is tried, including the six
    # QuantHub-mapped markets (cache -> LSEG -> QuantHub fallback) --
    # only the ones the mock rejects (non-SOFR RICs) go on to QuantHub.
    assert mock_lseg.call_count == len(instances)
    # All six QuantHub markets share one DAILY interval group, so
    # prewarm_leg_cache() -> get_history_batch() issues exactly ONE
    # download_history_quanthub_batch() call carrying every distinct
    # QuantHub instrument needing a fetch (chunking into
    # QUANTHUB_BATCH_SIZE-sized HTTP requests happens inside the real,
    # unmocked core.quanthub.download_history_batch -- out of scope for
    # this mock boundary, covered separately in tests/test_quanthub.py).
    assert mock_qh.call_count == 1
    quanthub_instruments_seen = set(mock_qh.call_args_list[0].args[0])
    assert len(quanthub_instruments_seen) == quanthub_instance_count

    # Every call the LSEG mock received was a genuine LSEG-style RIC
    # string (never a QuantHub instrument identifier) -- confirms LSEG
    # is tried for every candidate's own RIC, not some QH-derived value.
    lseg_instruments_seen = {call.args[0] for call in mock_lseg.call_args_list}
    all_instance_rics = {ric for i in instances for ric in i.rics}
    assert lseg_instruments_seen == all_instance_rics
    assert any(instr.startswith("SRA") for instr in lseg_instruments_seen)  # SOFR was tried too

    quanthub_roots_expected = {qh_root_for_market(mk) for mk in ALL_SEVEN_MARKETS if mk != "SOFR"}
    assert all(
        any(instr.startswith(root) for root in quanthub_roots_expected)
        for instr in quanthub_instruments_seen
    )
    # LSEG-only roots that differ from their own market's QH root
    # (EURIBOR's "FEI", SARON's "SARO3", ESTR_ICE's "EON3" -- unlike
    # SONIA/CORRA, whose QH root happens to equal their LSEG root)
    # must never appear as a QuantHub call's instrument.
    lseg_only_roots = {"FEI", "SARO3", "EON3"}
    assert not any(
        instr.startswith(root) for instr in quanthub_instruments_seen for root in lseg_only_roots
    )
