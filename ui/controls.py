"""
controls.py

The compact scan configuration panel (market/interval/dates/lookbacks/
Run Scan) and the strategy grid (curve positions as columns, one row
per template). Renders raw Streamlit controls and returns their current
values as a plain ScanSetup -- it builds no StrategyDefinition/
ScanRequest itself; that translation belongs to ui.scan_view +
ui.formatting.

The grid's column headers are bare curve-position numbers, not real
contract codes: template_from_dense_weights() + generate_instances()
roll a position-relative shape across every eligible starting point in
the contract universe, so "position 1" is a different real RIC for each
rolled instance -- there is no single fixed contract per column to show
truthfully. See ui.formatting.CURVE_POSITION_HELP for the caption that
explains this once, rather than repeating it per column.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core.config import MARKETS, BarInterval

from ui.formatting import (
    CURVE_POSITION_HELP,
    HISTORY_HELP,
    PERCENTILE_RANGE_HELP,
    PRIMARY_LOOKBACK_HELP,
    UNIVERSE_HELP,
    position_column,
)

_INTERVALS: tuple[BarInterval, ...] = (BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR)
_LOOKBACK_OPTIONS: tuple[int, ...] = (20, 40, 60, 90, 120)

_DEFAULT_POSITIONS = 6
_MIN_POSITIONS = 2
_MAX_POSITIONS = 12

_DEFAULT_LOWER_PERCENTILE = 5
_DEFAULT_UPPER_PERCENTILE = 95


@dataclass(frozen=True)
class ScanSetup:
    """Everything the scan panel and strategy grid currently hold, read
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
    lower_percentile: float
    upper_percentile: float
    grid_rows: list[dict]
    position_columns: tuple[str, ...]
    run_clicked: bool


def _clamp_session_value(key: str, valid_options: tuple, fallback) -> None:
    """Reset a widget's persisted session value if it's no longer among
    valid_options (e.g. the user deselected a lookback that was
    previously chosen as the primary lookback) -- avoids Streamlit
    raising on a selectbox whose stored value isn't in its options."""
    if st.session_state.get(key) not in valid_options:
        st.session_state[key] = fallback


def _default_grid(n_positions: int) -> pd.DataFrame:
    """One example row (a fly at the front of the curve). Unpopulated
    positions are an empty string, not a typed 0 -- position cells are
    TextColumns (not NumberColumns; see _render_strategy_grid) so an
    empty string renders as a genuinely blank cell instead of a
    distracting row of zeros, while still meaning "skip this position"
    once translated (see ui.formatting.build_definitions_from_grid).
    """
    base = ["1", "-2", "1"] + [""] * max(0, n_positions - 3)
    data = {"Label": ["3M Fly"]}
    for i in range(1, n_positions + 1):
        data[position_column(i)] = [base[i - 1]]
    return pd.DataFrame(data)


def render_scan_setup() -> ScanSetup:
    main, _ = st.columns([5, 1])
    with main:
        with st.container(border=True):
            st.subheader("Oscill8 — Range-Bound Scanner")
            setup_values = _render_scan_bar()

        with st.container(border=True):
            grid_rows, position_columns = _render_strategy_grid()

    return ScanSetup(
        grid_rows=grid_rows,
        position_columns=position_columns,
        **setup_values,
    )


