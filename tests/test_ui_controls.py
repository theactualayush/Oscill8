"""
test_ui_controls.py

Tests for ui/controls.py's pure helpers: the automatic Universe window
(and the "first active contract" it resolves via the real
core.futures_calendar source of truth), the six-month default History
window, and (from ui.strategy_set_view, since ui.controls delegates the
blank-grid shape to it) the blank grid "+ New Strategy Set" starts
from. Everything else in controls.py renders Streamlit widgets directly
and is exercised via AppTest/the browser smoke test instead, per the
project's "avoid brittle Streamlit rendering tests" guidance.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

from core.config import BarInterval

from ui.controls import (
    _DEFAULT_LOWER_PERCENTILE,
    _DEFAULT_UPPER_PERCENTILE,
    _HISTORY_LOOKBACK_DAYS,
    _UNIVERSE_FORWARD_DAYS,
    ScanSetup,
    _default_history_window,
    _default_universe_window,
    _first_active_contract,
)
from ui.formatting import INTERVAL_COLUMN, LABEL_COLUMN, MARKET_COLUMN, position_column
from ui.strategy_set_view import blank_grid_row

_TODAY = date(2026, 8, 11)


# ---------------------------------------------------------------------
# Automatic Universe window -- no manual date inputs (Module 7B Part 9)
# ---------------------------------------------------------------------

def test_default_universe_window_starts_today():
    start, _end = _default_universe_window(_TODAY)
    assert start == _TODAY


def test_default_universe_window_reaches_a_fixed_forward_horizon():
    _start, end = _default_universe_window(_TODAY)
    assert end == _TODAY + timedelta(days=_UNIVERSE_FORWARD_DAYS)


def test_default_universe_window_never_reaches_backward():
    start, end = _default_universe_window(_TODAY)
    assert start >= _TODAY
    assert end > start


# ---------------------------------------------------------------------
# First active contract -- uses the REAL contract-calendar source of
# truth (core.futures_calendar.generate_contracts), not a bare
# date.today() assumption (see the module docstring's Universe note).
# ---------------------------------------------------------------------

def test_first_active_contract_matches_generate_contracts_directly():
    from core import futures_calendar

    start, end = _default_universe_window(_TODAY)
    expected = futures_calendar.generate_contracts("SOFR", start, end)[0]

    assert _first_active_contract("SOFR", _TODAY) == expected


def test_first_active_contract_excludes_already_elapsed_quarterly_months():
    # SOFR is QUARTERLY (Mar/Jun/Sep/Dec). On 2026-08-11, March and June
    # 2026 have already elapsed -- the first active contract must be
    # September 2026 (RIC month code "U"), not an earlier, elapsed one.
    assert _first_active_contract("SOFR", _TODAY) == "SRAU26"


def test_first_active_contract_differs_by_market_listing_cycle():
    # FED_FUNDS is MONTHLY -- "today" (2026-08-11) still falls inside
    # August, so its first active contract is August 2026 (RIC month
    # code "Q"), independent of SOFR's quarterly cycle which has
    # already elapsed past both March and June by this date.
    assert _first_active_contract("FED_FUNDS", _TODAY) == "FFQ26"


# ---------------------------------------------------------------------
# Default History window -- six months, not three years (Part 10)
# ---------------------------------------------------------------------

def test_default_history_window_ends_today():
    _start, end = _default_history_window(_TODAY)
    assert end == _TODAY


def test_default_history_window_starts_about_six_months_back():
    start, _end = _default_history_window(_TODAY)
    assert start == _TODAY - timedelta(days=_HISTORY_LOOKBACK_DAYS)
    # Pinned to guard against silently reverting to the old ~3-year
    # (1095-day) default -- six months is roughly 180-183 days.
    assert 175 <= _HISTORY_LOOKBACK_DAYS <= 190


def test_default_history_window_is_not_three_years():
    start, end = _default_history_window(_TODAY)
    assert (end - start).days < 366


# ---------------------------------------------------------------------
# Blank grid -- "+ New Strategy Set" starts from nothing pre-filled
# ---------------------------------------------------------------------

def test_blank_grid_row_has_no_prefilled_example():
    position_columns = tuple(position_column(i) for i in range(1, 7))
    df = blank_grid_row(position_columns, "SOFR", BarInterval.DAILY)

    assert len(df) == 1
    assert df.loc[0, LABEL_COLUMN] == ""
    for col in position_columns:
        assert df.loc[0, col] == ""


def test_blank_grid_row_column_count_matches_requested_positions():
    position_columns = tuple(position_column(i) for i in range(1, 5))
    df = blank_grid_row(position_columns, "SOFR", BarInterval.DAILY)
    position_cols = [c for c in df.columns if c not in (LABEL_COLUMN, MARKET_COLUMN, INTERVAL_COLUMN)]
    assert position_cols == list(position_columns)


def test_blank_grid_row_market_and_interval_default_to_the_scan_bars_current_selection():
    position_columns = tuple(position_column(i) for i in range(1, 5))
    df = blank_grid_row(position_columns, "SONIA", BarInterval.HOURLY)

    assert df.loc[0, MARKET_COLUMN] == "SONIA"
    assert df.loc[0, INTERVAL_COLUMN] == BarInterval.HOURLY.value


# ---------------------------------------------------------------------
# Percentile Range control -- ScanSetup carries lower/upper_percentile
# ---------------------------------------------------------------------

def test_default_percentile_constants_are_5_and_95():
    # The Streamlit number_input widgets default to these -- pinned here
    # so a future edit that changes the trader-facing default is a
    # visible, deliberate test change, not silent.
    assert _DEFAULT_LOWER_PERCENTILE == 5
    assert _DEFAULT_UPPER_PERCENTILE == 95


def test_scan_setup_has_percentile_fields():
    field_names = {f.name for f in dataclasses.fields(ScanSetup)}
    assert {"lower_percentile", "upper_percentile"}.issubset(field_names)


def test_scan_setup_still_carries_contract_start_end_even_though_automatic():
    # contract_start/contract_end are no longer user-entered, but
    # ScanSetup/ScanRequest still need them -- computed automatically
    # (see _default_universe_window) rather than removed.
    field_names = {f.name for f in dataclasses.fields(ScanSetup)}
    assert {"contract_start", "contract_end"}.issubset(field_names)


# ---------------------------------------------------------------------
# Task 1: no global Market field, no second Strategy Set Scan path
# ---------------------------------------------------------------------

def test_scan_setup_has_no_global_market_field():
    # Scan Configuration's Market dropdown is removed entirely -- a
    # Strategy Set's markets are exactly the markets its rows carry (the
    # grid's own per-row Market column), so there is no grid-wide market
    # for ScanSetup to hold.
    field_names = {f.name for f in dataclasses.fields(ScanSetup)}
    assert "market_key" not in field_names


def test_scan_setup_has_no_separate_strategy_set_scan_fields():
    # The former separate "Run '<Strategy Set>'" button/interval are
    # gone -- "▶ Run Scan" (run_clicked) is the only execution trigger.
    field_names = {f.name for f in dataclasses.fields(ScanSetup)}
    assert "strategy_set_scan_requested" not in field_names
    assert "strategy_set_scan_interval" not in field_names
