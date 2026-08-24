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
