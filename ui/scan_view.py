"""
scan_view.py

Section C (Run Scan) and Section D (Skipped Candidates). Translates
Section A/B's raw control values into StrategyDefinitions and a
template_scanner.ScanRequest, calls the existing run_scan() exactly
once per Run Scan press, and renders the skipped-candidates section for
whatever ScanReport is currently in session state.

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

from template_scanner.scanner import ScanReport, ScanRequest, run_scan

from ui import state
from ui.controls import ScanSetup
from ui.formatting import build_definitions


def handle_run_scan(setup: ScanSetup) -> None:
    """Validate Section A/B's current values and, if valid, run exactly
    one scan. Does nothing to session state until validation passes."""
    state.store_scan_error(None)

    if setup.display_lookback is None:
        st.error("Select at least one lookback before running a scan.")
        return

    row_results = build_definitions(setup.ratio_rows, setup.market_key, setup.interval)
    errors = [r for r in row_results if r.error is not None]
    definitions = [r.definition for r in row_results if r.definition is not None]

    if errors:
        for err in errors:
            st.error(f"Ratio '{err.ratio_text}': {err.error}")
        return

    if not definitions:
        st.error("Add at least one strategy ratio (e.g. `1 | -2 | 1`) before running a scan.")
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


def render_skipped_section(report: ScanReport) -> None:
    analyzed = len(report.results)
    skipped = len(report.skipped)
    st.caption(f"{analyzed} analyzed · {skipped} skipped")
    if not report.skipped:
        return
    with st.expander(f"Skipped candidates ({skipped})"):
        for item in report.skipped:
            rics = " / ".join(item.instance.rics)
            st.write(f"**{rics}** — unavailable: `{item.unavailable_ric}` — {item.message}")
