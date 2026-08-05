"""
state.py

Session-state keys and accessor helpers for the one thing Module 6A
must NOT recompute on every rerun: the expensive scan result. Filters,
ranking controls, and the template grid use Streamlit's own widget-key
persistence and are left to Streamlit; this module only guards the
ScanRequest/ScanReport pair (and the display lookback + selection
derived from it) that must survive until the user explicitly presses
Run Scan again.
"""

from __future__ import annotations

import streamlit as st

from template_scanner.scan_results import ScanCandidateResult
from template_scanner.scanner import ScanReport, ScanRequest

SCAN_REQUEST = "oscill8_scan_request"
SCAN_REPORT = "oscill8_scan_report"
DISPLAY_LOOKBACK = "oscill8_display_lookback_used"
SELECTED_CANDIDATE = "oscill8_selected_candidate"
SCAN_ERROR = "oscill8_scan_error"


def init_state() -> None:
    """Seed every key this module owns, once per session."""
    st.session_state.setdefault(SCAN_REQUEST, None)
    st.session_state.setdefault(SCAN_REPORT, None)
    st.session_state.setdefault(DISPLAY_LOOKBACK, None)
    st.session_state.setdefault(SELECTED_CANDIDATE, None)
    st.session_state.setdefault(SCAN_ERROR, None)


def store_scan_result(request: ScanRequest, report: ScanReport, display_lookback: int) -> None:
    """Record a freshly completed scan. Clears any prior selection (it
    referred to the previous report's candidates) and any prior error."""
    st.session_state[SCAN_REQUEST] = request
    st.session_state[SCAN_REPORT] = report
    st.session_state[DISPLAY_LOOKBACK] = display_lookback
    st.session_state[SELECTED_CANDIDATE] = None
    st.session_state[SCAN_ERROR] = None


def store_scan_error(message: str | None) -> None:
    st.session_state[SCAN_ERROR] = message


def get_scan_request() -> ScanRequest | None:
    return st.session_state.get(SCAN_REQUEST)


def get_scan_report() -> ScanReport | None:
    return st.session_state.get(SCAN_REPORT)


def get_display_lookback() -> int | None:
    return st.session_state.get(DISPLAY_LOOKBACK)


def set_selected_candidate(candidate: ScanCandidateResult | None) -> None:
    st.session_state[SELECTED_CANDIDATE] = candidate


def get_selected_candidate() -> ScanCandidateResult | None:
    return st.session_state.get(SELECTED_CANDIDATE)
