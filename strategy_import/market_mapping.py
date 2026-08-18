"""
market_mapping.py

Translates the short market codes traders use in their strategy
workbooks/CSVs (RIC-root-style codes: "SRA", "SON", "CRA", "ER") into
Oscill8's internal core.config.MARKETS registry keys ("SOFR", "SONIA",
"CORRA", ...).

This mapping is deliberately NOT the same thing as core.config.MARKETS
itself, and NOT the same thing as a MarketDefinition.ric_root lookup:
a workbook's "Market" column is the trader's own external vocabulary,
independent of Oscill8's internal registry key naming (see the
CLAUDE.md-documented distinction: "SRA"/"SON"/"CRA" are RIC roots,
"SOFR"/"SONIA"/"CORRA" are core.config.MARKETS dict keys). Keeping this
table here means the workbook never needs to be edited to match
Oscill8's internal naming, and a future registry-key rename only
touches this one file.

Three-way resolution, not two-way (valid/invalid): a market code from
a workbook can be
    1. supported      -- translates to a real, configured MarketDefinition
    2. unavailable     -- a real market Oscill8 recognizes by name, but
                           has no configured MarketDefinition for yet
                           (today: "ER" / Euribor -- see core/config.py's
                           Deferred list; bp_per_point/exchange must
                           never be guessed, so this is NOT solved by
                           adding a placeholder entry here)
    3. unrecognized    -- not a market code this importer knows about
                           at all (e.g. a typo)

"Unavailable" is a distinct, first-class outcome (not merged into
"unrecognized") specifically so an ER row is never silently dropped
and never reported as if it were a data-entry mistake -- see
strategy_import/validation.py, which routes each of these three
outcomes to a different result type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Trader-workbook code -> core.config.MARKETS registry key, for markets
# Oscill8 can actually price today. Deliberately explicit (not derived
# from MarketDefinition.ric_root) -- the workbook's vocabulary and
# Oscill8's RIC-root convention are allowed to diverge, and this table
# is the one place that seam is bridged.
SUPPORTED_MARKET_CODES: dict[str, str] = {
    "SRA": "SOFR",
    "SON": "SONIA",
    "CRA": "CORRA",
}

# Trader-workbook codes for markets Oscill8 recognizes by name but has
# no configured core.config.MARKETS entry for. Each value is the exact
# reason shown to the user -- never a guessed RIC root or bp_per_point,
# per core/config.py's explicit "must not be invented" note for
# EURIBOR. Adding a market here does NOT make it importable; it only
# changes how clearly its absence is reported.
UNAVAILABLE_MARKET_CODES: dict[str, str] = {
    "ER": "Euribor is not currently configured in Oscill8.",
}


@dataclass(frozen=True)
class MarketResolution:
    """The outcome of resolving one workbook "Market" cell.

    `code` preserves the original cell value (case-normalized) exactly
    as given, so it can be echoed back in preview/error messages without
    the caller needing to hold onto the raw cell separately.
    """

    status: Literal["supported", "unavailable", "unrecognized"]
    code: str
    market_key: str | None = None  # set only when status == "supported"
    reason: str | None = None  # set only when status == "unavailable"


def resolve_market_code(code: str) -> MarketResolution:
    """Resolve one workbook "Market" cell value into a MarketResolution.

    Matching is case-insensitive and strips surrounding whitespace (a
    trader-typed cell), but the returned `code` is upper-cased/stripped
    -- the canonical form used throughout strategy_import and shown in
    the preview -- not the raw, possibly differently-cased original.
    """
    normalized = (code or "").strip().upper()

    if normalized in SUPPORTED_MARKET_CODES:
        return MarketResolution(
            status="supported", code=normalized, market_key=SUPPORTED_MARKET_CODES[normalized]
        )
    if normalized in UNAVAILABLE_MARKET_CODES:
        return MarketResolution(
            status="unavailable", code=normalized, reason=UNAVAILABLE_MARKET_CODES[normalized]
        )
    return MarketResolution(status="unrecognized", code=normalized)


__all__ = [
    "SUPPORTED_MARKET_CODES",
    "UNAVAILABLE_MARKET_CODES",
    "MarketResolution",
    "resolve_market_code",
]
