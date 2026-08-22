"""
strategy_import_view.py

"Import Strategies": upload an .xlsx workbook (one worksheet = one
Strategy Set) or a .csv file (one file = one Strategy Set), preview
what would be imported, and only write to StrategySetRepository on an
explicit "Import All" click. Every write goes through the existing,
unmodified strategy_sets.repository.StrategySetRepository.save() --
there is no separate "imported Strategy Set" model; a saved imported
set is byte-for-byte the same object a hand-built one is, and is
immediately visible to and usable by ui.strategy_set_view's selector
(loading it into the grid, which is what ui.scan_view.handle_run_scan()
-- the single Run Scan path -- actually scans, see that module's
docstring), unaware of how a given file got there.

parse -> preview -> import, all client-side of this module:
    strategy_import.parsing.parse_csv()/parse_workbook()  -- pure, no writes
    strategy_import.preview.build_preview()                -- pure, no writes
        (build_preview() DOES call repo.exists() to compute a
        de-duplicated import name -- a read, never a write)
    strategy_import.commit.commit_import()                  -- the ONLY write,
        called exclusively from the Import All button below

No interval is ever read from the uploaded file, and none is ever
requested from the user at import time -- every imported entry keeps
strategy_import.validation.DEFAULT_IMPORT_INTERVAL (a required
placeholder value; StrategyDefinition.interval has no optional/None
state in the existing, unmodified schema) purely so it can be
constructed at all. The interval a trader actually cares about is
chosen at RUN time via Scan Configuration's own Interval selector,
which ui.formatting.apply_interval_override() applies to every leg of
every scan regardless of what's stored -- so this placeholder is inert
for that workflow. It only becomes visible/relevant as the grid's own
per-row Interval cell (still shown/edited, still what gets saved) once
an imported set is loaded, where it behaves exactly like any
freshly-typed row's Interval cell: editable, not authoritative.
"""

from __future__ import annotations

import streamlit as st

from strategy_import.commit import commit_import
from strategy_import.parsing import parse_csv, parse_workbook
from strategy_import.preview import ImportCandidate, ImportPreview, build_preview
from strategy_sets.repository import StrategySetRepository

from ui import strategy_import_state as import_state
from ui import strategy_set_state as ss_state
from ui.strategy_import_formatting import (
    candidate_summary_line,
    import_summary_lines,
    invalid_row_lines,
    market_breakdown_lines,
    unavailable_row_lines,
)


def render_import_button() -> None:
    """Opens the Import Strategies panel. A plain flag flip -- the panel
    itself renders later in the same script pass via
    render_import_panel(), so no st.rerun() is needed here."""
    if st.button("📥 Import", key="oscill8_import_open_button", width="stretch"):
        import_state.open_panel()


def _build_preview_for_upload(uploaded, repo: StrategySetRepository) -> ImportPreview:
    file_bytes = uploaded.getvalue()
    if uploaded.name.lower().endswith(".csv"):
        sheets = [parse_csv(file_bytes, uploaded.name)]
    else:
        sheets = parse_workbook(file_bytes)
    return build_preview(sheets, repo.exists)


def _handle_upload(repo: StrategySetRepository) -> None:
    uploaded = st.file_uploader(
        "Upload a Strategy Set workbook (.xlsx, one worksheet per Strategy Set) "
        "or a single Strategy Set (.csv)",
        type=["xlsx", "csv"],
        key="oscill8_import_uploader",
    )
    if uploaded is None:
        return

    signature = (uploaded.name, uploaded.size)
    if signature == import_state.get_file_signature():
        return  # same file as the last rerun -- don't re-parse/rebuild on every widget interaction

    import_state.set_file_signature(signature)
    try:
        preview = _build_preview_for_upload(uploaded, repo)
    except ValueError as exc:
        # No traceback, no exception type -- strategy_import.parsing's
        # own ValueError messages are already trader-facing.
        import_state.set_parse_error(str(exc))
        import_state.set_preview(None)
        return

    import_state.set_parse_error(None)
    import_state.set_preview(preview)


def _render_preview(preview: ImportPreview) -> None:
    st.markdown(
        f"**{len(preview.candidates)} Strategy Set(s) detected · "
        f"{preview.total_strategies} strategies detected**"
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total", preview.total_strategies)
    m2.metric("Ready", preview.ready_count)
    m3.metric("Unavailable", preview.unavailable_count)
    m4.metric("Invalid", preview.invalid_count)

    for line in market_breakdown_lines(preview):
        st.caption(line)

    for candidate in preview.candidates:
        _render_candidate(candidate)


def _render_candidate(candidate: ImportCandidate) -> None:
    if candidate.sheet_error is not None:
        st.warning(f"**{candidate.sheet_name}** — not imported: {candidate.sheet_error}")
        return

    icon = "✓" if candidate.importable else "⚠"
    target = f" → *{candidate.import_name}*" if candidate.import_name else ""
    st.markdown(f"{icon} **{candidate.sheet_name}**{target}")
    st.caption(candidate_summary_line(candidate))

    if candidate.invalid:
        with st.expander(f"⚠ {len(candidate.invalid)} invalid row(s) — will not be imported"):
            for line in invalid_row_lines(candidate):
                st.caption(line)

    if candidate.unavailable:
        with st.expander(f"⚠ {len(candidate.unavailable)} unavailable row(s) — will not be imported"):
            for line in unavailable_row_lines(candidate):
                st.caption(line)


def _render_summary(summary) -> None:
    lines = import_summary_lines(summary)
    st.success("  ·  ".join(lines))
    if summary.created_set_names:
        st.caption("Created: " + ", ".join(summary.created_set_names))


def render_import_panel(repo: StrategySetRepository) -> None:
    """The full Import Strategies panel: upload, preview, Cancel/Import
    All, and the post-import summary. The summary is rendered
    regardless of whether the panel is currently open (Import closes
    the panel immediately, but the confirmation must still show)."""
    summary = import_state.pop_summary()
    if summary is not None:
        _render_summary(summary)

    if not import_state.is_panel_open():
        return

    with st.container(border=True):
        st.caption("IMPORT STRATEGIES")
        _handle_upload(repo)

        parse_error = import_state.get_parse_error()
        if parse_error is not None:
            st.error(parse_error)

        preview = import_state.get_preview()
        if preview is not None:
            _render_preview(preview)

            col_cancel, col_import = st.columns(2)
            with col_cancel:
                if st.button("Cancel", key="oscill8_import_cancel_button", width="stretch"):
                    import_state.close_panel()
                    st.rerun()
            with col_import:
                importable = preview.importable_candidates
                if st.button(
                    "Import All", key="oscill8_import_confirm_button", type="primary",
                    width="stretch", disabled=not importable,
                ):
                    result = commit_import(preview, repo)
                    import_state.close_panel()
                    import_state.set_summary(result)
                    if result.created_set_names:
                        ss_state.set_pending_selection(result.created_set_names[0])
                    st.rerun()
        else:
            if st.button("Cancel", key="oscill8_import_cancel_no_file_button", width="stretch"):
                import_state.close_panel()
                st.rerun()


__all__ = ["render_import_button", "render_import_panel"]
