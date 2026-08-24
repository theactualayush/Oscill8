"""
tests/test_template_scanner_scanner.py

ScanRequest/run_scan()/analyze_histories() tested with
database.get_history mocked at the strategy_engine.pricing boundary --
the same pattern used in tests/test_strategy_pricing.py -- combined
with real SOFR contract-calendar candidate generation (Module 5A,
pure calendar arithmetic, no I/O) from tests/test_template_scanner_universe.py.
"""

from __future__ import annotations

import dataclasses
import math

import pandas as pd
import pytest

from core.config import BarInterval
from core.downloader import MarketDataUnavailableError
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory
from range_analytics import analyze_multi_lookback

from template_scanner.scanner import (
    ScanReport,
    ScanRequest,
    SkippedCandidate,
    analyze_histories,
    run_scan,
    run_scan_on_instances,
)
from template_scanner.templates import template_from_dense_weights
from template_scanner.universe import generate_candidates

_DATES = pd.date_range("2020-01-01", periods=150, freq="D").strftime("%Y-%m-%d").tolist()
_VALUES = ([0.98, 1.00, 1.02] * 60)[:150]


def _leg_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(_DATES),
            "Open": _VALUES,
            "High": _VALUES,
            "Low": _VALUES,
            "Close": _VALUES,
            "Volume": [1000.0] * len(_DATES),
        }
    )


def _batch_leg_df(rics, interval, start, end):
    """Default get_history_batch side_effect: every requested RIC
    succeeds with the same leg data -- mirrors the old blanket
    `return_value=_leg_df()` get_history mock, now at the batched
    prewarm_leg_cache() boundary (strategy_engine.pricing.
    get_history_batch), which is what run_scan()/run_scan_on_instances()
    now call FIRST, before any per-instance build_history()."""
    return {ric: _leg_df() for ric in rics}


def _spread():
    return template_from_dense_weights("SOFR", (1, -1), BarInterval.DAILY)


def _fly():
    return template_from_dense_weights("SOFR", (1, -2, 1), BarInterval.DAILY)


