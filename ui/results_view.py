"""
results_view.py

The dominant post-scan section: "Range-Bound Opportunities" (status +
ranking/filters + the ranked result grid), and the Selected Strategy
panel (summary + Module 6B history chart) -- all operating purely on a
ScanReport already in session state. Never calls run_scan()/
build_history()/get_history() for the scan itself. Filtering and
ranking read directly off the ALREADY-COMPUTED Module 4 analytics and
recompute on every rerun: only the scan itself is expensive and gated
behind Run Scan in ui.scan_view. (The Selected Strategy chart's own,
separate history fetch is a cache-only call -- see ui.chart_view.)

Pipeline order matters: filter and rank the Python ScanCandidateResult
objects FIRST, then build the DataFrame, so row position in the
displayed grid maps back to the same-position entry in the ranked
candidate list -- the Rank column and the click-to-select mapping both
depend on this order being preserved end to end.
"""

from __future__ import annotations

import streamlit as st

from template_scanner.filters import apply_filters
from template_scanner.ranking import rank_results
from template_scanner.scan_results import results_to_dataframe
from template_scanner.scanner import ScanReport, ScanRequest

from ui import state
from ui.chart_view import render_chart
from ui.formatting import (
    ALL_FILTER_SPECS,
    NO_SECONDARY_RANK,
    RANK_METRIC_OPTIONS,
    RESULT_COLUMN_HELP,
    add_rank_column,
    build_filter_criteria,
    build_sort_keys,
    format_ranked_by,
    selected_strategy_summary,
    to_display_dataframe,
)

_TABLE_HEIGHT = 460


def _current_rank_state() -> dict:
    """Read the Ranking popover's persisted widget values -- or its own
    defaults, on the very first render before those keys exist -- so the
    "Ranked by:" label can render ABOVE the popover trigger in the same
    script pass, before the widgets themselves are (re)created below it.
    Both reads describe the same state: nothing changes between this
    read and the widgets' own creation later in the same rerun.
    """
    labels = [label for label, _ in RANK_METRIC_OPTIONS]
    field_by_label = dict(RANK_METRIC_OPTIONS)
    primary_label = st.session_state.get("oscill8_rank_primary", labels[0])
    primary_ascending = st.session_state.get("oscill8_rank_primary_dir", "Ascending") == "Ascending"
    secondary_label = st.session_state.get("oscill8_rank_secondary", NO_SECONDARY_RANK)
    secondary_ascending = st.session_state.get("oscill8_rank_secondary_dir", "Ascending") == "Ascending"
    return {
        "primary_field": field_by_label.get(primary_label, RANK_METRIC_OPTIONS[0][1]),
        "primary_ascending": primary_ascending,
        "secondary_field": field_by_label.get(secondary_label),
        "secondary_ascending": secondary_ascending,
    }


def _render_ranking_popover() -> dict:
    labels = [label for label, _ in RANK_METRIC_OPTIONS]
    field_by_label = dict(RANK_METRIC_OPTIONS)

    with st.popover("Ranking ▾"):
        st.caption("Primary")
        primary_label = st.selectbox("Metric", labels, key="oscill8_rank_primary")
        primary_ascending = (
            st.radio("Direction", ["Ascending", "Descending"], key="oscill8_rank_primary_dir", horizontal=True)
            == "Ascending"
        )
        st.caption("Secondary (optional)")
        secondary_label = st.selectbox(
            "Metric ", [NO_SECONDARY_RANK] + labels, key="oscill8_rank_secondary"
        )
        secondary_ascending = (
            st.radio(
                "Direction ", ["Ascending", "Descending"], key="oscill8_rank_secondary_dir", horizontal=True
            )
            == "Ascending"
        )

    return {
        "primary_field": field_by_label[primary_label],
        "primary_ascending": primary_ascending,
        "secondary_field": field_by_label.get(secondary_label),
        "secondary_ascending": secondary_ascending,
    }


