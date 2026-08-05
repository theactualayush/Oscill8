"""
results_view.py

Section E (Range-Bound Filters), Section F (Ranking), and Section G
(Result Grid + selection). Operates entirely on a ScanReport already in
session state -- never calls run_scan()/build_history()/get_history.
Filtering and ranking read directly off the ALREADY-COMPUTED Module 4
analytics and recompute on every rerun (Section H): only the scan itself
is expensive and gated behind Run Scan in scan_view.py.

Pipeline order matters (Section G): filter and rank the Python
ScanCandidateResult objects FIRST, then build the DataFrame, so row
position in the displayed grid maps back to the same-position entry in
the ranked candidate list.
"""

from __future__ import annotations

import streamlit as st

from template_scanner.filters import apply_filters
from template_scanner.ranking import rank_results
from template_scanner.scan_results import results_to_dataframe
from template_scanner.scanner import ScanReport

from ui import state
from ui.formatting import (
    ALL_FILTER_SPECS,
    NO_SECONDARY_RANK,
    RANK_METRIC_OPTIONS,
    build_filter_criteria,
    build_sort_keys,
    to_display_dataframe,
)


def render_filters() -> dict[str, dict]:
    st.subheader("Range-Bound Filters")
    filter_state: dict[str, dict] = {}
    for spec in ALL_FILTER_SPECS:
        col_enable, col_value = st.columns([2, 1])
        with col_enable:
            enabled = st.checkbox(spec.label, key=f"oscill8_filter_enabled_{spec.key}")
        with col_value:
            value = st.number_input(
                spec.label,
                key=f"oscill8_filter_value_{spec.key}",
                value=0.0,
                disabled=not enabled,
                label_visibility="collapsed",
            )
        filter_state[spec.key] = {"enabled": enabled, "value": value if enabled else None}
    return filter_state


def render_ranking() -> dict:
    st.subheader("Ranking")
    labels = [label for label, _ in RANK_METRIC_OPTIONS]
    field_by_label = dict(RANK_METRIC_OPTIONS)

    col1, col2 = st.columns(2)
    with col1:
        primary_label = st.selectbox("Primary metric", labels, key="oscill8_rank_primary")
        primary_ascending = (
            st.radio(
                "Primary direction",
                ["Ascending", "Descending"],
                key="oscill8_rank_primary_dir",
                horizontal=True,
            )
            == "Ascending"
        )
    with col2:
        secondary_label = st.selectbox(
            "Secondary metric", [NO_SECONDARY_RANK] + labels, key="oscill8_rank_secondary"
        )
        secondary_ascending = (
            st.radio(
                "Secondary direction",
                ["Ascending", "Descending"],
                key="oscill8_rank_secondary_dir",
                horizontal=True,
            )
            == "Ascending"
        )

    return {
        "primary_field": field_by_label[primary_label],
        "primary_ascending": primary_ascending,
        "secondary_field": field_by_label.get(secondary_label),
        "secondary_ascending": secondary_ascending,
    }


def render_results(
    report: ScanReport,
    display_lookback: int,
    filter_state: dict[str, dict],
    rank_state: dict,
) -> None:
    st.subheader("Results")

    if not report.results:
        st.info("No candidates were successfully analyzed for this scan.")
        return

    criteria = build_filter_criteria(filter_state, display_lookback)
    filtered = apply_filters(report.results, criteria)

    if not filtered:
        st.info(f"{len(report.results)} candidate(s) analyzed — none passed the selected filters.")
        return

    sort_keys = build_sort_keys(
        rank_state["primary_field"],
        rank_state["primary_ascending"],
        rank_state["secondary_field"],
        rank_state["secondary_ascending"],
        display_lookback,
    )
    ranked = rank_results(filtered, sort_keys)

    results_df = results_to_dataframe(ranked, display_lookback)
    display_df = to_display_dataframe(results_df)

    event = st.dataframe(
        display_df,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        key="oscill8_result_grid",
    )

    selected_rows = list(event.selection.rows) if event is not None else []
    state.set_selected_candidate(ranked[selected_rows[0]] if selected_rows else None)

    render_selection()


def render_selection() -> None:
    candidate = state.get_selected_candidate()
    if candidate is None:
        st.caption("Select a row above to inspect a strategy.")
        return
    rics = " / ".join(candidate.rics)
    st.success(f"Selected: {rics}")
