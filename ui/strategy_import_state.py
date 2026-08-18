"""
strategy_import_state.py

Session-state keys for the Import Strategies panel (ui.
strategy_import_view) -- whether the panel is open, the currently
built (pure, in-memory) ImportPreview, which uploaded file it was built
from (so a rerun unrelated to the uploader doesn't re-parse the same
file), and the one-shot post-import summary. Mirrors ui.strategy_set_
state's convention: this module owns keys/accessors only, never widget
rendering.

Nothing here ever touches StrategySetRepository -- only ui.
strategy_import_view.render_import_panel() calls strategy_import.
commit.commit_import(), and only on an explicit Import click.
"""

from __future__ import annotations

import streamlit as st

from strategy_import.commit import ImportSummary
from strategy_import.preview import ImportPreview

SHOW_PANEL = "oscill8_import_show_panel"
PREVIEW = "oscill8_import_preview"  # ImportPreview | None -- pure, never written to disk
FILE_SIGNATURE = "oscill8_import_file_signature"  # (name, size) | None -- detects a new upload
PARSE_ERROR = "oscill8_import_parse_error"  # str | None -- whole-file unreadable (corrupt upload)
SUMMARY = "oscill8_import_summary"  # ImportSummary | None -- shown once after a successful Import


def init_state() -> None:
    st.session_state.setdefault(SHOW_PANEL, False)
    st.session_state.setdefault(PREVIEW, None)
    st.session_state.setdefault(FILE_SIGNATURE, None)
    st.session_state.setdefault(PARSE_ERROR, None)
    st.session_state.setdefault(SUMMARY, None)


def is_panel_open() -> bool:
    return bool(st.session_state.get(SHOW_PANEL, False))


def open_panel() -> None:
    st.session_state[SHOW_PANEL] = True


def close_panel() -> None:
    """Closes the panel and discards its in-memory preview/upload state
    -- Cancel never wrote anything, so there is nothing to undo, only
    state to forget."""
    st.session_state[SHOW_PANEL] = False
    st.session_state[PREVIEW] = None
    st.session_state[FILE_SIGNATURE] = None
    st.session_state[PARSE_ERROR] = None


def get_preview() -> ImportPreview | None:
    return st.session_state.get(PREVIEW)


def set_preview(preview: ImportPreview | None) -> None:
    st.session_state[PREVIEW] = preview


def get_file_signature() -> tuple | None:
    return st.session_state.get(FILE_SIGNATURE)


def set_file_signature(signature: tuple | None) -> None:
    st.session_state[FILE_SIGNATURE] = signature


def get_parse_error() -> str | None:
    return st.session_state.get(PARSE_ERROR)


def set_parse_error(message: str | None) -> None:
    st.session_state[PARSE_ERROR] = message


def set_summary(summary: ImportSummary | None) -> None:
    st.session_state[SUMMARY] = summary


def pop_summary() -> ImportSummary | None:
    summary = st.session_state.get(SUMMARY)
    st.session_state[SUMMARY] = None
    return summary


__all__ = [
    "init_state",
    "is_panel_open",
    "open_panel",
    "close_panel",
    "get_preview",
    "set_preview",
    "get_file_signature",
    "set_file_signature",
    "get_parse_error",
    "set_parse_error",
    "set_summary",
    "pop_summary",
]
