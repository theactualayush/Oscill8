"""
tests/test_connection.py

Unit tests for database/connection.py: engine creation, idempotent
schema creation, parent-directory auto-creation, and the RBS_SQLITE_PATH
env-var override.
"""

from __future__ import annotations

import importlib

from sqlalchemy import text

from core import config
from database.connection import get_engine, get_session, init_db


# ---------------------------------------------------------------------
# get_engine / init_db
# ---------------------------------------------------------------------

def test_get_engine_creates_sqlite_file_at_given_path(tmp_path):
    db_path = tmp_path / "cache.db"
    engine = get_engine(str(db_path))
    init_db(engine)
    assert db_path.exists()


def test_init_db_creates_parent_directory_if_missing(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "cache.db"
    engine = get_engine(str(db_path))
    init_db(engine)
    assert db_path.exists()


def test_init_db_is_idempotent(tmp_path):
    engine = get_engine(str(tmp_path / "cache.db"))
    init_db(engine)
    init_db(engine)  # should not raise


# ---------------------------------------------------------------------
# get_session
# ---------------------------------------------------------------------

def test_get_session_returns_working_session(db_engine):
    with get_session(db_engine) as session:
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1


# ---------------------------------------------------------------------
# RBS_SQLITE_PATH override
# ---------------------------------------------------------------------

def test_sqlite_path_env_override(monkeypatch, tmp_path):
    override_path = str(tmp_path / "custom" / "oscill8.db")
    monkeypatch.setenv("RBS_SQLITE_PATH", override_path)
    importlib.reload(config)
    try:
        assert config.SQLITE_DB_PATH == override_path
    finally:
        monkeypatch.delenv("RBS_SQLITE_PATH", raising=False)
        importlib.reload(config)
