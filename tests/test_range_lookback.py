"""
tests/test_range_lookback.py

resolve_window tested with hand-built DataFrames -- no StrategyHistory
or mocking needed for a pure window-selection function.
"""

from __future__ import annotations

import pandas as pd
import pytest

from range_analytics.lookback import resolve_window


def _history(dates: list[str], values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Date": pd.to_datetime(dates), "Strategy": values})


def test_resolve_window_defaults_to_full_history():
    history = _history(["2026-01-01", "2026-01-02", "2026-01-03"], [1.0, 2.0, 3.0])
    result = resolve_window(history)
    assert result["Strategy"].tolist() == [1.0, 2.0, 3.0]


def test_resolve_window_lookback_takes_last_n_valid_observations():
    history = _history(
        ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
        [1.0, 2.0, 3.0, 4.0],
    )
    result = resolve_window(history, lookback=2)
    assert result["Strategy"].tolist() == [3.0, 4.0]


def test_resolve_window_lookback_skips_nan_rows_when_counting():
    history = _history(
        ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
        [1.0, float("nan"), 3.0, 4.0],
    )
    result = resolve_window(history, lookback=2)
    assert result["Strategy"].tolist() == [3.0, 4.0]


def test_resolve_window_start_end_filters_by_calendar_date():
    history = _history(["2026-01-01", "2026-01-05", "2026-01-10"], [1.0, 2.0, 3.0])
    result = resolve_window(history, start="2026-01-02", end="2026-01-05")
    assert result["Strategy"].tolist() == [2.0]


def test_resolve_window_start_only():
    history = _history(["2026-01-01", "2026-01-05", "2026-01-10"], [1.0, 2.0, 3.0])
    result = resolve_window(history, start="2026-01-05")
    assert result["Strategy"].tolist() == [2.0, 3.0]


def test_resolve_window_rejects_both_lookback_and_start():
    history = _history(["2026-01-01"], [1.0])
    with pytest.raises(ValueError):
        resolve_window(history, lookback=1, start="2026-01-01")


def test_resolve_window_rejects_non_positive_lookback():
    history = _history(["2026-01-01"], [1.0])
    with pytest.raises(ValueError):
        resolve_window(history, lookback=0)


def test_resolve_window_raises_on_duplicate_dates():
    history = _history(["2026-01-01", "2026-01-01"], [1.0, 2.0])
    with pytest.raises(ValueError):
        resolve_window(history)


def test_resolve_window_empty_history_returns_empty_frame():
    history = pd.DataFrame(columns=["Date", "Strategy"])
    result = resolve_window(history)
    assert result.empty


def test_resolve_window_unsorted_input_is_sorted_by_date():
    history = _history(["2026-01-03", "2026-01-01", "2026-01-02"], [3.0, 1.0, 2.0])
    result = resolve_window(history)
    assert result["Strategy"].tolist() == [1.0, 2.0, 3.0]
