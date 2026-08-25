"""
utils.py

Small, generic helpers shared across modules (logging setup, date
coercion, etc.). Nothing here should import from any other project
module, to keep this safe to import from anywhere.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from typing import Union

import pandas as pd

from core import config

DateLike = Union[str, date, datetime]

# Canonical OHLCV schema every market-data provider (core.downloader for
# LSEG, core.quanthub for QuantHub) normalizes its response into. Shared
# here so both providers -- and resample_to_4h below -- agree on exactly
# one column set/order, never two independently-maintained copies.
CANONICAL_OHLCV_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger that writes to both console and file.

    Safe to call repeatedly (e.g. once per module via
    `logger = get_logger(__name__)`) -- handlers are only attached once
    per logger name.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(config.LOG_LEVEL)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(config.LOG_FILE)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def to_date(value: DateLike) -> date:
    """Coerce a str / date / datetime into a plain `date`.

    Accepts ISO format strings ("2026-07-31").
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    raise TypeError(f"Cannot coerce {type(value)} to date")


def missing_business_days(dates: pd.Series) -> list[pd.Timestamp]:
    """Weekdays (Mon-Fri) within [dates.min(), dates.max()] with no bar
    anywhere in `dates` -- i.e. holidays, market closures, or a genuine
    vendor data gap. Deliberately excludes Saturday/Sunday: a market-
    closure signal only makes sense on days the market could plausibly
    have traded.

    Mirrors the exact same 'valid-observation' concept already
    established for chart rendering (ui.chart_view._missing_weekdays,
    DAILY-only there) -- duplicated here rather than imported, since a
    lower layer (core/, and database/service.py which uses this) must
    never depend on ui/. Generalizes cleanly to HOURLY/4H too: `dates`
    is normalized to midnight before comparison, so a date with a bar
    at ANY time of day counts as present -- this checks "does this
    weekday have any bar at all", not "does every expected intraday
    timestamp have one" (Oscill8 has no per-market trading-session
    calendar to check the latter against, and none is invented here).

    Returns [] for an empty input.
    """
    if dates.empty:
        return []
    normalized = pd.DatetimeIndex(dates).normalize()
    full_range = pd.date_range(normalized.min(), normalized.max(), freq="D")
    present = set(normalized)
    return [d for d in full_range if d.weekday() < 5 and d not in present]


def longest_missing_business_day_run(dates: pd.Series) -> int:
    """Longest run of CONSECUTIVE missing business days within `dates`
    (see missing_business_days) -- used to distinguish a normal holiday
    cluster (a short run) from a genuine data gap (a long one) without
    assuming a fixed number of calendar days must always contain a bar.

    A weekend (Friday -> Monday, 3 calendar days) between two missing
    weekdays does NOT break the run: weekends are never themselves
    "missing" (missing_business_days excludes them entirely by
    definition), so two missing weekdays separated only by a weekend
    represent one unbroken stretch of missing trading days, not two
    separate gaps -- e.g. a whole month of missing weekdays must not be
    fragmented into a sequence of isolated 5-day runs just because each
    week's weekend sits between them.

    Returns 0 if no business day is missing.
    """
    missing = missing_business_days(dates)
    if not missing:
        return 0
    longest = 1
    current = 1
    for prev, curr in zip(missing, missing[1:]):
        if (curr - prev).days <= 3:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def resample_to_4h(df: pd.DataFrame, resample_rule: str) -> pd.DataFrame:
    """Aggregate hourly OHLCV bars into 4-hour bars.

    Standard OHLCV resampling rules:
        Open   = first
        High   = max
        Low    = min
        Close  = last
        Volume = sum

    Shared by every provider that needs to synthesize 4H bars from native
    hourly data (core.downloader for LSEG, core.quanthub for QuantHub) --
    the one place this aggregation is implemented, per CLAUDE.md's "reuse
    the existing Oscill8 resampling implementation rather than creating a
    second aggregation implementation" rule.

    Args:
        df: canonical OHLCV DataFrame (CANONICAL_OHLCV_COLUMNS) at native
            hourly granularity.
        resample_rule: pandas resample rule string, e.g. "4h".
    """
    if df.empty:
        return df

    indexed = df.set_index("Date")
    agg = indexed.resample(resample_rule).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    agg = agg.dropna(subset=["Open", "High", "Low", "Close"], how="all")
    return agg.reset_index()[CANONICAL_OHLCV_COLUMNS]
