"""
controls.py

Section A (Scan Setup) and Section B (Template / Ratio Grid) widgets.
Renders raw Streamlit controls and returns their current values as a
plain ScanSetup -- it builds no StrategyDefinition/ScanRequest itself;
that translation belongs to ui.scan_view + ui.formatting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core.config import MARKETS, BarInterval

_INTERVALS: tuple[BarInterval, ...] = (BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR)
_LOOKBACK_OPTIONS: tuple[int, ...] = (20, 40, 60, 90, 120)

_TEMPLATE_EDITOR_KEY = "oscill8_template_editor"
_DEFAULT_TEMPLATE_ROWS = pd.DataFrame({"Ratio": ["1 | -2 | 1"]})


@dataclass(frozen=True)
class ScanSetup:
    """Everything Section A/B currently hold, read live from widget
    state -- not yet validated or translated into backend objects."""

    market_key: str
    interval: BarInterval
    contract_start: date
    contract_end: date
    price_start: date
    price_end: date
    lookbacks: tuple[int, ...]
    display_lookback: int | None
    ratio_rows: list[str]


def _clamp_session_value(key: str, valid_options: tuple, fallback) -> None:
    """Reset a widget's persisted session value if it's no longer among
    valid_options (e.g. the user deselected a lookback that was
    previously chosen as the display lookback) -- avoids Streamlit
    raising on a selectbox whose stored value isn't in its options."""
    if st.session_state.get(key) not in valid_options:
        st.session_state[key] = fallback


def render_scan_setup() -> ScanSetup:
    st.subheader("Scan Setup")

    market_key = st.selectbox(
        "Market",
        list(MARKETS.keys()),
        format_func=lambda k: MARKETS[k].name,
        key="oscill8_market",
    )
    interval = st.selectbox(
        "Interval",
        _INTERVALS,
        format_func=lambda i: i.value,
        key="oscill8_interval",
    )

    today = date.today()
    col1, col2 = st.columns(2)
    with col1:
        contract_start = st.date_input(
            "Contract universe start", value=today - timedelta(days=730), key="oscill8_contract_start"
        )
        price_start = st.date_input(
            "Price-history start", value=today - timedelta(days=1095), key="oscill8_price_start"
        )
    with col2:
        contract_end = st.date_input(
            "Contract universe end", value=today + timedelta(days=730), key="oscill8_contract_end"
        )
        price_end = st.date_input("Price-history end", value=today, key="oscill8_price_end")

    lookbacks = st.multiselect(
        "Lookbacks", _LOOKBACK_OPTIONS, default=list(_LOOKBACK_OPTIONS), key="oscill8_lookbacks"
    )
    lookbacks_sorted = tuple(sorted(set(lookbacks)))

    if lookbacks_sorted:
        _clamp_session_value("oscill8_display_lookback", lookbacks_sorted, lookbacks_sorted[0])
        display_lookback = st.selectbox(
            "Display lookback", lookbacks_sorted, key="oscill8_display_lookback"
        )
    else:
        st.warning("Select at least one lookback.")
        display_lookback = None

    st.subheader("Strategy Ratios")
    st.caption(
        "One row per strategy shape, e.g. `1 | -2 | 1`. `0` skips that curve "
        "position -- the ratio itself defines the strategy, no need to name it "
        "outright/spread/fly/condor. Add or remove rows freely."
    )
    edited = st.data_editor(
        _DEFAULT_TEMPLATE_ROWS,
        num_rows="dynamic",
        key=_TEMPLATE_EDITOR_KEY,
        use_container_width=True,
        column_config={"Ratio": st.column_config.TextColumn("Ratio")},
    )
    ratio_rows = ["" if pd.isna(v) else str(v) for v in edited["Ratio"].tolist()]

    return ScanSetup(
        market_key=market_key,
        interval=interval,
        contract_start=contract_start,
        contract_end=contract_end,
        price_start=price_start,
        price_end=price_end,
        lookbacks=lookbacks_sorted,
        display_lookback=display_lookback,
        ratio_rows=ratio_rows,
    )
