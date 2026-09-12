"""
scan_view.py

Run Scan: assembles the scan bar + strategy grid + the loaded Strategy
Set's non-grid content into ONE transient StrategySet and executes it
via strategy_sets.execution.run_strategy_set() exactly once per Run
Scan press. The skipped-candidates detail and the analyzed/skipped/
shown status render as part of ui.results_view's "Range-Bound
Opportunities" section, since they're one visual unit with the result
grid.

Single execution path (Task 1 simplification, completed in Phase 4):
this is the ONLY way a scan runs, whether the grid is a manually-typed
workspace or was loaded from a saved Strategy Set (see
ui.strategy_set_view). Phase 4 widened WHAT that one path can carry
rather than adding a second path: the grid can only represent ordinary
single-market rows, so a loaded set's Module 9 `intermarket_entries`
(previously displayed read-only but never scannable) and the composite
`groups` (Group A x Group B) currently held by ui.composite_view's
authoring panel are attached to the transient StrategySet alongside the
grid's own rows. All three kinds then expand through
strategy_sets.expansion into ONE mixed instance list and reach
template_scanner.scanner.run_scan_on_instances() -- the same pricing,
skip-handling, and Module 4A/4B analytics every scan has always used.
There is no composite scanner and no intermarket scanner.

TRANSIENT, never saved: the assembled StrategySet exists only for the
duration of one Run Scan press. It is never written to the repository,
and the Strategy Sets a composite's groups REFERENCE are only ever
read -- nothing here modifies a saved file, and no generated
combination, instance, or result is persisted anywhere.

Scan Configuration's Interval selector (setup.interval) remains the
single runtime interval for every leg: build_definitions_from_grid()
still reads each row's own persisted Interval (needed so a
mixed-interval Strategy Set round-trips through load/edit/save
unchanged), and run_strategy_set() then forces every leg to
setup.interval -- via with_interval_override() for the transient set's
own entries, and via composite resolution's own `interval` argument for
a group's SOURCE definitions, which live in other saved files and are
only loaded during resolution. So there is exactly one interval control
a trader can conflict with, never two, and a composite can never
silently run at a source file's persisted interval. Market has no such
override: each row's own Market always determines which market that leg
prices against (a Strategy Set's markets are exactly the markets its
rows carry -- there is no global Market selector to remove a conflict
from).

CompositeResolutionError is caught SEPARATELY, ahead of that
catch-all: its messages are written to be read by a trader (they name
the composite Strategy Set, which group, the source Strategy Set, and
the exact missing/offending value), so routing them through the generic
vendor/system classifier below would replace real, actionable text with
"the scan could not be completed". It is shown directly instead, the
same treatment ScanRequest's own ValueError already gets.

Exception handling here is otherwise deliberately an UI-boundary
catch-all, not a reimplementation of the scanner's own classification:
the scan pipeline already catches core.downloader.
MarketDataUnavailableError internally and reports it via
ScanReport.skipped, so it never reaches this module.
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

from strategy_sets.composite import CompositeResolutionError
from strategy_sets.execution import run_strategy_set

from ui import state
from ui.controls import ScanSetup
from ui.error_formatting import classify_scan_error
from ui.formatting import build_definitions_from_grid
from ui.strategy_set_formatting import build_strategy_set_from_grid

# Fallback market_key for build_definitions_from_grid()'s legacy
# grid-wide-market parameter -- unreachable in practice, since the
# grid's own Market column is a required SelectboxColumn (see
# ui.controls' column_config) and always populates every row. Any real
# configured market works here; it exists only so the function has a
# value to fall back to.
_FALLBACK_MARKET_KEY = next(iter(MARKETS))

# Name for the transient, never-saved StrategySet a Run Scan press
# assembles when no saved set is loaded (the manual-grid workflow).
# StrategySet validates its own name, so this must satisfy that pattern;
# it only ever appears in log lines and in a CompositeResolutionError's
# text, never in a filename -- nothing here writes to the repository.
_TRANSIENT_SET_NAME = "Current Scan"


def handle_run_scan(setup: ScanSetup) -> None:
    """Validate the scan bar/strategy grid's current values and, if
    valid, run exactly one scan. Does nothing to session state until
    validation passes."""
    state.store_scan_error(None)

    if setup.display_lookback is None:
        st.error("Select at least one lookback before running a scan.")
        return

    # Row-level shape errors are reported per row, before anything else
    # -- unchanged behaviour, and it keeps a bad row's message specific
    # rather than surfacing as a generic set-construction failure below.
    row_results = build_definitions_from_grid(
        setup.grid_rows, setup.position_columns, _FALLBACK_MARKET_KEY, setup.interval
    )
    errors = [r for r in row_results if r.error is not None]
    if errors:
        for err in errors:
            st.error(f"{err.label}: {err.error}")
        return

    loaded_set = setup.loaded_set
    intermarket_entries = () if loaded_set is None else loaded_set.intermarket_entries
    # The composite configuration comes from ScanSetup, not from
    # loaded_set.groups: ui.composite_view's authoring panel is what
    # currently holds it, and it seeds itself from the loaded set, so
    # the two agree whenever nothing was edited. Reading the LIVE value
    # is what makes an unsaved composite edit scan what the panel
    # actually shows -- exactly how an unsaved grid row already scans.
    groups = setup.groups

    # ONE transient StrategySet covering everything this scan should
    # cover: the grid's own rows, plus whatever the grid cannot
    # represent (Module 9 intermarket entries, composite groups) taken
    # by reference from the set loaded earlier in this same script pass.
    # Never saved; the sets a composite references are only ever read.
    try:
        transient_set = build_strategy_set_from_grid(
            loaded_set.name if loaded_set is not None else _TRANSIENT_SET_NAME,
            setup.grid_rows,
            setup.position_columns,
            _FALLBACK_MARKET_KEY,
            setup.interval,
            intermarket_entries=intermarket_entries,
            groups=groups,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    try:
        with st.spinner("Scanning market data..."):
            request, report = run_strategy_set(
                transient_set,
                setup.interval,
                setup.contract_start,
                setup.contract_end,
                setup.price_start,
                setup.price_end,
                lookbacks=setup.lookbacks,
                lower_percentile=setup.lower_percentile,
                upper_percentile=setup.upper_percentile,
                repository=setup.repository,
            )
    except CompositeResolutionError as exc:
        # Deliberately NOT routed through classify_scan_error(): these
        # messages already name the composite set, the group, the source
        # set, and the offending value, and are the actionable thing the
        # trader needs to fix.
        st.error(str(exc))
        return
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
