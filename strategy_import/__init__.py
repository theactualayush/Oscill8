"""
strategy_import

Turns an uploaded Excel workbook or CSV file into ordinary
strategy_sets.StrategySet objects -- an import MECHANISM, not a
separate persisted model. One worksheet (Excel) or one file (CSV) is
exactly one Strategy Set (see strategy_import.parsing); interval is
never read from the file (see strategy_import.validation); a market
code (SRA/SON/CRA/ER, the trader's own RIC-root vocabulary) is resolved
to Oscill8's internal core.config.MARKETS registry key via
strategy_import.market_mapping, which also distinguishes a genuinely
UNAVAILABLE market (ER/Euribor -- recognized, not yet configured, never
guessed) from an UNRECOGNIZED one (a typo).

Pipeline, matching the product brief's parse -> normalize -> validate
-> preview -> import:

    strategy_import.parsing.parse_workbook()/parse_csv()
        -> list[SheetFrame]                        (pure, no I/O beyond the given bytes)
    strategy_import.validation.validate_row()
        -> ReadyRow | UnavailableRow | InvalidRow    (pure, per-row classification)
    strategy_import.preview.build_preview()
        -> ImportPreview                              (pure; only reads repo.exists()
                                                          for de-duplicated naming)
    strategy_import.commit.commit_import()
        -> ImportSummary                                (the ONLY write boundary --
                                                            called after user confirmation)

Nothing here modifies strategy_sets/, strategy_engine/, template_scanner/,
range_analytics/, or database/ -- an imported StrategySet is byte-for-
byte the same kind of object a hand-built one is, and behaves
identically afterward (select/edit/save/delete/run).
"""

from strategy_import.commit import ImportSummary, commit_import
from strategy_import.market_mapping import MarketResolution, resolve_market_code
from strategy_import.naming import unique_strategy_set_name
from strategy_import.parsing import SheetFrame, parse_csv, parse_workbook
from strategy_import.preview import ImportCandidate, ImportPreview, build_preview
from strategy_import.validation import (
    DEFAULT_IMPORT_INTERVAL,
    InvalidRow,
    ReadyRow,
    RowOutcome,
    UnavailableRow,
    validate_row,
)

__all__ = [
    "ImportSummary",
    "commit_import",
    "MarketResolution",
    "resolve_market_code",
    "unique_strategy_set_name",
    "SheetFrame",
    "parse_csv",
    "parse_workbook",
    "ImportCandidate",
    "ImportPreview",
    "build_preview",
    "DEFAULT_IMPORT_INTERVAL",
    "InvalidRow",
    "ReadyRow",
    "RowOutcome",
    "UnavailableRow",
    "validate_row",
]
