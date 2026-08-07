"""
test_ui_chart.py

Tests for Module 6B's pure figure-builder (ui/chart_view.build_strategy_chart).
build_strategy_chart() takes an already-fetched raw history DataFrame
and an already-computed RangeAnalytics (real objects from range_analytics,
never hand-faked field-by-field) and only shapes them into a Plotly
figure -- no analytics are computed here, so these tests check the
figure's structure, not any statistical value.

get_selected_history() (the cache-aware fetch) is not tested here: it's
a thin, obviously-correct call to the existing build_history()/database.
get_history(), exercised via the browser smoke test instead, matching
how ui.scan_view.handle_run_scan's own run_scan() call is verified.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from core.config import BarInterval

from range_analytics.results import analyze_range

from strategy_engine.combinations import StrategyInstance
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.pricing import StrategyHistory

from ui.chart_view import _build_rangebreaks, _missing_weekdays, build_strategy_chart


def _history(n: int) -> StrategyHistory:
    dates = pd.bdate_range("2024-01-01", periods=n)
    values = [100.0 + 0.01 * (i % 7) - 0.005 * (i % 5) for i in range(n)]
    df = pd.DataFrame({"Date": dates, "Leg_1": values, "Leg_2": values, "Strategy": values})
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1.0, -1.0), interval=BarInterval.DAILY
    )
    instance = StrategyInstance(definition=definition, rics=("SRAZ25", "SRAH26"))
    return StrategyHistory(instance=instance, history=df, price_field="Close")


def _history_from_dates(dates: pd.DatetimeIndex) -> StrategyHistory:
    values = [100.0 + 0.01 * (i % 7) - 0.005 * (i % 5) for i in range(len(dates))]
    df = pd.DataFrame({"Date": dates, "Leg_1": values, "Leg_2": values, "Strategy": values})
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1.0, -1.0), interval=BarInterval.DAILY
    )
    instance = StrategyInstance(definition=definition, rics=("SRAZ25", "SRAH26"))
    return StrategyHistory(instance=instance, history=df, price_field="Close")


def test_build_strategy_chart_includes_price_line_and_current_marker():
    history = _history(100)
    analytics = analyze_range(history, lookback=20)

    fig = build_strategy_chart(history.history, lookback=20, analytics=analytics)

    assert isinstance(fig, go.Figure)
    trace_names = [t.name for t in fig.data]
    assert "Strategy" in trace_names
    assert "Current" in trace_names


def test_build_strategy_chart_plots_exactly_the_lookback_window():
    history = _history(100)
    analytics = analyze_range(history, lookback=20)

    fig = build_strategy_chart(history.history, lookback=20, analytics=analytics)

    price_trace = next(t for t in fig.data if t.name == "Strategy")
    assert len(price_trace.x) == 20


def test_build_strategy_chart_adds_robust_range_band_when_defined():
    history = _history(100)
    analytics = analyze_range(history, lookback=20)

    fig = build_strategy_chart(history.history, lookback=20, analytics=analytics)

    assert len(fig.layout.shapes) >= 1  # the shaded robust-range rectangle


def test_build_strategy_chart_title_reflects_lookback():
    history = _history(100)
    analytics = analyze_range(history, lookback=40)

    fig = build_strategy_chart(history.history, lookback=40, analytics=analytics)

    assert "40 bars" in fig.layout.title.text


def test_build_strategy_chart_title_reflects_percentile_range():
    history = _history(100)
    analytics = analyze_range(history, lookback=40, lower_percentile=25.0, upper_percentile=75.0)

    fig = build_strategy_chart(history.history, lookback=40, analytics=analytics)

    assert "P25-P75" in fig.layout.title.text


def test_build_strategy_chart_default_percentile_range_in_title():
    history = _history(100)
    analytics = analyze_range(history, lookback=40)

    fig = build_strategy_chart(history.history, lookback=40, analytics=analytics)

    assert "P5-P95" in fig.layout.title.text


def test_build_strategy_chart_includes_a_subtle_mean_line():
    history = _history(100)
    analytics = analyze_range(history, lookback=20)

    fig = build_strategy_chart(history.history, lookback=20, analytics=analytics)

    mean_lines = [s for s in fig.layout.shapes if s.type == "line"]
    # at least Robust Low, Median, Robust High, Mean -- 4 hline shapes
    assert len(mean_lines) >= 4


def test_build_strategy_chart_handles_empty_window_gracefully():
    history = _history(0)
    analytics = analyze_range(history, lookback=20)  # NaN low/median/high on an empty window

    fig = build_strategy_chart(history.history, lookback=20, analytics=analytics)

    assert isinstance(fig, go.Figure)
    price_trace = next(t for t in fig.data if t.name == "Strategy")
    assert len(price_trace.x) == 0
    assert "Current" not in [t.name for t in fig.data]
    assert len(fig.layout.shapes) == 0  # no robust-range band on undefined bounds


# --------------------------------------------------------------------------
# Trading-day rangebreaks (data-integrity phase)
# --------------------------------------------------------------------------


def test_missing_weekdays_empty_for_pure_weekend_gap():
    # bdate_range never contains a Saturday/Sunday row to begin with --
    # a plain week-to-week transition has no OTHER missing weekday.
    dates = pd.bdate_range("2024-01-04", periods=2)  # Thu 01-04, Fri 01-05
    dates = dates.append(pd.bdate_range("2024-01-08", periods=1))  # Mon 01-08
    assert _missing_weekdays(pd.Series(dates)) == []


def test_missing_weekdays_detects_a_dropped_business_day():
    bdays = pd.bdate_range("2024-01-01", periods=10)
    dropped = bdays[5]
    dates = pd.Series(bdays.delete(5))

    missing = _missing_weekdays(dates)

    assert missing == [pd.Timestamp(dropped).normalize()]


def test_missing_weekdays_never_includes_a_saturday_or_sunday():
    bdays = pd.bdate_range("2024-01-01", periods=10)
    dates = pd.Series(bdays.delete(5))

    missing = _missing_weekdays(dates)

    assert all(d.weekday() < 5 for d in missing)


def test_missing_weekdays_empty_for_empty_input():
    assert _missing_weekdays(pd.Series([], dtype="datetime64[ns]")) == []


def test_missing_weekdays_normalizes_time_of_day():
    bdays = pd.bdate_range("2024-01-01", periods=5)
    dropped = bdays[2]
    remaining = bdays.delete(2)
    # Give the remaining timestamps a non-midnight, inconsistent
    # time-of-day component -- normalization must still line them up
    # against the full calendar range without spuriously flagging any
    # of them as missing.
    dates = pd.Series([ts + pd.Timedelta(hours=i) for i, ts in enumerate(remaining)])

    missing = _missing_weekdays(dates)

    assert missing == [pd.Timestamp(dropped).normalize()]


def test_build_rangebreaks_weekend_only_gap_has_no_values_entry():
    dates = pd.Series(pd.bdate_range("2024-01-04", periods=2).append(pd.bdate_range("2024-01-08", periods=1)))

    breaks = _build_rangebreaks(dates)

    assert breaks == [dict(bounds=["sat", "mon"])]


def test_build_rangebreaks_includes_values_entry_for_a_holiday_gap():
    bdays = pd.bdate_range("2024-01-01", periods=10)
    dropped = bdays[5]
    dates = pd.Series(bdays.delete(5))

    breaks = _build_rangebreaks(dates)

    assert breaks[0] == dict(bounds=["sat", "mon"])
    assert len(breaks) == 2
    assert breaks[1]["values"] == [pd.Timestamp(dropped).normalize()]


def test_chart_rangebreaks_excludes_holiday_but_not_weekend_saturday_sunday():
    bdays = pd.bdate_range("2024-01-01", periods=20)
    dropped = bdays[10]
    dates = bdays.delete(10)
    history = _history_from_dates(dates)
    analytics = analyze_range(history, lookback=len(dates))

    fig = build_strategy_chart(history.history, lookback=len(dates), analytics=analytics)

    rangebreaks = fig.layout.xaxis.rangebreaks
    assert rangebreaks[0].bounds == ("sat", "mon")
    holiday_values = [pd.Timestamp(v) for v in rangebreaks[1].values]
    assert holiday_values == [pd.Timestamp(dropped).normalize()]
    # No Saturday/Sunday should ever appear in the explicit values list --
    # weekends are handled exclusively by the bounds entry above.
    assert all(v.weekday() < 5 for v in holiday_values)


def test_chart_preserves_real_chronological_date_labels_across_a_weekend():
    dates = pd.bdate_range("2024-01-04", periods=2).append(pd.bdate_range("2024-01-08", periods=1))
    history = _history_from_dates(dates)
    analytics = analyze_range(history, lookback=3)

    fig = build_strategy_chart(history.history, lookback=3, analytics=analytics)

    price_trace = next(t for t in fig.data if t.name == "Strategy")
    assert list(pd.to_datetime(price_trace.x)) == list(dates)