def _render_filters_popover() -> dict[str, dict]:
    filter_state: dict[str, dict] = {}
    with st.popover("Filters ▾"):
        for spec in ALL_FILTER_SPECS:
            col_enable, col_value = st.columns([2, 1])
            with col_enable:
                enabled = st.checkbox(
                    spec.label, key=f"oscill8_filter_enabled_{spec.key}", help=spec.help_text
                )
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


def _render_skipped_expander(report: ScanReport) -> None:
    if not report.skipped:
        return
    with st.expander(f"Skipped candidates ({len(report.skipped)})"):
        for item in report.skipped:
            rics = " / ".join(item.instance.rics)
            st.write(f"**{rics}** — unavailable: `{item.unavailable_ric}` — {item.message}")


def render_results(report: ScanReport, display_lookback: int, scan_request: ScanRequest | None) -> None:
    analyzed = len(report.results)
    skipped = len(report.skipped)

    with st.container(border=True):
        if not report.results:
            st.subheader("Range-Bound Opportunities")
            st.caption(f"{analyzed} analyzed · {skipped} skipped · 0 shown")
            st.info("No candidates were successfully analyzed for this scan.")
            _render_skipped_expander(report)
            return

        left, right = st.columns([2, 1])
        with right:
            st.caption(format_ranked_by(_current_rank_state()))
            ranking_col, filters_col = st.columns(2)
            with ranking_col:
                rank_state = _render_ranking_popover()
            with filters_col:
                filter_state = _render_filters_popover()

        criteria = build_filter_criteria(filter_state, display_lookback)
        filtered = apply_filters(report.results, criteria)

        with left:
            st.subheader("Range-Bound Opportunities")
            st.caption(f"{analyzed} analyzed · {skipped} skipped · {len(filtered)} shown")

        if not filtered:
            st.info(f"{analyzed} candidate(s) analyzed — none passed the selected filters.")
            _render_skipped_expander(report)
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
        display_df = add_rank_column(to_display_dataframe(results_df))

        column_config = {
            label: st.column_config.Column(label, help=help_text)
            for label, help_text in RESULT_COLUMN_HELP.items()
        }

        event = st.dataframe(
            display_df,
            hide_index=True,
            height=_TABLE_HEIGHT,
            on_select="rerun",
            selection_mode="single-row",
            column_config=column_config,
            key="oscill8_result_grid",
        )

        selected_rows = list(event.selection.rows) if event is not None else []
        selected_candidate = ranked[selected_rows[0]] if selected_rows else None
        selected_rank = selected_rows[0] + 1 if selected_rows else None
        state.set_selected_candidate(selected_candidate, selected_rank)
        st.caption("Click a row's checkbox to select it.")

        _render_skipped_expander(report)

    render_selection_and_chart(display_lookback, scan_request)


def render_selection_and_chart(display_lookback: int, scan_request: ScanRequest | None) -> None:
    candidate = state.get_selected_candidate()
    if candidate is None:
        return

    rank = state.get_selected_rank()
    summary = selected_strategy_summary(candidate, display_lookback)

    with st.container(border=True):
        st.subheader("Selected Strategy")
        rank_prefix = f"#{rank}  " if rank is not None else ""
        st.write(f"**{rank_prefix}{summary['rics']}**")
        st.caption(f"{summary['weights']} · {summary['interval']} · Robust Range · {summary['percentile_range_label']}")

        labels = [
            "Current", "Mean", "Median", "Robust Low", "Robust High", "Position",
            "Z-Score", "ER", "Movement (bp)", "Oscillations",
        ]
        values = [
            summary["current"],
            summary["mean"],
            summary["median"],
            summary["robust_low"],
            summary["robust_high"],
            summary["position"],
            summary["z_score"],
            summary["efficiency_ratio"],
            summary["movement"],
            summary["oscillations"],
        ]
        for col, label, value in zip(st.columns(len(labels)), labels, values):
            with col:
                st.caption(label)
                st.write(f"**{value}**")

        render_chart(display_lookback, scan_request)
