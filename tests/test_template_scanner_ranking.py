"""
tests/test_template_scanner_ranking.py

SortKey/rank_results() tested against hand-built ScanCandidateResult
fixtures using real analyze_multi_lookback() output -- no I/O.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.config import BarInterval
from range_analytics import analyze_multi_lookback
from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory

from template_scanner.filters import at_lookback
from template_scanner.ranking import SortKey, rank_results
from template_scanner.scan_results import ScanCandidateResult


def _dates(n: int, start: str = "2020-01-01") -> list[str]:
    return pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()


def _candidate(values: list[float], rics: tuple[str, ...], lookbacks=(10,)) -> ScanCandidateResult:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0,), weights=(1.0,), interval=BarInterval.DAILY,
    )
    instance = StrategyInstance(definition=definition, rics=rics)
    df = pd.DataFrame(
        {"Date": pd.to_datetime(_dates(len(values))), "Leg_1": values, "Strategy": values}
    )
    history = StrategyHistory(instance=instance, price_field="Close", history=df)
    multi_lookback = analyze_multi_lookback(history, lookbacks=lookbacks)
    return ScanCandidateResult(
        market_key=definition.market_key,
        rics=rics,
        weights=definition.weights,
        offsets=definition.offsets,
        interval=definition.interval,
        price_field="Close",
        instance=instance,
        multi_lookback=multi_lookback,
    )


_LOW = _candidate([100.0] * 20, ("SRAH26",))     # current_price = 100.0
_MID = _candidate([105.0] * 20, ("SRAM26",))     # current_price = 105.0
_HIGH = _candidate([110.0] * 20, ("SRAU26",))    # current_price = 110.0
_NAN_CANDIDATE = _candidate([100.0], ("SRAZ26",), lookbacks=(10,))  # 1 obs -> efficiency_ratio NaN
# Monotonic -> non-degenerate (non-NaN) efficiency_ratio, unlike the flat
# _LOW/_MID/_HIGH fixtures above (a constant series has 0/0 efficiency_ratio).
_TRENDING = _candidate([100.0 + i for i in range(20)], ("SRAH27",))


def _price_key(ascending: bool) -> SortKey:
    return SortKey(at_lookback("current_price", 10), ascending=ascending)


def test_rank_results_no_keys_returns_original_order():
    candidates = [_HIGH, _LOW, _MID]
    assert rank_results(candidates, []) == candidates


def test_rank_results_single_key_ascending():
    ranked = rank_results([_HIGH, _LOW, _MID], [_price_key(ascending=True)])
    assert ranked == [_LOW, _MID, _HIGH]


def test_rank_results_single_key_descending():
    ranked = rank_results([_HIGH, _LOW, _MID], [_price_key(ascending=False)])
    assert ranked == [_HIGH, _MID, _LOW]


def test_rank_results_multi_key_tie_break():
    # Both candidates end at the same current_price (a genuine tie on
    # the primary key) but oscillate with different amplitude, so their
    # robust range width differs -- the secondary key must break the tie.
    narrow = _candidate(
        [99.0, 101.0, 99.0, 101.0, 99.0, 101.0, 99.0, 101.0, 99.0, 100.0], ("SRAH27",), lookbacks=(10,)
    )
    wide = _candidate(
        [90.0, 110.0, 90.0, 110.0, 90.0, 110.0, 90.0, 110.0, 90.0, 100.0], ("SRAM27",), lookbacks=(10,)
    )
    assert at_lookback("current_price", 10)(narrow) == at_lookback("current_price", 10)(wide) == 100.0

    keys = [
        SortKey(at_lookback("current_price", 10), ascending=True),
        SortKey(at_lookback("range_width_robust", 10), ascending=True),
    ]
    ranked = rank_results([wide, narrow], keys)
    assert ranked == [narrow, wide]


def test_rank_results_nan_sorts_last_ascending():
    accessor = at_lookback("efficiency_ratio", 10)
    assert math.isnan(accessor(_NAN_CANDIDATE))
    assert not math.isnan(accessor(_TRENDING))
    ranked = rank_results([_NAN_CANDIDATE, _TRENDING], [SortKey(accessor, ascending=True)])
    assert ranked[-1] is _NAN_CANDIDATE


def test_rank_results_nan_sorts_last_descending():
    accessor = at_lookback("efficiency_ratio", 10)
    ranked = rank_results([_NAN_CANDIDATE, _TRENDING], [SortKey(accessor, ascending=False)])
    assert ranked[-1] is _NAN_CANDIDATE


def test_rank_results_is_stable_for_equal_keys():
    equal_key = SortKey(lambda r: 1.0, ascending=True)
    candidates = [_LOW, _MID, _HIGH]
    assert rank_results(candidates, [equal_key]) == candidates


def test_rank_results_no_composite_score_attribute_exists():
    # SortKey has no field resembling a composite/opaque score -- only
    # accessor + direction.
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(SortKey)}
    assert field_names == {"accessor", "ascending"}


def test_rank_results_by_derived_metric_accessor():
    # Regression: ranking by a derived Module 5 metric (not a direct
    # RangeAnalytics field) works end-to-end now that at_lookback()
    # resolves it via metric_value().
    calm = _candidate([100.0] * 10, ("SRAH28",), lookbacks=(10,))
    busy = _candidate(
        [99.0, 101.0, 99.0, 101.0, 99.0, 101.0, 99.0, 101.0, 99.0, 101.0],
        ("SRAM28",), lookbacks=(10,),
    )

    accessor = at_lookback("normalized_crossing_frequency", 10)
    assert accessor(calm) < accessor(busy)  # sanity check on the fixtures

    ranked = rank_results([busy, calm], [SortKey(accessor, ascending=True)])
    assert ranked == [calm, busy]
