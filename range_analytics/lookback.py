"""
lookback.py

Selects which rows of a StrategyHistory.history frame Module 4A should
analyze. Deliberately kept independent of the diagnostic calculations
themselves (location.py, volatility.py, etc.), which operate on the
already-resolved Series this module hands them.
"""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from core.utils import DateLike, get_logger, to_date

logger = get_logger(__name__)


def _start_of_day(value: DateLike) -> datetime:
    return datetime.combine(to_date(value), time.min)


def _end_of_day(value: DateLike) -> datetime:
    return datetime.combine(to_date(value), time.max)


def resolve_window(
    history: pd.DataFrame,
    lookback: int | None = None,
    start: DateLike | None = None,
    end: DateLike | None = None,
) -> pd.DataFrame:
    """Select the Strategy-history rows Module 4A should analyze.

    Exactly one of `lookback` (last N valid observations) or
    `start`/`end` (calendar-date filter) may be given; both `None`
    selects the entire history. This mirrors strategy_engine keeping
    contract-selection and price-history windows independent --
    "how much history to analyze" stays independent of how that
    history was originally fetched.

    Rows with a NaN `Strategy` value are dropped before `lookback` is
    applied, so `lookback=N` always means N *valid* observations, not
    N rows.

    Duplicate `Date` values are treated as an upstream invariant
    violation -- StrategyHistory.history should already have unique
    dates from Module 3's inner-join alignment -- and raise ValueError
    rather than silently keeping one, since silently picking a row
    would mask a real bug elsewhere in the pipeline rather than
    surface it.

    Returns:
        A DataFrame with just `Date` and `Strategy` columns, sorted by
        Date, NaN-free, duplicate-free.

    Raises:
        ValueError: both `lookback` and `start`/`end` given; `lookback`
            is not >= 1; or `history` contains duplicate Date values.
    """
    if lookback is not None and (start is not None or end is not None):
        raise ValueError("Specify either lookback or start/end, not both")
    if lookback is not None and lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")

    df = history[["Date", "Strategy"]].copy()

    duplicated = df["Date"][df["Date"].duplicated(keep=False)]
    if not duplicated.empty:
        dupes = sorted(duplicated.unique())
        raise ValueError(
            f"StrategyHistory.history contains duplicate Date values: {dupes}"
        )

    df = df.dropna(subset=["Strategy"]).sort_values("Date").reset_index(drop=True)

    if lookback is not None:
        df = df.tail(lookback).reset_index(drop=True)
    elif start is not None or end is not None:
        if start is not None:
            df = df[df["Date"] >= pd.Timestamp(_start_of_day(start))]
        if end is not None:
            df = df[df["Date"] <= pd.Timestamp(_end_of_day(end))]
        df = df.reset_index(drop=True)

    logger.debug("resolve_window: %d row(s) selected", len(df))
    return df
