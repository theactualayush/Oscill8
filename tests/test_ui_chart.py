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

from ui.chart_view import build_strategy_chart


def _history(n: int) -> StrategyHistory:
    dates = pd.bdate_range("2024-01-01", periods=n)
    values = [100.0 + 0.01 * (i % 7) - 0.005 * (i % 5) for i in range(n)]
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
