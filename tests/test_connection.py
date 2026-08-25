"""
tests/test_connection.py

Unit tests for database/connection.py: engine creation, idempotent
schema creation, parent-directory auto-creation, and the RBS_SQLITE_PATH
env-var override.
"""

from __future__ import annotations

import importlib

from sqlalchemy import inspect, text

from core import config
from database import connection
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
# sync_ranges.provider additive migration (provider-provenance design)
# ---------------------------------------------------------------------

def test_init_db_creates_provider_column_on_a_brand_new_database(tmp_path):
    engine = get_engine(str(tmp_path / "cache.db"))
    init_db(engine)
    columns = {col["name"] for col in inspect(engine).get_columns("sync_ranges")}
    assert "provider" in columns


def test_init_db_adds_provider_column_to_a_pre_existing_database_without_losing_data(tmp_path):
    # Simulate a database created BEFORE the provider column existed:
    # build the table manually, without it, and seed a row.
    engine = get_engine(str(tmp_path / "legacy.db"))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE sync_ranges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ric VARCHAR(32) NOT NULL,
                    interval VARCHAR(8) NOT NULL,
                    start_datetime DATETIME NOT NULL,
                    end_datetime DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO sync_ranges "
                "(ric, interval, start_datetime, end_datetime, updated_at) "
                "VALUES ('SRAH26', 'DAILY', '2026-01-01', '2026-01-05', '2026-01-01')"
            )
        )

    columns_before = {col["name"] for col in inspect(engine).get_columns("sync_ranges")}
    assert "provider" not in columns_before

    init_db(engine)  # must migrate in place, not raise, not drop the existing row

    columns_after = {col["name"] for col in inspect(engine).get_columns("sync_ranges")}
    assert "provider" in columns_after

    with engine.connect() as conn:
        row = conn.execute(text("SELECT ric, provider FROM sync_ranges")).fetchone()
    assert row.ric == "SRAH26"
    assert row.provider is None  # pre-existing rows have no provenance recorded


def test_init_db_provider_migration_is_idempotent(tmp_path):
    engine = get_engine(str(tmp_path / "cache.db"))
    init_db(engine)
    init_db(engine)  # second call must be a no-op, not raise "duplicate column"
    columns = {col["name"] for col in inspect(engine).get_columns("sync_ranges")}
    assert "provider" in columns


# ---------------------------------------------------------------------
# Default engine auto-initializes the schema on first use
# ---------------------------------------------------------------------

def test_default_engine_auto_initializes_schema_on_a_fresh_database(monkeypatch, tmp_path):
    """Reproduces the real-world failure: a brand-new installation, where
    nobody has called init_db() manually, must still work end-to-end via
    get_session()/get_engine() -- not raise sqlite3.OperationalError: no
    such table: sync_ranges."""
    fresh_db_path = str(tmp_path / "oscill8.db")  # tmp_path already exists,
    # isolating exactly the "schema never created" failure mode reported.
    monkeypatch.setattr(config, "SQLITE_DB_PATH", fresh_db_path)
    monkeypatch.setattr(connection, "_engine", None)  # simulate a fresh process

    with connection.get_session() as session:
        # sync_ranges (and price_bars) must already exist -- this is
        # exactly the query database.cache.get_sync_ranges() runs as the
        # first step of get_history().
        result = session.execute(text("SELECT COUNT(*) FROM sync_ranges")).scalar()

    assert result == 0


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
