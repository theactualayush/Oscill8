"""
strategy_set_scan_view.py

"Strategy Set Scan": a SEPARATE, additive execution workflow from the
existing grid ("Run Scan") path -- select a saved Strategy Set, pick
ONE interval, run every entry at that interval for this scan only. The
grid, its per-row Market/Interval, and template_scanner.scanner.
run_scan() are completely untouched by this module; a mixed-interval
Strategy Set loaded into the grid still runs exactly as before via
ui.scan_view.handle_run_scan().

Rendering is deliberately minimal: one interval selectbox + one button,
shown only when a saved Strategy Set is currently selected (see ui.
strategy_set_state.get_selected_name()) -- no separate results/chart
UI. handle_run_strategy_set_scan() stores its result via the SAME ui.
state.store_scan_result()/store_scan_error() the grid path already
uses, so ui.results_view/ui.chart_view render either kind of scan
identically with no changes of their own.

The button is captured here (inside ui.controls._render_strategy_
templates(), which renders before the scan-bar's own contract/price/
lookback/percentile controls) but its actual execution is deferred to
handle_run_strategy_set_scan(setup), called from ui/app.py AFTER
render_scan_setup() has returned a complete ScanSetup -- the same
"capture now, process once the rest of the page has rendered" split
ui.strategy_set_view.render_save_button()/process_save() already uses,
for the same reason (the click happens before the values it needs
exist in this script pass).
"""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

import streamlit as st

from core.config import BarInterval

from strategy_sets.execution import run_strategy_set
from strategy_sets.repository import StrategySetRepository

from ui import state
from ui import strategy_set_state as ss_state
from ui.error_formatting import classify_scan_error

if TYPE_CHECKING:  # avoid a runtime circular import -- ui.controls imports this module
    from ui.controls import ScanSetup

_INTERVALS: tuple[BarInterval, ...] = (BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR)

_INTERVAL_HELP = (
    "Applied to every strategy in the selected Strategy Set for this scan only -- the saved "
    "Strategy Set itself is never modified."
)


def render_controls(selected_name: str) -> tuple[bool, BarInterval]:
    """The interval selectbox + Run button for the currently selected
    saved Strategy Set. Returns (clicked_this_rerun, chosen_interval) --
    the caller (ui.controls) threads these through ScanSetup so the
    actual run can happen once contract/price/lookback/percentile
    values are known (see the module docstring)."""
    st.caption("STRATEGY SET SCAN")
    col_interval, col_button = st.columns([2, 1.6], vertical_alignment="bottom")
    with col_interval:
        interval = st.selectbox(
            "Interval",
            _INTERVALS,
            format_func=lambda i: i.value,
            key="oscill8_ss_scan_interval",
            help=_INTERVAL_HELP,
        )
    with col_button:
        clicked = st.button(
            f"▶ Run '{selected_name}'", key="oscill8_ss_scan_run_button", width="stretch",
        )
    return clicked, interval


def handle_run_strategy_set_scan(setup: "ScanSetup") -> None:
    """Runs after render_scan_setup() has returned a complete ScanSetup
    -- a no-op unless the Strategy Set Scan button was actually clicked
    this rerun (setup.strategy_set_scan_requested)."""
    if not setup.strategy_set_scan_requested:
        return

    state.store_scan_error(None)

    selected_name = ss_state.get_selected_name()
    if selected_name is None:
        # The button only renders when a saved set is selected -- this
        # is a defensive no-op, not a reachable user-facing state.
        return

    if setup.display_lookback is None:
        st.error("Select at least one lookback before running a scan.")
        return

    interval = setup.strategy_set_scan_interval
    repo = StrategySetRepository()

    try:
        strategy_set = repo.load(selected_name)
        request, report = run_strategy_set(
            strategy_set,
            interval,
            contract_start=setup.contract_start,
            contract_end=setup.contract_end,
            price_start=setup.price_start,
            price_end=setup.price_end,
            lookbacks=setup.lookbacks,
            lower_percentile=setup.lower_percentile,
            upper_percentile=setup.upper_percentile,
        )
    except Exception as exc:  # noqa: BLE001 -- UI boundary: surface, don't classify (see ui.scan_view)
        presentation = classify_scan_error(type(exc).__name__, str(exc))
        technical = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        state.store_scan_error((presentation, technical))
        return

    state.store_scan_result(request, report, setup.display_lookback)


__all__ = ["render_controls", "handle_run_strategy_set_scan"]
