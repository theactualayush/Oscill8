"""
tests/test_strategy_sets_multimarket_pipeline.py

Audit regression suite proving the existing Strategy Set architecture
carries multiple independent markets/intervals safely all the way
through the REAL, unmodified pipeline:

    StrategySet -> strategy_sets.expansion.expand_strategy_set()
    -> strategy_engine.combinations.StrategyInstance
    -> template_scanner.scanner.run_scan_on_instances()
    -> strategy_engine.pricing.build_history()
    -> strategy_engine.pricing.get_history() [the database.get_history
       boundary, mocked here with a deterministic fake provider --
       see the module docstring in tests/test_template_scanner_
       scanner.py for why this exact seam is mocked project-wide]

No production code is changed by this file. It exists to answer one
question: can a single StrategySet mix markets/intervals across its
entries without one entry's market, interval, contracts, RICs, or
fetched history ever leaking into another's?

Key structural finding this suite locks in: template_scanner.scanner.
ScanRequest has NO market/interval field at all -- every market/
interval concept lives entirely on each individual StrategyDefinition/
StrategyInstance. There is therefore no "global scan market" for a
Strategy Set entry to ever be overridden by at this layer; the tests
below exist to prove that structural fact holds in practice, not to
work around a design that lacks it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import BarInterval
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import StrategySet, StrategySetEntry
from template_scanner.metrics import at_lookback
from template_scanner.scanner import ScanRequest, run_scan_on_instances

_START, _END = "2026-01-01", "2027-12-31"
_PRICE_START, _PRICE_END = "2026-06-01", "2026-06-10"

# Full, real contract lists for this window -- verified directly
# against core.futures_calendar.generate_contracts (see
# tests/test_strategy_sets_expansion.py's own convention for citing
# this rather than re-deriving the rolling logic by hand).
_SOFR_CONTRACTS = ("SRAH26", "SRAM26", "SRAU26", "SRAZ26", "SRAH27", "SRAM27", "SRAU27", "SRAZ27")
_CORRA_CONTRACTS = ("CRAH6", "CRAM6", "CRAU6", "CRAZ6", "CRAH7", "CRAM7", "CRAU7", "CRAZ7")


def _outright(market_key: str, interval: BarInterval, weight: float = 1.0) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0,), weights=(weight,), interval=interval,
    )


def _fly(market_key: str, interval: BarInterval, weights: tuple[float, ...]) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=(0, 1, 2), weights=weights, interval=interval,
    )


# ---------------------------------------------------------------------
# A deterministic fake historical-data provider.
#
# Mocks strategy_engine.pricing.get_history -- the exact boundary
# tests/test_template_scanner_scanner.py and tests/test_strategy_
# pricing.py already mock project-wide (database.get_history imported
# into pricing.py's own module namespace) -- so build_history()/
# run_scan_on_instances() run completely unmodified and for real; only
# the LSEG-facing data ever gets faked.
#
# Keyed by (ric, interval) so a test can assert not just THAT a call
# happened, but exactly which (ric, interval) combination it was for --
# this is what lets these tests prove independence rather than merely
# "the scan didn't crash".
# ---------------------------------------------------------------------

_SERIES_LEVEL = {
    # SOFR's front three legs are deliberately a NON-arithmetic
    # sequence (100, 105, 100 -- not a straight line) so a fly's
    # weighted sum (1, -2, 1) is sensitive to which market's levels it
    # actually received: a linear sequence would fly-weight to zero
    # regardless of which market's data leaked in, silently hiding a
    # cross-market substitution bug.
    ("SRAH26", BarInterval.DAILY): 100.0,
    ("SRAM26", BarInterval.DAILY): 105.0,
    ("SRAU26", BarInterval.DAILY): 100.0,
    ("SRAZ26", BarInterval.DAILY): 103.0,
    ("SRAH27", BarInterval.DAILY): 104.0,
    ("SRAM27", BarInterval.DAILY): 105.0,
    # CORRA's own front three legs: also non-arithmetic, and chosen so
    # CORRA's fly result would differ from SOFR's fly result even if
    # the two markets' level sets were accidentally swapped.
    ("CRAH6", BarInterval.DAILY): 200.0,
    ("CRAM6", BarInterval.DAILY): 190.0,
    ("CRAU6", BarInterval.DAILY): 200.0,
    ("CRAZ6", BarInterval.DAILY): 203.0,
    ("CRAH7", BarInterval.DAILY): 204.0,
    ("CRAM7", BarInterval.DAILY): 205.0,
    # Same RIC as an existing SOFR DAILY entry above, but under a
    # DIFFERENT interval -- deliberately given a wildly different level
    # so any interval leakage (e.g. the DAILY series being reused for
    # the 4H request) is immediately obvious in the priced result.
    ("SRAH26", BarInterval.FOUR_HOUR): 900.0,
    ("SRAM26", BarInterval.FOUR_HOUR): 901.0,
}


def _fake_series(ric: str, interval: BarInterval) -> pd.DataFrame:
    level = _SERIES_LEVEL[(ric, interval)]
    dates = pd.date_range("2026-06-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": [level] * 5,
            "High": [level] * 5,
            "Low": [level] * 5,
            "Close": [level] * 5,
            "Volume": [1000.0] * 5,
        }
    )


@pytest.fixture
def fake_provider(mocker):
    """Patches strategy_engine.pricing.get_history with a callable that
    returns a distinct, identifiable series per (ric, interval) and
    records every call it received -- so a test can assert exactly
    which requests were made, in addition to the priced results."""
    calls: list[tuple] = []

    def _get_history(ric, interval, start, end):
        calls.append((ric, interval, str(start), str(end)))
        return _fake_series(ric, interval)

    mock = mocker.patch("strategy_engine.pricing.get_history", side_effect=_get_history)
    mock.calls = calls
    return mock


# ---------------------------------------------------------------------
# Task 3 + Task 4: expansion preserves each entry's own market/
# interval/weights/offsets, and produces RICs from that entry's own
# market ONLY -- never a mix.
# ---------------------------------------------------------------------

def test_multi_market_expansion_preserves_every_entry_and_produces_independent_rics():
    sofr_entry = StrategySetEntry(
        name="SOFR Fly", definition=_fly("SOFR", BarInterval.DAILY, (1.0, -2.0, 1.0)),
    )
    corra_entry = StrategySetEntry(
        name="CORRA Fly", definition=_fly("CORRA", BarInterval.DAILY, (-1.0, 2.0, -1.0)),
    )
    strategy_set = StrategySet(name="Multi-Market Fly Set", entries=(sofr_entry, corra_entry))

    instances = expand_strategy_set(strategy_set, _START, _END)
    assert instances, "expansion produced no instances at all"

    sofr_instances = [i for i in instances if i.definition.market_key == "SOFR"]
    corra_instances = [i for i in instances if i.definition.market_key == "CORRA"]

    # Every instance is accounted for by exactly one of the two markets.
    assert len(sofr_instances) + len(corra_instances) == len(instances)
    assert sofr_instances and corra_instances

    for inst in sofr_instances:
        assert inst.definition.interval == BarInterval.DAILY
        assert inst.definition.weights == (1.0, -2.0, 1.0)
        assert inst.definition.offsets == (0, 1, 2)
        assert all(ric in _SOFR_CONTRACTS for ric in inst.rics)
        assert not any(ric in _CORRA_CONTRACTS for ric in inst.rics)

    for inst in corra_instances:
        assert inst.definition.interval == BarInterval.DAILY
        assert inst.definition.weights == (-1.0, 2.0, -1.0)
        assert inst.definition.offsets == (0, 1, 2)
        assert all(ric in _CORRA_CONTRACTS for ric in inst.rics)
        assert not any(ric in _SOFR_CONTRACTS for ric in inst.rics)

    # No RIC string is ever shared between the two markets' instances --
    # the strongest possible statement of "no leakage" at the RIC level.
    sofr_rics = {r for inst in sofr_instances for r in inst.rics}
    corra_rics = {r for inst in corra_instances for r in inst.rics}
    assert sofr_rics.isdisjoint(corra_rics)


def test_sofr_outright_entry_only_ever_produces_sofr_rics():
    entry = StrategySetEntry(name="SOFR Outright", definition=_outright("SOFR", BarInterval.DAILY))
    strategy_set = StrategySet(name="SOFR Only", entries=(entry,))

    instances = expand_strategy_set(strategy_set, _START, _END)
    rics = {r for inst in instances for r in inst.rics}

    assert rics == set(_SOFR_CONTRACTS)
    assert not any(ric.startswith("CRA") for ric in rics)


def test_corra_outright_entry_only_ever_produces_corra_rics():
    entry = StrategySetEntry(name="CORRA Outright", definition=_outright("CORRA", BarInterval.DAILY))
    strategy_set = StrategySet(name="CORRA Only", entries=(entry,))

    instances = expand_strategy_set(strategy_set, _START, _END)
    rics = {r for inst in instances for r in inst.rics}

    assert rics == set(_CORRA_CONTRACTS)
    assert not any(ric.startswith("SRA") for ric in rics)


def test_combined_set_never_lets_corra_request_sofr_rics_or_vice_versa():
    """Direct test of Task 4's exact wording: there must be no
    possibility of the CORRA entry accidentally requesting SRA... RICs,
    or the SOFR entry accidentally requesting CRA... RICs."""
    sofr_entry = StrategySetEntry(name="SOFR Outright", definition=_outright("SOFR", BarInterval.DAILY))
    corra_entry = StrategySetEntry(name="CORRA Outright", definition=_outright("CORRA", BarInterval.DAILY))
    strategy_set = StrategySet(name="Combined", entries=(sofr_entry, corra_entry))

    instances = expand_strategy_set(strategy_set, _START, _END)

    for inst in instances:
        if inst.definition.market_key == "SOFR":
            assert all(ric.startswith("SRA") for ric in inst.rics)
        elif inst.definition.market_key == "CORRA":
            assert all(ric.startswith("CRA") for ric in inst.rics)
        else:
            pytest.fail(f"unexpected market_key on instance: {inst.definition.market_key}")


# ---------------------------------------------------------------------
# Task 5: interval independence -- the SAME market/RIC under two
# different intervals must be fetched, and priced, completely
# separately, with no cross-contamination.
# ---------------------------------------------------------------------

def test_same_market_different_interval_entries_are_priced_independently(fake_provider):
    daily_entry = StrategySetEntry(name="SOFR Daily", definition=_outright("SOFR", BarInterval.DAILY))
    four_hour_entry = StrategySetEntry(name="SOFR 4H", definition=_outright("SOFR", BarInterval.FOUR_HOUR))
    strategy_set = StrategySet(name="Interval Mix", entries=(daily_entry, four_hour_entry))

    instances = expand_strategy_set(strategy_set, _START, _END)
    # Restrict to the shared front contract so both entries reference
    # the literal same RIC string under different intervals -- the
    # scenario where interval leakage would be most likely to hide.
    front = [i for i in instances if i.rics == ("SRAH26",)]
    assert len(front) == 2
    assert {i.definition.interval for i in front} == {BarInterval.DAILY, BarInterval.FOUR_HOUR}

    report = run_scan_on_instances(front, _PRICE_START, _PRICE_END, lookbacks=(3,))
    assert len(report.results) == 2
    assert not report.skipped

    by_interval = {r.interval: r for r in report.results}
    daily_result = by_interval[BarInterval.DAILY]
    four_hour_result = by_interval[BarInterval.FOUR_HOUR]

    # Both requested the same RIC...
    assert daily_result.rics == ("SRAH26",)
    assert four_hour_result.rics == ("SRAH26",)
    # ...but each got its OWN interval's series, never the other's.
    assert at_lookback(daily_result.multi_lookback, 3).median == pytest.approx(100.0)
    assert at_lookback(four_hour_result.multi_lookback, 3).median == pytest.approx(900.0)

    # Exactly one call per (ric, interval) -- never merged, never reused
    # across intervals, never double-counted.
    assert {(c[0], c[1]) for c in fake_provider.calls} == {
        ("SRAH26", BarInterval.DAILY), ("SRAH26", BarInterval.FOUR_HOUR),
    }
    assert len(fake_provider.calls) == 2


def test_sofr_daily_sofr_4h_and_corra_daily_combined_never_cross_contaminate(fake_provider):
    """Task 5's suggested combination: SOFR DAILY + SOFR 4H + CORRA
    DAILY all in the same set. Proves market independence and interval
    independence hold simultaneously, not just pairwise."""
    entries = (
        StrategySetEntry(name="SOFR Daily", definition=_outright("SOFR", BarInterval.DAILY)),
        StrategySetEntry(name="SOFR 4H", definition=_outright("SOFR", BarInterval.FOUR_HOUR)),
        StrategySetEntry(name="CORRA Daily", definition=_outright("CORRA", BarInterval.DAILY)),
    )
    strategy_set = StrategySet(name="Triple Mix", entries=entries)

    instances = expand_strategy_set(strategy_set, _START, _END)
    front_instances = [
        i for i in instances
        if i.rics in (("SRAH26",), ("CRAH6",))
    ]
    assert len(front_instances) == 3  # SOFR/DAILY, SOFR/4H, CORRA/DAILY

    report = run_scan_on_instances(front_instances, _PRICE_START, _PRICE_END, lookbacks=(3,))
    assert len(report.results) == 3
    assert not report.skipped

    by_key = {(r.market_key, r.interval): r for r in report.results}
    assert at_lookback(by_key[("SOFR", BarInterval.DAILY)].multi_lookback, 3).median == pytest.approx(100.0)
    assert at_lookback(by_key[("SOFR", BarInterval.FOUR_HOUR)].multi_lookback, 3).median == pytest.approx(900.0)
    assert at_lookback(by_key[("CORRA", BarInterval.DAILY)].multi_lookback, 3).median == pytest.approx(200.0)

    requested = {(c[0], c[1]) for c in fake_provider.calls}
    assert requested == {
        ("CRAH6", BarInterval.DAILY),
        ("SRAH26", BarInterval.DAILY),
        ("SRAH26", BarInterval.FOUR_HOUR),
    }
    assert len(fake_provider.calls) == 3


# ---------------------------------------------------------------------
# Task 7: a mismatched global scan-level market/interval (standing in
# for ScanSetup's manual-scan controls) must never override a Strategy
# Set entry's own market/interval. ScanRequest/run_scan_on_instances
# have no market/interval field to leak from in the first place -- this
# test proves that holds even when a caller has a completely unrelated
# "current scan bar" market/interval selected at the same time.
# ---------------------------------------------------------------------

def test_mismatched_scan_setup_style_market_interval_never_overrides_entries(fake_provider):
    # Stands in for "the manual scan bar is currently showing FED_FUNDS
    # / 4H" -- deliberately matching NEITHER Strategy Set entry below.
    _MISMATCHED_SCAN_SETUP_MARKET = "FED_FUNDS"
    _MISMATCHED_SCAN_SETUP_INTERVAL = BarInterval.FOUR_HOUR

    sofr_entry = StrategySetEntry(name="SOFR Daily", definition=_outright("SOFR", BarInterval.DAILY))
    corra_entry = StrategySetEntry(name="CORRA Daily", definition=_outright("CORRA", BarInterval.DAILY))
    strategy_set = StrategySet(name="Mismatch Guard", entries=(sofr_entry, corra_entry))

    # expand_strategy_set has no market/interval parameter at all --
    # only a contract window -- so there is structurally no argument
    # position where a "scan setup" market/interval could even be
    # threaded through. Confirmed once, explicitly, here.
    import inspect
    params = inspect.signature(expand_strategy_set).parameters
    assert "market_key" not in params and "interval" not in params

    instances = expand_strategy_set(strategy_set, _START, _END)
    front_instances = [i for i in instances if i.rics in (("SRAH26",), ("CRAH6",))]
    assert len(front_instances) == 2

    # ScanRequest itself has no market/interval field either -- confirm
    # that structurally, not just by absence of a parameter somewhere.
    scan_request_fields = {f.name for f in __import__("dataclasses").fields(ScanRequest)}
    assert "market" not in scan_request_fields
    assert "market_key" not in scan_request_fields
    assert "interval" not in scan_request_fields

    report = run_scan_on_instances(front_instances, _PRICE_START, _PRICE_END, lookbacks=(3,))
    assert len(report.results) == 2

    result_markets = {r.market_key for r in report.results}
    result_intervals = {r.interval for r in report.results}
    assert result_markets == {"SOFR", "CORRA"}
    assert result_intervals == {BarInterval.DAILY}
    assert _MISMATCHED_SCAN_SETUP_MARKET not in result_markets
    assert _MISMATCHED_SCAN_SETUP_INTERVAL not in result_intervals

    # And the actual historical-data requests reflect each entry's own
    # market/interval, never the mismatched "scan setup" values.
    requested_rics = {c[0] for c in fake_provider.calls}
    requested_intervals = {c[1] for c in fake_provider.calls}
    assert requested_rics == {"SRAH26", "CRAH6"}
    assert requested_intervals == {BarInterval.DAILY}


# ---------------------------------------------------------------------
# Task 8: multi-market result integrity -- each StrategyInstance's
# result is computed from its own market's data, never mixed with
# another instance's.
# ---------------------------------------------------------------------

def test_scanner_processes_multiple_markets_without_mixing_histories(fake_provider):
    sofr_entry = StrategySetEntry(name="SOFR Fly", definition=_fly("SOFR", BarInterval.DAILY, (1.0, -2.0, 1.0)))
    corra_entry = StrategySetEntry(name="CORRA Fly", definition=_fly("CORRA", BarInterval.DAILY, (-1.0, 2.0, -1.0)))
    strategy_set = StrategySet(name="Integrity Check", entries=(sofr_entry, corra_entry))

    instances = expand_strategy_set(strategy_set, _START, _END)
    # One fly instance per market, restricted to legs with known fake levels.
    sofr_front = next(i for i in instances if i.rics == ("SRAH26", "SRAM26", "SRAU26"))
    corra_front = next(i for i in instances if i.rics == ("CRAH6", "CRAM6", "CRAU6"))

    report = run_scan_on_instances([sofr_front, corra_front], _PRICE_START, _PRICE_END, lookbacks=(3,))
    assert len(report.results) == 2
    by_market = {r.market_key: r for r in report.results}

    # SOFR fly: 1*100 - 2*105 + 1*100 = -10, computed from SOFR's OWN
    # (deliberately non-arithmetic) levels. If CORRA's levels had
    # leaked in with these same weights the result would instead be
    # 1*200 - 2*190 + 1*200 = +20 -- a different sign and magnitude, so
    # a leak here cannot hide behind a coincidental zero.
    assert at_lookback(by_market["SOFR"].multi_lookback, 3).median == pytest.approx(
        1 * 100.0 - 2 * 105.0 + 1 * 100.0
    )
    # CORRA fly: -1*200 + 2*190 - 1*200 = -20, computed from CORRA's OWN
    # levels -- if SOFR's levels had leaked in here with these weights
    # the result would instead be -1*100 + 2*105 - 1*100 = +10.
    assert at_lookback(by_market["CORRA"].multi_lookback, 3).median == pytest.approx(
        -1 * 200.0 + 2 * 190.0 - 1 * 200.0
    )

    # Every leg RIC requested for the SOFR result actually belongs to
    # SOFR's own contract list, and likewise for CORRA -- the strongest
    # possible check that no leg was substituted from the other market.
    assert set(by_market["SOFR"].rics) <= set(_SOFR_CONTRACTS)
    assert set(by_market["CORRA"].rics) <= set(_CORRA_CONTRACTS)
