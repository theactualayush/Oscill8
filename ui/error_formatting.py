"""
error_formatting.py

Pure translation of a raised exception's identity (type name + message)
into a short, trader-facing error headline + description -- never a
Python traceback, LSEG error code, file path, or function name. The
full technical detail (exception type, message, traceback) is preserved
unmodified elsewhere (ui.scan_view stores it separately for the
"Technical details" expander); this module ONLY produces presentation
text and never changes what is caught, logged, or stored.

Classification is deliberately generic -- a case-insensitive keyword
match against the exception's own type name + message -- rather than
hard-coding any specific market's error (e.g. CORRA's current LSEG
entitlement gap, documented in CLAUDE.md). Recognized categories:
permission/entitlement, no-data/no-response, and connection/session.
Anything unrecognized falls back to one safe generic message. No
Streamlit import here -- unit-testable directly against plain strings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorPresentation:
    """A user-facing title + message pair. Never contains exception
    types, tracebacks, file paths, or vendor error codes."""

    title: str
    message: str


GENERIC_ERROR = ErrorPresentation(
    "⚠ The scan could not be completed",
    "Please check the technical details below.",
)

# Ordered (keywords, presentation) categories -- checked in order, first
# match wins. Keywords are matched case-insensitively against
# f"{exc_type_name} {exc_message}"; deliberately generic strings (no
# specific market name, no specific LSEG error code) so this never
# special-cases one market's current entitlement gap.
_CATEGORIES: tuple[tuple[tuple[str, ...], ErrorPresentation], ...] = (
    (
        ("usernotpermission", "permission", "entitlement"),
        ErrorPresentation(
            "⚠ Unable to fetch market data",
            "The selected market data is not available with the current data access.",
        ),
    ),
    (
        ("no data", "no successful response"),
        ErrorPresentation(
            "⚠ No market data available",
            "No data was returned for the selected market, contracts, or date range.",
        ),
    ),
    (
        ("connection", "session", "proxy", "timeout", "network"),
        ErrorPresentation(
            "⚠ Unable to connect to market data",
            "The market-data service could not be reached. Please try again.",
        ),
    ),
)


def classify_scan_error(exc_type_name: str, exc_message: str) -> ErrorPresentation:
    """Classify a failed scan's exception into a safe, trader-facing
    ErrorPresentation. Only ever reads `exc_type_name`/`exc_message` --
    never a traceback -- so nothing file-path- or line-number-shaped can
    leak into the returned title/message. Falls back to GENERIC_ERROR
    for anything that doesn't match a known category.
    """
    haystack = f"{exc_type_name} {exc_message}".lower()
    for keywords, presentation in _CATEGORIES:
        if any(keyword in haystack for keyword in keywords):
            return presentation
    return GENERIC_ERROR
