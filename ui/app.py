"""
app.py

Module 6A/6B/7B entry point: scan panel (Market/Data, Universe,
History, Analytics, Run Scan) + Strategy Templates grid (with its
integrated Strategy Set selector/Save) -> Run Scan -> Range-Bound
Opportunities (status, ranking/filters, ranked result grid, skipped
candidates) -> Selected Strategy summary -> Selected Strategy history
chart. Thin orchestration only -- every analytics, filtering, ranking,
pricing, and Strategy Set persistence computation is delegated to
strategy_engine / range_analytics / template_scanner / strategy_sets,
unmodified. There is exactly one strategy grid and one Run Scan button
-- a loaded Strategy Set becomes ordinary grid rows and takes the same
Run Scan path a manually-typed row does (see ui.controls/ui.
strategy_set_view).

Run with: streamlit run ui/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run ui/app.py` puts ui/ (not the repo root) on sys.path, so
# `ui`'s own sibling modules can't be imported as a package without this --
# add the repo root once, before importing anything from ui.*.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import streamlit as st

from ui import state
from ui import strategy_import_state as import_state
from ui import strategy_set_state as ss_state
from ui.controls import render_scan_setup
from ui.results_view import render_results
from ui.scan_view import handle_run_scan, render_scan_error
from ui.strategy_set_scan_view import handle_run_strategy_set_scan

st.set_page_config(page_title="Oscill8 Scanner", layout="wide")

# Trading-terminal density pass: tighten default Streamlit spacing and
# restrain decoration (section 16/17 of the UI/UX spec). Presentation
# only -- no selector targets st.data_editor/st.dataframe internals, so
# the grid's rendered column geometry (and the keyboard-workflow
# Playwright test that measures it in pixels) is unaffected.
st.markdown(
    """
    <style>
    div.block-container {padding-top: 1.5rem; padding-bottom: 2rem;}
    div[data-testid="stVerticalBlockBorderWrapper"] {gap: 0.4rem;}
    hr {margin: 0.6rem 0;}
    </style>
    """,
    unsafe_allow_html=True,
)

state.init_state()
ss_state.init_state()
import_state.init_state()

setup = render_scan_setup()

if setup.run_clicked:
    handle_run_scan(setup)

if setup.strategy_set_scan_requested:
    handle_run_strategy_set_scan(setup)

render_scan_error()

report = state.get_scan_report()
display_lookback = state.get_display_lookback()
scan_request = state.get_scan_request()

if report is None:
    st.info("Configure a scan above and press **Run Scan**.")
else:
    render_results(report, display_lookback, scan_request)
