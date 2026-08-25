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

    open/high/low/close/volume are individually nullable: LSEG can
    legitimately return an hourly (or synthesized 4H) bar where some
    OHLCV fields have no value (e.g. no trade printed within that hour)
    while others do -- a bar with a valid Close but missing Open/High/
    Low is still real and usable (e.g. for a Close-based strategy), so
    it is persisted with SQL NULL in the missing fields rather than
    dropped or filled with a fabricated value. ric/interval/datetime
    remain mandatory -- a bar's identity must always be complete.

    MIGRATION NOTE: this nullability was tightened from NOT NULL to
    NULLABLE. database.connection.init_db() only creates missing
    tables (SQLAlchemy's create_all does not alter existing ones), so
    any pre-existing local data/oscill8.db created under the old NOT
    NULL schema must be deleted and rebuilt once -- it is a pure LSEG
    cache and fully re-fetchable, so this loses no real data.
    """

    __tablename__ = "price_bars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ric: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("ric", "interval", "datetime", name="uq_price_bars_ric_interval_dt"),
        Index("ix_price_bars_ric_interval_dt", "ric", "interval", "datetime"),
    )


class SyncRange(Base):
    """A confirmed-downloaded coverage window for one (ric, interval).

    A row means "we successfully asked a market-data provider for bars
    across the entire [start_datetime, end_datetime] span" -- not that
    every timestamp in that span has a bar (non-trading days/holidays
    are legitimately absent). Datetime (not date) granularity is
    required because intraday intervals (HOURLY, 4H) need sub-day
    coverage precision.

    provider: which provider ("LSEG" or "QUANTHUB", see core.providers.
        Provider) actually supplied this coverage window -- NULL for a
        market with no QuantHub mapping at all (core.providers.
        resolve_provider returns Provider.LSEG unconditionally for
        these; there is no per-(ric, interval) decision to record, so
        this column is simply never populated for them). For a
        QuantHub-mapped market, every row for a given (ric, interval)
        is guaranteed to carry the SAME provider value once a provider
        has been established (see database.service's cache -> LSEG ->
        QuantHub provider-provenance design and database.cache.
        get_established_provider) -- this column is that provider's
        one and only persisted record; it is never inferred from
        whether bars happen to exist.

        MIGRATION NOTE: this column was added after price_bars/
        sync_ranges already shipped. database.connection.init_db()
        only creates MISSING tables (SQLAlchemy's create_all does not
        alter an existing one) -- unlike the earlier PriceBar
        nullability tightening (whose migration note said to delete
        and rebuild the local cache, since it's fully re-fetchable),
        this column must NOT cost an existing installation its cached
        history, so init_db() additionally runs a small, idempotent
        `ALTER TABLE sync_ranges ADD COLUMN provider` migration for a
        pre-existing database that predates this column (see
        database/connection.py's _ensure_sync_ranges_provider_column).
    """

    __tablename__ = "sync_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ric: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    start_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_sync_ranges_ric_interval", "ric", "interval"),
    )
