"""
controls.py

The compact scan bar (market/interval/dates/lookbacks/Run Scan) and the
strategy grid (curve positions as columns, one row per template).
Renders raw Streamlit controls and returns their current values as a
plain ScanSetup -- it builds no StrategyDefinition/ScanRequest itself;
that translation belongs to ui.scan_view + ui.formatting.

The grid's column headers are plain curve-position numbers, not real
contract codes: template_from_dense_weights() + generate_instances()
roll a position-relative shape across every eligible starting point in
the contract universe, so "position 1" is a different real RIC for each
rolled instance -- there is no single fixed contract per column to show
truthfully. An illustrative "first eligible combination" caption (pure
core.futures_calendar calendar arithmetic, no LSEG call) gives a trader
a concrete example without implying the grid represents one fixed set
of real contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import futures_calendar
from core.config import MARKETS, BarInterval

from ui.formatting import CURVE_POSITION_HELP, position_column

_INTERVALS: tuple[BarInterval, ...] = (BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR)
_LOOKBACK_OPTIONS: tuple[int, ...] = (20, 40, 60, 90, 120)

_DEFAULT_POSITIONS = 8
_MIN_POSITIONS = 2
_MAX_POSITIONS = 12


@dataclass(frozen=True)
class ScanSetup:
    """Everything the scan bar and strategy grid currently hold, read
    live from widget state -- not yet validated or translated into
    backend objects."""

    market_key: str
    interval: BarInterval
    contract_start: date
    contract_end: date
    price_start: date
    price_end: date
    lookbacks: tuple[int, ...]
    display_lookback: int | None
    grid_rows: list[dict]
    position_columns: tuple[str, ...]
    run_clicked: bool


def _clamp_session_value(key: str, valid_options: tuple, fallback) -> None:
    """Reset a widget's persisted session value if it's no longer among
    valid_options (e.g. the user deselected a lookback that was
    previously chosen as the display lookback) -- avoids Streamlit
    raising on a selectbox whose stored value isn't in its options."""
    if st.session_state.get(key) not in valid_options:
        st.session_state[key] = fallback


def _default_grid(n_positions: int) -> pd.DataFrame:
    """One example row (a fly at the front of the curve) sized to
    whatever position count is currently selected."""
    base = [1.0, -2.0, 1.0] + [0.0] * max(0, n_positions - 3)
    data = {"Label": ["Strategy 1"]}
    for i in range(1, n_positions + 1):
        data[position_column(i)] = [base[i - 1]]
    return pd.DataFrame(data)


def _example_combination(market_key: str, contract_start, contract_end, n_positions: int) -> list[str]:
    """Best-effort illustrative example only -- never blocks the grid.
    Pure calendar arithmetic (core.futures_calendar), no LSEG call."""
    try:
        contracts = futures_calendar.generate_contracts(market_key, contract_start, contract_end)
    except Exception:
        return []
    return contracts[:n_positions]


def render_scan_setup() -> ScanSetup:
    st.subheader("Oscill8 · Range-Bound Scanner")

    today = date.today()
    row1 = st.columns(6)
    with row1[0]:
        market_key = st.selectbox(
            "Market", list(MARKETS.keys()), format_func=lambda k: MARKETS[k].name, key="oscill8_market"
        )
    with row1[1]:
        interval = st.selectbox(
            "Interval", _INTERVALS, format_func=lambda i: i.value, key="oscill8_interval"
        )
    with row1[2]:
        contract_start = st.date_input(
            "Universe start", value=today - timedelta(days=730), key="oscill8_contract_start"
        )
    with row1[3]:
        contract_end = st.date_input(
            "Universe end", value=today + timedelta(days=730), key="oscill8_contract_end"
        )
    with row1[4]:
        price_start = st.date_input(
            "History start", value=today - timedelta(days=1095), key="oscill8_price_start"
        )
    with row1[5]:
        price_end = st.date_input("History end", value=today, key="oscill8_price_end")

    row2 = st.columns([4, 1.5, 1.5])
    with row2[0]:
        lookbacks = st.multiselect(
            "Lookbacks", _LOOKBACK_OPTIONS, default=list(_LOOKBACK_OPTIONS), key="oscill8_lookbacks"
        )
    lookbacks_sorted = tuple(sorted(set(lookbacks)))
    with row2[1]:
        if lookbacks_sorted:
            _clamp_session_value("oscill8_display_lookback", lookbacks_sorted, lookbacks_sorted[0])
            display_lookback = st.selectbox(
                "Display", lookbacks_sorted, key="oscill8_display_lookback"
            )
        else:
            st.warning("Select a lookback")
            display_lookback = None
    with row2[2]:
        st.write("")  # baseline-align the button with the selectboxes above
        run_clicked = st.button("▶ Run Scan", type="primary", width="stretch")

    st.subheader("Strategy Templates")
    n_positions = st.number_input(
        "Positions",
        min_value=_MIN_POSITIONS,
        max_value=_MAX_POSITIONS,
        value=_DEFAULT_POSITIONS,
        step=1,
        key="oscill8_positions",
        help="How many curve positions to show. Changing this resets the grid below.",
    )
    st.caption(CURVE_POSITION_HELP)

    position_columns = tuple(position_column(i) for i in range(1, n_positions + 1))
    column_config = {"Label": st.column_config.TextColumn("Label", width="small")}
    for col in position_columns:
        column_config[col] = st.column_config.NumberColumn(col, width="small", step=1)

    edited = st.data_editor(
        _default_grid(n_positions),
        num_rows="dynamic",
        key=f"oscill8_template_grid_{n_positions}",
        column_config=column_config,
    )
    grid_rows = edited.to_dict("records")

    example = _example_combination(market_key, contract_start, contract_end, n_positions)
    if example:
        st.caption(f"Example — first eligible combination in the selected universe: {' / '.join(example)}")

    return ScanSetup(
        market_key=market_key,
        interval=interval,
        contract_start=contract_start,
        contract_end=contract_end,
        price_start=price_start,
        price_end=price_end,
        lookbacks=lookbacks_sorted,
        display_lookback=display_lookback,
        grid_rows=grid_rows,
        position_columns=position_columns,
        run_clicked=run_clicked,
    )