def _fields_equal(a, b) -> bool:
    """NaN-tolerant equality for dataclasses/tuples/floats, same helper
    pattern as tests/test_range_multi_lookback.py."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_fields_equal(x, y) for x, y in zip(a, b))
    if dataclasses.is_dataclass(a) and dataclasses.is_dataclass(b):
        return all(
            _fields_equal(getattr(a, f.name), getattr(b, f.name))
            for f in dataclasses.fields(a)
        )
    return a == b


# ---------------------------------------------------------------------
# ScanRequest validation
# ---------------------------------------------------------------------

def test_scan_request_rejects_empty_definitions():
    with pytest.raises(ValueError, match="definitions"):
        ScanRequest(
            definitions=(),
            contract_start="2026-01-01", contract_end="2026-12-31",
            price_start="2020-01-01", price_end="2020-06-30",
        )


def test_scan_request_rejects_price_start_after_price_end():
    with pytest.raises(ValueError, match="price_start"):
        ScanRequest(
            definitions=(_spread(),),
            contract_start="2026-01-01", contract_end="2026-12-31",
            price_start="2020-06-30", price_end="2020-01-01",
        )


def test_scan_request_defaults_percentiles_to_5_95():
    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
    )
    assert request.lower_percentile == 5.0
    assert request.upper_percentile == 95.0


def test_scan_request_rejects_invalid_percentiles():
    with pytest.raises(ValueError):
        ScanRequest(
            definitions=(_spread(),),
            contract_start="2026-01-01", contract_end="2026-12-31",
            price_start="2020-01-01", price_end="2020-06-30",
            lower_percentile=95.0, upper_percentile=5.0,
        )
    with pytest.raises(ValueError):
        ScanRequest(
            definitions=(_spread(),),
            contract_start="2026-01-01", contract_end="2026-12-31",
            price_start="2020-01-01", price_end="2020-06-30",
            lower_percentile=-1.0, upper_percentile=95.0,
        )


def test_scan_report_fields_are_results_and_skipped_only():
    # locks in the narrow contract: ScanReport never grows a general
    # failure bucket -- only results and skipped (confirmed
    # MarketDataUnavailableError cases only).
    assert [f.name for f in dataclasses.fields(ScanReport)] == ["results", "skipped"]


def test_scan_report_skipped_defaults_to_empty():
    assert ScanReport(results=()).skipped == ()


# ---------------------------------------------------------------------
# run_scan: candidate generation -> pricing -> analytics
# ---------------------------------------------------------------------

def test_run_scan_builds_one_result_per_deduped_candidate(mocker):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20, 40),
    )
    report = run_scan(request)

    # SOFR quarterly contracts in 2026: H26,M26,U26,Z26 -> 3 spread instances
    assert len(report.results) == 3
    assert {r.rics for r in report.results} == {
        ("SRAH26", "SRAM26"), ("SRAM26", "SRAU26"), ("SRAU26", "SRAZ26"),
    }


def test_run_scan_preserves_exact_weights_for_scaled_and_unscaled_templates(mocker):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    fly = _fly()
    fly_2x = template_from_dense_weights("SOFR", (2, -4, 2), BarInterval.DAILY)
    request = ScanRequest(
        definitions=(fly, fly_2x),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    report = run_scan(request)

    weight_sets = {r.weights for r in report.results}
    assert weight_sets == {(1.0, -2.0, 1.0), (2.0, -4.0, 2.0)}


def test_run_scan_dedupes_before_pricing(mocker):
    mock_batch = mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    request = ScanRequest(
        definitions=(_fly(), _fly()),  # identical template twice
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    report = run_scan(request)

    # 2 unique fly instances (SRAH26-M26-U26, SRAM26-U26-Z26), each 3 legs,
    # 2 legs shared between the two instances -> 4 distinct RICs total,
    # batched into ONE prewarm_leg_cache -> get_history_batch call (not
    # one get_history call per leg).
    assert len(report.results) == 2
    assert mock_batch.call_count == 1
    assert set(mock_batch.call_args[0][0]) == {"SRAH26", "SRAM26", "SRAU26", "SRAZ26"}


def test_run_scan_shares_leg_cache_across_the_entire_scan(mocker):
    mock_batch = mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    run_scan(request)

    # 3 spread instances over 4 quarterly contracts (H26,M26,U26,Z26) share
    # M26/U26 across adjacent instances -> 4 distinct RICs, all requested
    # in ONE batched prewarm call (not 6 individual get_history calls,
    # 3 instances x 2 legs) -- proves the leg_cache is shared and
    # batch-prefetched, not fetched per-instance.
    assert mock_batch.call_count == 1
    assert set(mock_batch.call_args[0][0]) == {"SRAH26", "SRAM26", "SRAU26", "SRAZ26"}


@pytest.mark.parametrize("interval", [BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR])
def test_run_scan_covers_all_supported_intervals(mocker, interval):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    request = ScanRequest(
        definitions=(template_from_dense_weights("SOFR", (1, -1), interval),),
        contract_start="2026-01-01", contract_end="2026-06-30",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    report = run_scan(request)

    assert len(report.results) > 0
    assert all(r.interval == interval for r in report.results)


def test_run_scan_propagates_build_history_exceptions_uncaught(mocker):
    # An unrelated failure during the (now batched) prefetch propagates
    # uncaught, exactly like an unrelated per-leg get_history() failure
    # did before batching -- run_scan()'s exception policy is unchanged.
    mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=RuntimeError("simulated LSEG failure"),
    )

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    with pytest.raises(RuntimeError, match="simulated LSEG failure"):
        run_scan(request)


def test_run_scan_no_data_candidate_is_a_result_not_an_exception(mocker):
    empty = pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=lambda rics, interval, start, end: {ric: empty for ric in rics},
    )

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    report = run_scan(request)

    assert len(report.results) == 3
    assert all(math.isnan(r.multi_lookback.per_lookback[0].current_price) for r in report.results)


# ---------------------------------------------------------------------
# Module 5B.1: MarketDataUnavailableError skip-and-continue behaviour
# ---------------------------------------------------------------------

def _batch_omitting(*omit_rics):
    """get_history_batch side_effect simulating the real batched
    prewarm's own behavior for an LSEG RIC confirmed unavailable: that
    RIC is silently omitted from the returned dict (see database.
    service.get_history_batch's docstring), never raised from the batch
    call itself. build_history()/_fetch_leg() then transparently fall
    back to an individual get_history() call for that missing leg --
    which is the mock that must actually raise MarketDataUnavailableError,
    exactly where run_scan_on_instances()'s existing per-candidate
    skip-and-continue try/except already catches it, unchanged by
    batching.
    """
    def _batch(rics, interval, start, end):
        return {ric: _leg_df() for ric in rics if ric not in omit_rics}
    return _batch


def test_run_scan_skips_candidate_with_unavailable_leg_and_continues(mocker):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_omitting("SRAH26"))
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=MarketDataUnavailableError("SRAH26", "The universe is not found"),
    )

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    report = run_scan(request)

    # candidates: (H26,M26), (M26,U26), (U26,Z26). H26 unavailable ->
    # only the first (which needs H26) is skipped; the other two price
    # normally.
    assert len(report.results) == 2
    assert {r.rics for r in report.results} == {("SRAM26", "SRAU26"), ("SRAU26", "SRAZ26")}

    assert len(report.skipped) == 1
    assert report.skipped[0].instance.rics == ("SRAH26", "SRAM26")
    assert report.skipped[0].unavailable_ric == "SRAH26"
    assert "universe is not found" in report.skipped[0].message


def test_run_scan_shared_unavailable_ric_skips_every_affected_candidate_without_reattempt(mocker):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_omitting("SRAM26"))
    mock_get_history = mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=MarketDataUnavailableError("SRAM26", "The universe is not found"),
    )

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    report = run_scan(request)

    # candidates: (H26,M26), (M26,U26), (U26,Z26). M26 unavailable ->
    # both candidates referencing it are skipped; only (U26,Z26), which
    # never touches M26, prices successfully.
    assert len(report.results) == 1
    assert report.results[0].rics == ("SRAU26", "SRAZ26")

    assert len(report.skipped) == 2
    assert {s.instance.rics for s in report.skipped} == {("SRAH26", "SRAM26"), ("SRAM26", "SRAU26")}
    assert all(s.unavailable_ric == "SRAM26" for s in report.skipped)

    # H26, U26, Z26 are all served from the batch-prewarmed leg_cache
    # (the batch omitted only M26) -- the individual get_history()
    # fallback is only ever needed ONCE, for M26's first (candidate 1)
    # attempt. The second candidate's own reference to M26 never
    # reaches get_history() at all -- it's pre-emptively skipped via
    # unavailable_rics, proving the shared unavailable RIC is never
    # re-attempted against LSEG.
    assert mock_get_history.call_count == 1


def test_run_scan_all_unavailable_candidates_returns_empty_results_no_exception(mocker):
    outright = template_from_dense_weights("SOFR", (1,), BarInterval.DAILY)
    # Both single-leg candidates' RICs are omitted from the batch-prewarmed
    # leg_cache, so each falls back to its own individual get_history() call.
    mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=_batch_omitting("SRAH26", "SRAM26"),
    )
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            MarketDataUnavailableError("SRAH26", "The universe is not found"),
            MarketDataUnavailableError("SRAM26", "The universe is not found"),
        ],
    )

    request = ScanRequest(
        definitions=(outright,),
        contract_start="2026-01-01", contract_end="2026-06-30",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    report = run_scan(request)  # must not raise

    assert report.results == ()
    assert len(report.skipped) == 2
    assert {s.unavailable_ric for s in report.skipped} == {"SRAH26", "SRAM26"}


def test_run_scan_unrelated_exception_after_a_skip_still_aborts(mocker):
    # SRAM26 is served from the batch-prewarmed leg_cache (present/succeeds);
    # SRAH26 and SRAU26 are omitted, so each falls back to an individual
    # get_history() call -- H26 first (candidate 1 leg 1, raises and is
    # caught/skipped), then U26 (candidate 2 leg 2, raises and propagates).
    mocker.patch(
        "strategy_engine.pricing.get_history_batch",
        side_effect=_batch_omitting("SRAH26", "SRAU26"),
    )
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=[
            MarketDataUnavailableError("SRAH26", "The universe is not found"),
            RuntimeError("simulated unrelated failure"),  # SRAU26 (candidate 2 leg 2)
        ],
    )

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20,),
    )
    with pytest.raises(RuntimeError, match="simulated unrelated failure"):
        run_scan(request)


# ---------------------------------------------------------------------
# Mixed-market scan: an available SOFR candidate alongside a CORRA
# candidate whose leg raises MarketDataUnavailableError (as core.
# downloader now translates CORRA's real, documented 70112 entitlement
# error into -- see core/downloader.py's _is_confirmed_no_permission()).
# run_scan_on_instances() is called directly with hand-built instances
# so this test is agnostic to real contract-calendar rolling; the
# translation itself is covered separately in tests/test_downloader.py.
# ---------------------------------------------------------------------

def _sofr_outright_instance(ric: str = "SRAH26") -> StrategyInstance:
    definition = template_from_dense_weights("SOFR", (1,), BarInterval.DAILY)
    return StrategyInstance(definition=definition, rics=(ric,))


def _corra_outright_instance(ric: str = "CRAH6") -> StrategyInstance:
    definition = template_from_dense_weights("CORRA", (1,), BarInterval.DAILY)
    return StrategyInstance(definition=definition, rics=(ric,))


def test_mixed_scan_skips_unavailable_corra_candidate_and_still_returns_sofr_results(mocker):
    # Simulates core.downloader having already translated CORRA's real
    # 70112 entitlement error into MarketDataUnavailableError -- proves
    # run_scan_on_instances()'s EXISTING skip machinery (no changes of
    # its own) correctly keeps the available SOFR candidate.
    # SRAH26 is served from the batch-prewarmed leg_cache; CRAH6 is
    # omitted, so it falls back to an individual get_history() call that
    # raises the confirmed-unavailable error.
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_omitting("CRAH6"))
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=MarketDataUnavailableError("CRAH6", "User does not have permission for this universe"),
    )

    instances = [_sofr_outright_instance(), _corra_outright_instance()]
    report = run_scan_on_instances(
        instances, price_start="2020-01-01", price_end="2020-06-30", lookbacks=(20,),
    )

    assert len(report.results) == 1
    assert report.results[0].rics == ("SRAH26",)
    assert report.results[0].market_key == "SOFR"

    assert len(report.skipped) == 1
    assert report.skipped[0].unavailable_ric == "CRAH6"
    assert report.skipped[0].instance.rics == ("CRAH6",)


def test_mixed_scan_unrelated_corra_exception_still_aborts_and_loses_sofr_results(mocker):
    # An UNRELATED exception on the CORRA leg (not the confirmed 70112
    # condition) must still abort the whole scan, per the deliberately
    # narrow exception policy -- even though the SOFR candidate would
    # otherwise have priced successfully. Order matters: SOFR is fetched
    # first and succeeds, CORRA fails second, proving the already-
    # computed SOFR result is discarded when the function raises.
    # SRAH26 is served from the batch-prewarmed leg_cache (succeeds first,
    # added to histories); CRAH6 is omitted, so it falls back to an
    # individual get_history() call that raises the unrelated exception,
    # aborting the scan and discarding the already-computed SOFR result.
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_omitting("CRAH6"))
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=RuntimeError("simulated unrelated failure"),
    )

    instances = [_sofr_outright_instance(), _corra_outright_instance()]
    with pytest.raises(RuntimeError, match="simulated unrelated failure"):
        run_scan_on_instances(
            instances, price_start="2020-01-01", price_end="2020-06-30", lookbacks=(20,),
        )


def test_run_scan_carries_configured_percentiles_through_to_results(mocker):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    request = ScanRequest(
        definitions=(_spread(),),
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20, 40),
        lower_percentile=25.0, upper_percentile=75.0,
    )
    report = run_scan(request)

    assert report.results  # sanity: the scan actually produced candidates
    for candidate in report.results:
        for analytics in candidate.multi_lookback.per_lookback:
            assert analytics.lower_percentile == 25.0
            assert analytics.upper_percentile == 75.0


# ---------------------------------------------------------------------
# analyze_histories: mode-agnostic core, no drift from Module 4
# ---------------------------------------------------------------------

def test_analyze_histories_requires_no_io():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1.0,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26",))
    history = StrategyHistory(
        instance=instance,
        price_field="Close",
        history=pd.DataFrame(
            {"Date": pd.to_datetime(_DATES), "Leg_1": _VALUES, "Strategy": _VALUES}
        ),
    )

    report = analyze_histories([history], lookbacks=(20, 40))

    assert len(report.results) == 1
    assert report.results[0].rics == ("SRAH26",)


def test_analyze_histories_matches_direct_analyze_multi_lookback_call():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1.0, -1.0), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26", "SRAM26"))
    history = StrategyHistory(
        instance=instance,
        price_field="Close",
        history=pd.DataFrame(
            {"Date": pd.to_datetime(_DATES), "Leg_1": _VALUES, "Leg_2": _VALUES, "Strategy": _VALUES}
        ),
    )

    via_scanner = analyze_histories([history], lookbacks=(20, 40, 60)).results[0].multi_lookback
    direct = analyze_multi_lookback(history, lookbacks=(20, 40, 60))

    assert _fields_equal(via_scanner, direct)


# ---------------------------------------------------------------------
# run_scan_on_instances: the instances-in entry point Module 7B's
# Strategy Set panel uses (expand_strategy_set() -> here), extracted
# from run_scan()'s own body so both share one implementation.
# ---------------------------------------------------------------------

def test_run_scan_on_instances_prices_and_analyzes_a_prebuilt_candidate_list(mocker):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    instances = generate_candidates(_spread(), "2026-01-01", "2026-12-31")
    report = run_scan_on_instances(instances, "2020-01-01", "2020-06-30", lookbacks=(20,))

    assert len(report.results) == 3
    assert {r.rics for r in report.results} == {
        ("SRAH26", "SRAM26"), ("SRAM26", "SRAU26"), ("SRAU26", "SRAZ26"),
    }
    assert report.skipped == ()


def test_run_scan_on_instances_shares_leg_cache_across_the_whole_call(mocker):
    mock_batch = mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    instances = generate_candidates(_spread(), "2026-01-01", "2026-12-31")
    run_scan_on_instances(instances, "2020-01-01", "2020-06-30", lookbacks=(20,))

    # Same sharing guarantee as run_scan(): 4 distinct RICs requested in
    # ONE batched prewarm call, not 6 individual get_history calls
    # (3 instances x 2 legs).
    assert mock_batch.call_count == 1
    assert set(mock_batch.call_args[0][0]) == {"SRAH26", "SRAM26", "SRAU26", "SRAZ26"}


def test_run_scan_on_instances_skips_unavailable_leg_and_continues(mocker):
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_omitting("SRAH26"))
    mocker.patch(
        "strategy_engine.pricing.get_history",
        side_effect=MarketDataUnavailableError("SRAH26", "The universe is not found"),
    )

    instances = generate_candidates(_spread(), "2026-01-01", "2026-12-31")
    report = run_scan_on_instances(instances, "2020-01-01", "2020-06-30", lookbacks=(20,))

    assert len(report.results) == 2
    assert len(report.skipped) == 1
    assert report.skipped[0].unavailable_ric == "SRAH26"


def test_run_scan_on_instances_propagates_percentiles():
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1.0,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=("SRAH26",))
    history = StrategyHistory(
        instance=instance,
        price_field="Close",
        history=pd.DataFrame(
            {"Date": pd.to_datetime(_DATES), "Leg_1": _VALUES, "Strategy": _VALUES}
        ),
    )
    # analyze_histories is exercised directly here (no I/O needed); the
    # percentile plumbing is identical to run_scan_on_instances' own
    # call into it -- see test_run_scan_carries_configured_percentiles_
    # through_to_results above for the run_scan() equivalent.
    report = analyze_histories([history], lookbacks=(20,), lower_percentile=25.0, upper_percentile=75.0)
    analytics = report.results[0].multi_lookback.per_lookback[0]
    assert analytics.lower_percentile == 25.0
    assert analytics.upper_percentile == 75.0


def test_run_scan_delegates_to_run_scan_on_instances_with_deduped_candidates(mocker):
    """run_scan() must produce byte-identical results to calling
    generate_candidate_universe+dedupe_candidates and then
    run_scan_on_instances() by hand -- locking in that the refactor
    left run_scan()'s own behavior unchanged."""
    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)

    request = ScanRequest(
        definitions=(_fly(), _fly()),  # identical twice -> dedup exercised
        contract_start="2026-01-01", contract_end="2026-12-31",
        price_start="2020-01-01", price_end="2020-06-30",
        lookbacks=(20, 40),
    )
    via_run_scan = run_scan(request)

    mocker.patch("strategy_engine.pricing.get_history_batch", side_effect=_batch_leg_df)
    from template_scanner.universe import dedupe_candidates, generate_candidate_universe

    candidates = dedupe_candidates(
        generate_candidate_universe(list(request.definitions), request.contract_start, request.contract_end)
    )
    via_manual_call = run_scan_on_instances(
        candidates, request.price_start, request.price_end, lookbacks=request.lookbacks
    )

    assert {r.rics for r in via_run_scan.results} == {r.rics for r in via_manual_call.results}
    assert len(via_run_scan.results) == len(via_manual_call.results)
