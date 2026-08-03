"""
cache.py

Low-level, DB-facing read/write access to the price-bar and sync-range
tables. DataFrame-in/DataFrame-out at the boundary, no LSEG knowledge --
service.py is the only module that talks to core.downloader.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.config import BarInterval
from core.utils import get_logger

from database.models import PriceBar, SyncRange

logger = get_logger(__name__)

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]

# Nominal spacing between consecutive bars for each interval. Used by
# record_sync_range to decide whether two coverage windows are close
# enough together that no un-fetched bar could exist in the gap.
_BAR_DELTAS: dict[str, timedelta] = {
    BarInterval.DAILY.value: timedelta(days=1),
    BarInterval.HOURLY.value: timedelta(hours=1),
    BarInterval.FOUR_HOUR.value: timedelta(hours=4),
}


def bar_delta(interval: str) -> timedelta:
    """Return the nominal bar spacing for an interval (BarInterval.value)."""
    try:
        return _BAR_DELTAS[interval]
    except KeyError as exc:
        raise ValueError(f"Unknown interval '{interval}'") from exc


def _upsert_statement(engine: Engine, records: list[dict]):
    """Build a dialect-appropriate "insert, skip duplicates" statement.

    Isolates the ON CONFLICT DO NOTHING syntax behind one function so a
    future PostgreSQL migration only needs to change this function --
    sqlalchemy.dialects.postgresql.insert exposes the identical
    on_conflict_do_nothing(index_elements=...) call signature.
    """
    if engine.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    elif engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        raise NotImplementedError(f"No upsert strategy for dialect '{engine.dialect.name}'")

    stmt = dialect_insert(PriceBar).values(records)
    return stmt.on_conflict_do_nothing(index_elements=["ric", "interval", "datetime"])


def insert_bars(session: Session, ric: str, interval: str, df: pd.DataFrame) -> int:
    """Upsert canonical-schema OHLCV bars into price_bars.

    df must have columns [Date, Open, High, Low, Close, Volume] (the same
    schema core.downloader.download_history returns). Rows whose
    (ric, interval, datetime) already exist are silently skipped at the
    database level -- safe to call with overlapping/already-cached data.

    Returns the number of NEW rows actually inserted.
    """
    if df.empty:
        return 0

    requested_dts = [pd.Timestamp(d).to_pydatetime() for d in df["Date"]]
    existing = session.execute(
        select(PriceBar.datetime).where(
            PriceBar.ric == ric,
            PriceBar.interval == interval,
            PriceBar.datetime.in_(requested_dts),
        )
    ).scalars().all()
    existing_set = set(existing)

    records = [
        {
            "ric": ric,
            "interval": interval,
            "datetime": pd.Timestamp(row.Date).to_pydatetime(),
            "open": float(row.Open),
            "high": float(row.High),
            "low": float(row.Low),
            "close": float(row.Close),
            "volume": float(row.Volume),
        }
        for row in df.itertuples(index=False)
    ]
    new_records = [r for r in records if r["datetime"] not in existing_set]
    if not new_records:
        return 0

    stmt = _upsert_statement(session.get_bind(), new_records)
    session.execute(stmt)
    session.commit()
    logger.debug("Inserted %d new bar(s) for %s [%s]", len(new_records), ric, interval)
    return len(new_records)


def read_bars(
    session: Session,
    ric: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Read cached bars for (ric, interval) within [start, end], inclusive.

    Returns the same canonical schema/dtypes as
    core.downloader.download_history: columns [Date, Open, High, Low,
    Close, Volume], Date as datetime64[ns], OHLCV as float64, ascending
    order. Empty match returns an empty frame with the correct columns.
    """
    rows = session.execute(
        select(PriceBar)
        .where(
            PriceBar.ric == ric,
            PriceBar.interval == interval,
            PriceBar.datetime >= start,
            PriceBar.datetime <= end,
        )
        .order_by(PriceBar.datetime)
    ).scalars().all()

    if not rows:
        return pd.DataFrame(columns=_CANONICAL_COLUMNS)

    df = pd.DataFrame(
        {
            "Date": [r.datetime for r in rows],
            "Open": [r.open for r in rows],
            "High": [r.high for r in rows],
            "Low": [r.low for r in rows],
            "Close": [r.close for r in rows],
            "Volume": [r.volume for r in rows],
        }
    )
    df["Date"] = pd.to_datetime(df["Date"])
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.reset_index(drop=True)


def get_sync_ranges(
    session: Session, ric: str, interval: str
) -> list[tuple[datetime, datetime]]:
    """Return all confirmed-downloaded coverage windows for (ric, interval),
    sorted ascending by start."""
    rows = session.execute(
        select(SyncRange)
        .where(SyncRange.ric == ric, SyncRange.interval == interval)
        .order_by(SyncRange.start_datetime)
    ).scalars().all()
    return [(r.start_datetime, r.end_datetime) for r in rows]


def record_sync_range(
    session: Session,
    ric: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> None:
    """Record [start, end] as confirmed-downloaded coverage for (ric, interval).

    Merges with any existing range that overlaps [start, end]. Two
    non-overlapping ranges are only merged when the gap between them is
    no larger than one bar interval (see bar_delta) -- i.e. there's
    provably no room for an un-fetched bar in the gap. A larger gap is
    left as two separate rows, since data there genuinely hasn't been
    confirmed as downloaded and must still be treated as missing.
    """
    delta = bar_delta(interval)
    existing_rows = list(
        session.execute(
            select(SyncRange).where(SyncRange.ric == ric, SyncRange.interval == interval)
        ).scalars().all()
    )

    merged_start, merged_end = start, end
    absorbed: list[SyncRange] = []
    changed = True
    while changed:
        changed = False
        for row in existing_rows:
            if row in absorbed:
                continue
            gap = max(
                row.start_datetime - merged_end,
                merged_start - row.end_datetime,
                timedelta(0),
            )
            if gap <= delta:
                merged_start = min(merged_start, row.start_datetime)
                merged_end = max(merged_end, row.end_datetime)
                absorbed.append(row)
                changed = True

    for row in absorbed:
        session.delete(row)
    session.add(
        SyncRange(ric=ric, interval=interval, start_datetime=merged_start, end_datetime=merged_end)
    )
    session.commit()
    logger.debug(
        "Recorded sync range for %s [%s]: %s -> %s (absorbed %d existing range(s))",
        ric, interval, merged_start, merged_end, len(absorbed),
    )
