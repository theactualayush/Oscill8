"""
state.py

Session-state keys and accessor helpers for the things Module 6A/6B
must NOT recompute or re-fetch on every rerun: the expensive scan
result, and the selected candidate's raw price history (Module 6B's
chart data source). Filters, ranking controls, and the template grid
use Streamlit's own widget-key persistence and are left to Streamlit;
this module only guards state that must survive reruns unrelated to it
-- the ScanRequest/ScanReport pair, the display lookback, the selected
candidate (+ its rank), and the selected candidate's fetched history.

A failed scan's error is stored as an (ErrorPresentation, technical
detail string) pair -- see ui.error_formatting -- so ui.scan_view can
show a trader-friendly headline as the primary error and keep the raw
exception/traceback confined to a collapsed "Technical details" expander.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from template_scanner.scan_results import ScanCandidateResult
from template_scanner.scanner import ScanReport, ScanRequest

from ui.error_formatting import ErrorPresentation

SCAN_REQUEST = "oscill8_scan_request"
SCAN_REPORT = "oscill8_scan_report"
DISPLAY_LOOKBACK = "oscill8_display_lookback_used"
SELECTED_CANDIDATE = "oscill8_selected_candidate"
SELECTED_RANK = "oscill8_selected_rank"
SELECTED_HISTORY = "oscill8_selected_history"
SCAN_ERROR = "oscill8_scan_error"  # (ErrorPresentation, technical_detail_str) | None


def init_state() -> None:
    """Seed every key this module owns, once per session."""
    st.session_state.setdefault(SCAN_REQUEST, None)
    st.session_state.setdefault(SCAN_REPORT, None)
    st.session_state.setdefault(DISPLAY_LOOKBACK, None)
    st.session_state.setdefault(SELECTED_CANDIDATE, None)
    st.session_state.setdefault(SELECTED_RANK, None)
    st.session_state.setdefault(SELECTED_HISTORY, None)
    st.session_state.setdefault(SCAN_ERROR, None)


def store_scan_result(request: ScanRequest, report: ScanReport, display_lookback: int) -> None:
    """Record a freshly completed scan. Clears any prior selection (it
    referred to the previous report's candidates) and any prior error."""
    st.session_state[SCAN_REQUEST] = request
    st.session_state[SCAN_REPORT] = report
    st.session_state[DISPLAY_LOOKBACK] = display_lookback
    st.session_state[SELECTED_CANDIDATE] = None
    st.session_state[SELECTED_RANK] = None
    st.session_state[SELECTED_HISTORY] = None
    st.session_state[SCAN_ERROR] = None


def store_scan_error(error: tuple[ErrorPresentation, str] | None) -> None:
    """Record a failed scan's presentation (friendly title/message) and
    technical detail (exception type/message + traceback) together --
    ui.scan_view.render_scan_error() shows the former as the primary
    error and the latter only inside a collapsed "Technical details"
    expander. Pass None to clear (e.g. before validating a new Run Scan
    press)."""
    st.session_state[SCAN_ERROR] = error


def get_scan_error() -> tuple[ErrorPresentation, str] | None:
    return st.session_state.get(SCAN_ERROR)


def get_scan_request() -> ScanRequest | None:
    return st.session_state.get(SCAN_REQUEST)


def get_scan_report() -> ScanReport | None:
    return st.session_state.get(SCAN_REPORT)


def get_display_lookback() -> int | None:
    return st.session_state.get(DISPLAY_LOOKBACK)


def set_selected_candidate(candidate: ScanCandidateResult | None, rank: int | None) -> None:
    """Record the selected candidate and its rank. If the selection
    actually changed identity, the cached history (Module 6B's chart
    data source, see get_cached_history/cache_history below) is
    invalidated -- a re-selection of the SAME candidate on a later
    rerun (e.g. from an unrelated filter tweak) must not re-trigger a
    build_history() call.
    """
    if candidate is not st.session_state.get(SELECTED_CANDIDATE):
        st.session_state[SELECTED_HISTORY] = None
    st.session_state[SELECTED_CANDIDATE] = candidate
    st.session_state[SELECTED_RANK] = rank


def get_selected_candidate() -> ScanCandidateResult | None:
    return st.session_state.get(SELECTED_CANDIDATE)


def get_selected_rank() -> int | None:
    return st.session_state.get(SELECTED_RANK)


def get_cached_history() -> pd.DataFrame | None:
    return st.session_state.get(SELECTED_HISTORY)


def cache_history(history: pd.DataFrame) -> None:
    st.session_state[SELECTED_HISTORY] = history
