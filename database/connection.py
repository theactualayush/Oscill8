"""
connection.py

Engine/session lifecycle for the SQLite market-data cache. No business
logic and no model-specific knowledge beyond calling Base.metadata.create_all
-- everything else in database/ takes a Session, it doesn't open one.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
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
    """
    global _engine

    if db_path is not None:
        return create_engine(f"sqlite:///{db_path}")

    if _engine is None:
        _engine = create_engine(f"sqlite:///{config.SQLITE_DB_PATH}")

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


def get_session(engine: Engine | None = None) -> Session:
    """Return a new Session bound to the given (or default) engine.

    Caller owns the session's lifecycle, e.g.:
        with get_session() as session:
            ...
    """
    return Session(engine or get_engine())
