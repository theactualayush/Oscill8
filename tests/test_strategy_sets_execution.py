"""
tests/test_strategy_sets_execution.py

strategy_sets.execution: the Strategy Set Scan execution path --
with_interval_override() (pure, no market data) and run_strategy_set()
(composes expand_strategy_set() + run_scan_on_instances(), both
completely unmodified by this module).

strategy_engine.pricing.get_history is mocked with a deterministic fake
provider -- the exact seam tests/test_strategy_sets_multimarket_
pipeline.py and tests/test_template_scanner_scanner.py already mock --
so run_scan_on_instances()/analyze_histories() run for real; only the
LSEG-facing data is faked. Contract lists for the [_START, _END] window
are the same real, verified lists tests/test_strategy_sets_
multimarket_pipeline.py already cites against core.futures_calendar.
generate_contracts.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition

from strategy_sets.execution import run_strategy_set, with_interval_override
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository

from template_scanner.scanner import ScanReport, ScanRequest

_START, _END = "2026-01-01", "2027-12-31"
_PRICE_START, _PRICE_END = "2026-06-01", "2026-06-10"

_SOFR_CONTRACTS = ("SRAH26", "SRAM26", "SRAU26", "SRAZ26", "SRAH27", "SRAM27", "SRAU27", "SRAZ27")
_CORRA_CONTRACTS = ("CRAH6", "CRAM6", "CRAU6", "CRAZ6", "CRAH7", "CRAM7", "CRAU7", "CRAZ7")


def _outright(market_key: str, interval: BarInterval, weight: float = 1.0) -> StrategyDefinition:
    return StrategyDefinition(market_key=market_key, offsets=(0,), weights=(weight,), interval=interval)


def _fly(market_key: str, interval: BarInterval, weights: tuple[float, ...]) -> StrategyDefinition:
    return StrategyDefinition(market_key=market_key, offsets=(0, 1, 2), weights=weights, interval=interval)


# ---------------------------------------------------------------------
# with_interval_override -- pure, no market data
# ---------------------------------------------------------------------

def test_override_replaces_interval_on_every_entry():
    entries = (
        StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY)),
        StrategySetEntry(name="B", definition=_outright("SONIA", BarInterval.HOURLY)),
    )
    strategy_set = StrategySet(name="Mixed", entries=entries)

    overridden = with_interval_override(strategy_set, BarInterval.FOUR_HOUR)

    assert all(e.definition.interval == BarInterval.FOUR_HOUR for e in overridden.entries)


def test_override_preserves_everything_else():
    entry = StrategySetEntry(
        name="Fly", definition=_fly("SOFR", BarInterval.DAILY, (1.0, -2.0, 1.0)), enabled=False,
    )
    strategy_set = StrategySet(name="Set", entries=(entry,), description="desc")

    overridden = with_interval_override(strategy_set, BarInterval.HOURLY)
    result_entry = overridden.entries[0]

    assert overridden.name == "Set"
    assert overridden.description == "desc"
    assert result_entry.name == "Fly"
    assert result_entry.enabled is False
    assert result_entry.definition.market_key == "SOFR"
    assert result_entry.definition.offsets == (0, 1, 2)
    assert result_entry.definition.weights == (1.0, -2.0, 1.0)
    assert result_entry.definition.price_field == "Close"


def test_override_does_not_mutate_the_original_strategy_set():
    entry = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY))
    strategy_set = StrategySet(name="Original", entries=(entry,))

    with_interval_override(strategy_set, BarInterval.HOURLY)

    assert strategy_set.entries[0].definition.interval == BarInterval.DAILY


def test_override_result_is_always_a_distinct_object():
    entry = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY))
    strategy_set = StrategySet(name="Original", entries=(entry,))

    # Same interval value as the original -- proves this always builds a
    # fresh transient copy, not a "no-op passthrough" shortcut that
    # might tempt a future caller into mutating the original by accident.
    overridden = with_interval_override(strategy_set, BarInterval.DAILY)

    assert overridden is not strategy_set
    assert overridden.entries[0] is not strategy_set.entries[0]
    assert overridden.entries[0].definition is not strategy_set.entries[0].definition


def test_override_rejects_an_invalid_interval_like_any_strategydefinition_would():
    entry = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY))
    strategy_set = StrategySet(name="Set", entries=(entry,))

    with pytest.raises(ValueError):
        with_interval_override(strategy_set, "NOT_A_REAL_INTERVAL")


def test_persisted_strategy_set_is_unchanged_after_being_run(tmp_path):
    repo = StrategySetRepository(base_dir=str(tmp_path))
    entry = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY))
    repo.save(StrategySet(name="Persisted", entries=(entry,)))

    loaded = repo.load("Persisted")
    overridden = with_interval_override(loaded, BarInterval.FOUR_HOUR)
    assert overridden.entries[0].definition.interval == BarInterval.FOUR_HOUR  # sanity: override worked

    reloaded = repo.load("Persisted")
    assert reloaded.entries[0].definition.interval == BarInterval.DAILY  # untouched on disk


# ---------------------------------------------------------------------
# Deduplication happens AFTER the override, not before -- see execution.
# py's module docstring for why overriding after dedup would risk
# double-counting.
# ---------------------------------------------------------------------

def test_expand_strategy_set_mixed_intervals_unaffected_by_this_module():
    # Confirms this module's existence changes nothing about
    # expand_strategy_set()'s own, unmodified mixed-interval behavior --
    # the grid/manually-built-set path that must never be touched.
    entries = (
        StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY)),
        StrategySetEntry(name="B", definition=_outright("SOFR", BarInterval.FOUR_HOUR)),
    )
    strategy_set = StrategySet(name="Mixed", entries=entries)

    instances = expand_strategy_set(strategy_set, _START, _END)
    intervals = {i.definition.interval for i in instances}
    assert intervals == {BarInterval.DAILY, BarInterval.FOUR_HOUR}


def test_dedupe_occurs_after_override_not_before():
    entry_daily = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY))
    entry_4h = StrategySetEntry(name="B", definition=_outright("SOFR", BarInterval.FOUR_HOUR))
    strategy_set = StrategySet(name="Set", entries=(entry_daily, entry_4h))

    # Before override: different intervals -> genuinely different
    # candidates, both kept (8 SOFR contracts x 2 entries = 16).
    before = expand_strategy_set(strategy_set, _START, _END)
    assert len(before) == 2 * len(_SOFR_CONTRACTS)
    assert {i.definition.interval for i in before} == {BarInterval.DAILY, BarInterval.FOUR_HOUR}

    # After overriding both entries to the SAME interval: now genuinely
    # identical candidates (same market, RICs, weights, interval,
    # price_field) -- dedupe_candidates(), called INSIDE
    # expand_strategy_set() on the override's output, must collapse
    # them to one set of 8, not keep 16.
    overridden = with_interval_override(strategy_set, BarInterval.DAILY)
    after = expand_strategy_set(overridden, _START, _END)

    assert len(after) == len(_SOFR_CONTRACTS)
    assert all(i.definition.interval == BarInterval.DAILY for i in after)


# ---------------------------------------------------------------------
# run_strategy_set -- end to end through the real, unmodified
# run_scan_on_instances()/analyze_histories(), with a fake data provider.
# ---------------------------------------------------------------------

_SERIES_LEVEL = {
    ("SRAH26", BarInterval.DAILY): 100.0,
    ("SRAM26", BarInterval.DAILY): 105.0,
    ("SRAU26", BarInterval.DAILY): 100.0,
    ("SRAZ26", BarInterval.DAILY): 103.0,
    ("SRAH27", BarInterval.DAILY): 104.0,
    ("SRAM27", BarInterval.DAILY): 105.0,
    ("SRAU27", BarInterval.DAILY): 101.0,
    ("SRAZ27", BarInterval.DAILY): 102.0,
    ("CRAH6", BarInterval.DAILY): 200.0,
    ("CRAM6", BarInterval.DAILY): 190.0,
    ("CRAU6", BarInterval.DAILY): 200.0,
    ("CRAZ6", BarInterval.DAILY): 203.0,
    ("CRAH7", BarInterval.DAILY): 204.0,
    ("CRAM7", BarInterval.DAILY): 205.0,
    ("CRAU7", BarInterval.DAILY): 201.0,
    ("CRAZ7", BarInterval.DAILY): 202.0,
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
    """Patches strategy_engine.pricing.get_history AND get_history_batch --
    run_scan_on_instances()/build_history() run completely for real; only
    the LSEG/QuantHub-facing fetch is faked. run_scan_on_instances() now
    pre-warms its leg_cache via prewarm_leg_cache() -> get_history_batch()
    BEFORE the per-instance loop (see the QuantHub-batching phase), so
    get_history_batch must be mocked too, or the real (unmocked) function
    would try to reach a real database/provider. Both mocks share the
    same call-recording _get_history so fake_provider.calls stays a
    complete, chronologically accurate record of every (ric, interval,
    start, end) fetched, regardless of which of the two entry points
    actually resolved it."""
    calls: list[tuple] = []

    def _get_history(ric, interval, start, end):
        calls.append((ric, interval, str(start), str(end)))
        return _fake_series(ric, interval)

    def _get_history_batch(rics, interval, start, end):
        return {ric: _get_history(ric, interval, start, end) for ric in rics}

    mock = mocker.patch("strategy_engine.pricing.get_history", side_effect=_get_history)
    mock_batch = mocker.patch(
        "strategy_engine.pricing.get_history_batch", side_effect=_get_history_batch
    )
    mock.calls = calls
    mock_batch.calls = calls
    return mock


def test_run_strategy_set_applies_the_override_before_fetching_data(fake_provider):
    # Entry's ORIGINAL interval is HOURLY; _SERIES_LEVEL has no HOURLY
    # entries at all. If the override didn't take effect before pricing,
    # the fake provider would KeyError -- so a clean pass here is direct
    # proof the fetch actually used the overridden DAILY interval.
    entry = StrategySetEntry(name="Outright", definition=_outright("SOFR", BarInterval.HOURLY))
    strategy_set = StrategySet(name="Set", entries=(entry,))

    request, report = run_strategy_set(
        strategy_set, BarInterval.DAILY, _START, _END, _PRICE_START, _PRICE_END,
    )

    assert report.results
    assert all(r.interval == BarInterval.DAILY for r in report.results)
    assert all(interval == BarInterval.DAILY for (_, interval, _, _) in fake_provider.calls)


def test_run_strategy_set_supports_multiple_markets(fake_provider):
    sofr_entry = StrategySetEntry(name="SOFR Outright", definition=_outright("SOFR", BarInterval.DAILY))
    corra_entry = StrategySetEntry(name="CORRA Outright", definition=_outright("CORRA", BarInterval.HOURLY))
    strategy_set = StrategySet(name="Multi-Market", entries=(sofr_entry, corra_entry))

    request, report = run_strategy_set(
        strategy_set, BarInterval.DAILY, _START, _END, _PRICE_START, _PRICE_END,
    )

    market_keys = {r.market_key for r in report.results}
    assert market_keys == {"SOFR", "CORRA"}
    assert len(report.results) == len(_SOFR_CONTRACTS) + len(_CORRA_CONTRACTS)
    assert report.skipped == ()


def test_run_strategy_set_does_not_modify_the_input_strategy_set(fake_provider):
    entry = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.HOURLY))
    strategy_set = StrategySet(name="Set", entries=(entry,))

    run_strategy_set(strategy_set, BarInterval.DAILY, _START, _END, _PRICE_START, _PRICE_END)

    assert strategy_set.entries[0].definition.interval == BarInterval.HOURLY


def test_run_strategy_set_returns_a_scanrequest_the_chart_ui_can_use(fake_provider):
    entry = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY))
    strategy_set = StrategySet(name="Set", entries=(entry,))

    request, report = run_strategy_set(
        strategy_set, BarInterval.DAILY, _START, _END, _PRICE_START, _PRICE_END, lookbacks=(20, 40),
    )

    # These are the exact fields ui/chart_view.py's render_chart()/
    # get_selected_history() read off a stored ScanRequest.
    assert isinstance(request, ScanRequest)
    assert str(request.price_start) == _PRICE_START
    assert str(request.price_end) == _PRICE_END
    assert request.lookbacks == (20, 40)


def test_run_strategy_set_report_is_a_real_scanreport_of_scancandidateresults(fake_provider):
    entry = StrategySetEntry(name="A", definition=_outright("SOFR", BarInterval.DAILY))
    strategy_set = StrategySet(name="Set", entries=(entry,))

    _, report = run_strategy_set(strategy_set, BarInterval.DAILY, _START, _END, _PRICE_START, _PRICE_END)

    assert isinstance(report, ScanReport)
    for result in report.results:
        assert result.market_key == "SOFR"
        assert result.interval == BarInterval.DAILY
        assert result.multi_lookback is not None  # Module 4A/4B analytics actually ran
