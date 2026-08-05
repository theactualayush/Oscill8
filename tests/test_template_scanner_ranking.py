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


# ---------------------------------------------------------------------
# Z-score / absolute Z-score ranking
# ---------------------------------------------------------------------

# A noisy (non-degenerate) 19-bar background -- a single-outlier tail on
# an otherwise-CONSTANT background has a known quirk where |Z| converges
# to the same magnitude regardless of the outlier's size (an artifact of
# the in-sample formula on a zero-variance background), so a genuinely
# noisy background is used here to get real, distinguishable |Z| values.
_NOISY_BACKGROUND = [99.0, 101.0] * 9 + [99.0]  # 19 bars


def test_rank_results_by_z_score_ascending_and_descending():
    below_mean = _candidate(_NOISY_BACKGROUND + [60.0], ("SRAH29",), lookbacks=(20,))
    above_mean = _candidate(_NOISY_BACKGROUND + [130.0], ("SRAM29",), lookbacks=(20,))

    accessor = at_lookback("z_score", 20)
    z_below = accessor(below_mean)
    z_above = accessor(above_mean)
    assert z_below < 0 < z_above  # sanity check on the fixtures

    ascending = rank_results([above_mean, below_mean], [SortKey(accessor, ascending=True)])
    assert ascending == [below_mean, above_mean]

    descending = rank_results([above_mean, below_mean], [SortKey(accessor, ascending=False)])
    assert descending == [above_mean, below_mean]


def test_rank_results_by_absolute_z_score_surfaces_largest_dislocation_first():
    mild = _candidate(_NOISY_BACKGROUND + [101.0], ("SRAU29",), lookbacks=(20,))
    severe_negative = _candidate(_NOISY_BACKGROUND + [60.0], ("SRAZ29",), lookbacks=(20,))

    accessor = at_lookback("abs_z_score", 20)
    assert accessor(mild) < accessor(severe_negative)  # sanity check on the fixtures

    ranked_desc = rank_results([mild, severe_negative], [SortKey(accessor, ascending=False)])
    assert ranked_desc == [severe_negative, mild]  # largest |Z| dislocation ranked first

    ranked_asc = rank_results([mild, severe_negative], [SortKey(accessor, ascending=True)])
    assert ranked_asc == [mild, severe_negative]


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
