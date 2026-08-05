"""
app.py

Module 6A entry point: Scan Setup + Template Grid -> Run Scan -> Skipped
Candidates -> Range-Bound Filters + Ranking -> Result Grid -> Select
Strategy, per the Module 6A workflow. Thin orchestration only -- every
analytics, filtering, and ranking computation is delegated to
strategy_engine / range_analytics / template_scanner, unmodified.

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
from ui.controls import render_scan_setup
from ui.results_view import render_filters, render_ranking, render_results
from ui.scan_view import handle_run_scan, render_scan_error, render_skipped_section

st.set_page_config(page_title="Oscill8 Scanner", layout="wide")
state.init_state()

st.title("Oscill8 — Range-Bound Strategy Scanner")

setup = render_scan_setup()

if st.button("Run Scan", type="primary"):
    handle_run_scan(setup)

render_scan_error()

report = state.get_scan_report()
display_lookback = state.get_display_lookback()

if report is None:
    st.info("Configure a scan above and press **Run Scan**.")
else:
    render_skipped_section(report)

    left, right = st.columns([1, 2])
    with left:
        filter_state = render_filters()
        rank_state = render_ranking()
    with right:
        render_results(report, display_lookback, filter_state, rank_state)
