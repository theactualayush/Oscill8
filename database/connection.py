"""
connection.py

Engine/session lifecycle for the SQLite market-data cache. No business
logic and no model-specific knowledge beyond calling Base.metadata.create_all
-- everything else in database/ takes a Session, it doesn't open one.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from core import config
from core.utils import get_logger

from database.models import Base

logger = get_logger(__name__)

_engine: Engine | None = None


def get_engine(db_path: str | None = None) -> Engine:
    """Return the SQLAlchemy Engine for the SQLite cache.

    Args:
        db_path: Path to the SQLite file. Defaults to
            core.config.SQLITE_DB_PATH (override via RBS_SQLITE_PATH).

    A module-level engine is cached and reused when db_path is not given,
    so repeated calls with no arguments share one connection pool. Passing
    an explicit db_path (e.g. in tests) always returns a fresh engine.

    The schema is guaranteed to exist on the default engine before it's
    ever handed back -- init_db() runs once, right here, the first time
    the default engine is created in this process. This is what lets a
    completely fresh installation "just work" without a manual init_db()
    call: every get_session()/get_history() call goes through this
    singleton, so the one-time cost is paid on first use, not per query.
    """
    global _engine

    if db_path is not None:
        return create_engine(f"sqlite:///{db_path}")

    if _engine is None:
        _engine = create_engine(f"sqlite:///{config.SQLITE_DB_PATH}")
        init_db(_engine)

    return _engine


def init_db(engine: Engine | None = None) -> None:
    """Create the cache schema if it doesn't already exist.

    Idempotent -- safe to call on every application startup. Also creates
    the database file's parent directory if missing, since nothing else
    in the application creates data/ today.
    """
    engine = engine or get_engine()

    db_path = engine.url.database
    if db_path and db_path != ":memory:":
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

    logger.info("Ensuring market-data cache schema exists at %s", db_path)
    Base.metadata.create_all(engine)
    _ensure_sync_ranges_provider_column(engine)


def _ensure_sync_ranges_provider_column(engine: Engine) -> None:
    """Additive migration for sync_ranges.provider (added for the cache
    -> LSEG -> QuantHub provider-provenance design -- see database/
    models.py's SyncRange.provider docstring).

    Base.metadata.create_all() above only creates tables that don't
    exist yet -- it never alters an EXISTING table's columns -- so a
    pre-existing local data/oscill8.db created before this column
    existed would otherwise silently be missing it forever. Unlike the
    earlier PriceBar nullability-tightening migration (which could
    safely tell users to delete and rebuild a pure, fully re-fetchable
    cache), this one must NOT lose an existing installation's cached
    history, so it adds the column to the existing table in place
    instead. Idempotent and cheap: a no-op once the column already
    exists, safe to run on every startup alongside create_all() above.
    """
    inspector = inspect(engine)
    if "sync_ranges" not in inspector.get_table_names():
        return  # brand-new database -- create_all() above already made it with this column
    existing_columns = {col["name"] for col in inspector.get_columns("sync_ranges")}
    if "provider" in existing_columns:
        return
    logger.info("Migrating sync_ranges: adding 'provider' column (additive, no data loss)")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sync_ranges ADD COLUMN provider VARCHAR(16)"))


def get_session(engine: Engine | None = None) -> Session:
    """Return a new Session bound to the given (or default) engine.

    Caller owns the session's lifecycle, e.g.:
        with get_session() as session:
            ...
    """
    return Session(engine or get_engine())
