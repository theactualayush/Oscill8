"""
scan_view.py

Run Scan: translates the scan bar + strategy grid's current values into
StrategyDefinitions and a template_scanner.ScanRequest, and calls the
existing run_scan() exactly once per Run Scan press. The skipped-
candidates detail and the analyzed/skipped/shown status now render as
part of ui.results_view's "Range-Bound Opportunities" section, since
they're one visual unit with the result grid.

Single execution path (Task 1 simplification): this is now the ONLY way
a scan runs, whether the grid is a manually-typed workspace or was
loaded from a saved Strategy Set (see ui.strategy_set_view) -- the
separate "Run '<Strategy Set>'" button and its own interval selector
(formerly ui/strategy_set_scan_view.py) are gone. Scan Configuration's
Interval selector (setup.interval) is the single runtime interval for
every leg in the scan: build_definitions_from_grid() still reads each
row's own persisted Interval (needed so a mixed-interval Strategy Set
round-trips through load/edit/save unchanged), but
ui.formatting.apply_interval_override() then forces every resulting
StrategyDefinition to setup.interval before pricing -- so there is
exactly one interval control a trader can conflict with, never two.
Market has no such override: each row's own Market always determines
which market that leg prices against (a Strategy Set's markets are
exactly the markets its rows carry -- there is no global Market
selector to remove a conflict from).

Exception handling here is deliberately an UI-boundary catch-all, not a
reimplementation of run_scan()'s own classification: run_scan() already
catches core.downloader.MarketDataUnavailableError internally and
reports it via ScanReport.skipped, so it never reaches this module.
Anything that does reach here (session/auth/network failures, a
programming bug) is shown to the user, not silently retried or
reclassified -- what changes is only how it's PRESENTED: the raw
exception is still caught, still fully preserved as technical detail,
and still shown, but ui.error_formatting.classify_scan_error() derives
a short trader-facing headline from it first (see render_scan_error()
below) rather than putting the exception type/message in front of the
user directly.
"""

from __future__ import annotations

import traceback

import streamlit as st

from core.config import MARKETS

from template_scanner.scanner import ScanRequest, run_scan

from ui import state
from ui.controls import ScanSetup
from ui.error_formatting import classify_scan_error
from ui.formatting import apply_interval_override, build_definitions_from_grid

# Fallback market_key for build_definitions_from_grid()'s legacy
# grid-wide-market parameter -- unreachable in practice, since the
# grid's own Market column is a required SelectboxColumn (see
# ui.controls' column_config) and always populates every row. Any real
# configured market works here; it exists only so the function has a
# value to fall back to.
_FALLBACK_MARKET_KEY = next(iter(MARKETS))


def handle_run_scan(setup: ScanSetup) -> None:
    """Validate the scan bar/strategy grid's current values and, if
    valid, run exactly one scan. Does nothing to session state until
    validation passes."""
    state.store_scan_error(None)

    if setup.display_lookback is None:
        st.error("Select at least one lookback before running a scan.")
        return

    row_results = build_definitions_from_grid(
        setup.grid_rows, setup.position_columns, _FALLBACK_MARKET_KEY, setup.interval
    )
    errors = [r for r in row_results if r.error is not None]
    definitions = apply_interval_override(
        [r.definition for r in row_results if r.definition is not None], setup.interval
    )

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
            lower_percentile=setup.lower_percentile,
            upper_percentile=setup.upper_percentile,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    try:
        with st.spinner("Scanning market data..."):
            report = run_scan(request)
    except Exception as exc:  # noqa: BLE001 -- UI boundary: surface, don't classify
        presentation = classify_scan_error(type(exc).__name__, str(exc))
        technical = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        state.store_scan_error((presentation, technical))
        return

    state.store_scan_result(request, report, setup.display_lookback)


def render_scan_error() -> None:
    """Shows the classified, trader-facing headline/message as the
    PRIMARY error -- never a Python traceback, LSEG error code, file
    path, or exception type/message. The full technical detail (what
    used to be the entire visible error) is still shown, unmodified,
    but only inside the collapsed "Technical details" expander."""
    error = state.get_scan_error()
    if error is None:
        return
    presentation, technical = error
    st.error(f"**{presentation.title}**\n\n{presentation.message}")
    with st.expander("Technical details"):
        st.code(technical)