def _render_scan_bar() -> dict:
    today = date.today()

    row1 = st.columns([1, 1, 2, 2, 1.4])
    with row1[0]:
        market_key = st.selectbox(
            "Market", list(MARKETS.keys()), format_func=lambda k: MARKETS[k].name, key="oscill8_market"
        )
    with row1[1]:
        interval = st.selectbox(
            "Interval", _INTERVALS, format_func=lambda i: i.value, key="oscill8_interval"
        )
    with row1[2]:
        st.caption("Universe", help=UNIVERSE_HELP)
        u1, u2 = st.columns(2)
        with u1:
            contract_start = st.date_input(
                "Contract Start", value=today - timedelta(days=730), key="oscill8_contract_start",
                help=UNIVERSE_HELP,
            )
        with u2:
            contract_end = st.date_input(
                "Contract End", value=today + timedelta(days=730), key="oscill8_contract_end",
                help=UNIVERSE_HELP,
            )
    with row1[3]:
        st.caption("History", help=HISTORY_HELP)
        h1, h2 = st.columns(2)
        with h1:
            price_start = st.date_input(
                "Price History Start", value=today - timedelta(days=1095), key="oscill8_price_start",
                help=HISTORY_HELP,
            )
        with h2:
            price_end = st.date_input(
                "Price History End", value=today, key="oscill8_price_end", help=HISTORY_HELP,
            )
    with row1[4]:
        st.caption("Percentile Range", help=PERCENTILE_RANGE_HELP)
        p1, p2 = st.columns(2)
        with p1:
            lower_percentile = st.number_input(
                "Lower", min_value=0, max_value=100, value=_DEFAULT_LOWER_PERCENTILE, step=1,
                key="oscill8_lower_percentile", help=PERCENTILE_RANGE_HELP,
            )
        with p2:
            upper_percentile = st.number_input(
                "Upper", min_value=0, max_value=100, value=_DEFAULT_UPPER_PERCENTILE, step=1,
                key="oscill8_upper_percentile", help=PERCENTILE_RANGE_HELP,
            )

    row2 = st.columns([3, 1.5, 1.5])
    with row2[0]:
        lookbacks = st.multiselect(
            "Lookbacks (bars)", _LOOKBACK_OPTIONS, default=list(_LOOKBACK_OPTIONS), key="oscill8_lookbacks"
        )
    lookbacks_sorted = tuple(sorted(set(lookbacks)))
    with row2[1]:
        if lookbacks_sorted:
            _clamp_session_value("oscill8_display_lookback", lookbacks_sorted, lookbacks_sorted[0])
            display_lookback = st.selectbox(
                "Primary Lookback",
                lookbacks_sorted,
                format_func=lambda n: f"{n} bars",
                help=PRIMARY_LOOKBACK_HELP,
                key="oscill8_display_lookback",
            )
        else:
            st.warning("Select a lookback")
            display_lookback = None
    with row2[2]:
        st.write("")  # baseline-align the button with the selectboxes above
        run_clicked = st.button("▶ Run Scan", type="primary", width="stretch")

    return {
        "market_key": market_key,
        "interval": interval,
        "contract_start": contract_start,
        "contract_end": contract_end,
        "price_start": price_start,
        "price_end": price_end,
        "lookbacks": lookbacks_sorted,
        "display_lookback": display_lookback,
        "lower_percentile": float(lower_percentile),
        "upper_percentile": float(upper_percentile),
        "run_clicked": run_clicked,
    }


def _render_strategy_grid() -> tuple[list[dict], tuple[str, ...]]:
    header_col, positions_col = st.columns([4, 1])
    with header_col:
        st.subheader("Strategy Templates")
    with positions_col:
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
    for i, col in enumerate(position_columns, start=1):
        # TextColumn, not NumberColumn: verified empirically that this
        # Streamlit build renders an unpopulated/NaN NumberColumn cell as
        # the literal text "None" regardless of dtype, while a TextColumn
        # with an empty string renders correctly blank. The `validate`
        # regex still constrains committed input to a numeric-looking
        # pattern (optional sign, digits, optional decimal) or blank.
        column_config[col] = st.column_config.TextColumn(
            str(i), width="small", validate=r"^-?\d*\.?\d*$"
        )

    edited = st.data_editor(
        _default_grid(n_positions),
        num_rows="dynamic",
        key=f"oscill8_template_grid_{n_positions}",
        column_config=column_config,
    )
    grid_rows = edited.to_dict("records")

    return grid_rows, position_columns
