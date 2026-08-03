"""
tests/conftest.py

Shared fixtures for database/ tests: an isolated, file-backed SQLite
engine per test using pytest's tmp_path, so tests never touch the real
data/oscill8.db.
"""

from __future__ import annotations

import pytest

from database.connection import get_engine, get_session, init_db


@pytest.fixture
def db_engine(tmp_path):
    """A fresh SQLite engine backed by a temp file, with schema created."""
    engine = get_engine(str(tmp_path / "test.db"))
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """A Session bound to db_engine, closed automatically after the test."""
    with get_session(db_engine) as session:
        yield session
