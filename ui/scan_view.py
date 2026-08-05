"""
scan_view.py

Run Scan: translates the scan bar + strategy grid's current values into
StrategyDefinitions and a template_scanner.ScanRequest, and calls the
existing run_scan() exactly once per Run Scan press. The skipped-
candidates detail and the analyzed/skipped/shown status now render as
part of ui.results_view's "Range-Bound Opportunities" section, since
they're one visual unit with the result grid.

Exception handling here is deliberately an UI-boundary catch-all, not a
reimplementation of run_scan()'s own classification: run_scan() already
catches core.downloader.MarketDataUnavailableError internally and
reports it via ScanReport.skipped, so it never reaches this module.
Anything that does reach here (session/auth/network failures, a
programming bug) is shown to the user, not silently retried or
reclassified.
"""

from __future__ import annotations

import traceback

import streamlit as st

from template_scanner.scanner import ScanRequest, run_scan

from ui import state
from ui.controls import ScanSetup
from ui.formatting import build_definitions_from_grid


def handle_run_scan(setup: ScanSetup) -> None:
    """Validate the scan bar/strategy grid's current values and, if
    valid, run exactly one scan. Does nothing to session state until
    validation passes."""
    state.store_scan_error(None)

    if setup.display_lookback is None:
        st.error("Select at least one lookback before running a scan.")
        return

    row_results = build_definitions_from_grid(
        setup.grid_rows, setup.position_columns, setup.market_key, setup.interval
    )
    errors = [r for r in row_results if r.error is not None]
    definitions = [r.definition for r in row_results if r.definition is not None]

    if errors:
        for err in errors:
            st.error(f"{err.label}: {err.error}")
        return

    if not definitions:
        st.error("Add at least one strategy row with a nonzero ratio before running a scan.")
        return

    try:
        request = ScanRequest(
            definitions=tuple(definitions),
            contract_start=setup.contract_start,
            contract_end=setup.contract_end,
            price_start=setup.price_start,
            price_end=setup.price_end,
            lookbacks=setup.lookbacks,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    try:
        with st.spinner("Scanning market data..."):
            report = run_scan(request)
    except Exception as exc:  # noqa: BLE001 -- UI boundary: surface, don't classify
        state.store_scan_error(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        return

    state.store_scan_result(request, report, setup.display_lookback)


def render_scan_error() -> None:
    message = st.session_state.get(state.SCAN_ERROR)
    if not message:
        return
    st.error("The scan failed to complete. See technical details below.")
    with st.expander("Technical details"):
        st.code(message)
