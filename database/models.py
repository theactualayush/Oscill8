"""
models.py

SQLAlchemy ORM schema for the local market-data cache.

Two tables:
    PriceBar  -- individual OHLCV bars, unique per (ric, interval, datetime).
    SyncRange -- confirmed-downloaded coverage windows per (ric, interval),
                 used to determine what history is already cached.

No LSEG or Pandas knowledge lives here -- pure schema definition. Callers
convert to/from DataFrames in cache.py.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all Module 2 ORM models."""


class PriceBar(Base):
    """A single OHLCV bar for one RIC at one interval.

    RIC + interval + datetime is unique at the database level (not just
    application logic) so duplicate bars can never be created, even if
    caller-side range bookkeeping has a bug.
    """

    __tablename__ = "price_bars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ric: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("ric", "interval", "datetime", name="uq_price_bars_ric_interval_dt"),
        Index("ix_price_bars_ric_interval_dt", "ric", "interval", "datetime"),
    )


class SyncRange(Base):
    """A confirmed-downloaded coverage window for one (ric, interval).

    A row means "we successfully asked LSEG for bars across the entire
    [start_datetime, end_datetime] span" -- not that every timestamp in
    that span has a bar (non-trading days/holidays are legitimately
    absent). Datetime (not date) granularity is required because
    intraday intervals (HOURLY, 4H) need sub-day coverage precision.
    """

    __tablename__ = "sync_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ric: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    start_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_sync_ranges_ric_interval", "ric", "interval"),
    )
