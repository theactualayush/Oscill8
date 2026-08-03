"""
tests/test_models.py

Unit tests for database/models.py: the database-level uniqueness
constraint on price_bars, nullability, and schema idempotency.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from database.models import Base, PriceBar, SyncRange


def _make_bar(**overrides) -> PriceBar:
    fields = dict(
        ric="SRAZ26",
        interval="DAILY",
        datetime=datetime(2026, 1, 1),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000.0,
    )
    fields.update(overrides)
    return PriceBar(**fields)


# ---------------------------------------------------------------------
# PriceBar uniqueness
# ---------------------------------------------------------------------

def test_price_bar_unique_constraint_blocks_duplicate_insert(db_session):
    db_session.add(_make_bar())
    db_session.commit()

    db_session.add(_make_bar())
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_price_bar_allows_same_datetime_different_interval(db_session):
    db_session.add(_make_bar(interval="DAILY"))
    db_session.add(_make_bar(interval="HOURLY"))
    db_session.commit()  # should not raise

    count = db_session.query(PriceBar).count()
    assert count == 2


def test_price_bar_allows_same_datetime_different_ric(db_session):
    db_session.add(_make_bar(ric="SRAZ26"))
    db_session.add(_make_bar(ric="SRAH27"))
    db_session.commit()  # should not raise


# ---------------------------------------------------------------------
# Nullability
# ---------------------------------------------------------------------

def test_price_bar_allows_missing_ohlcv_field(db_session):
    # LSEG can legitimately return a bar (e.g. a thin HOURLY bar with no
    # trade printed) where some OHLCV fields have no value while others
    # do -- individually nullable, not dropped or fabricated.
    bar = PriceBar(
        ric="SRAZ26",
        interval="HOURLY",
        datetime=datetime(2026, 1, 1),
        open=100.0,
        high=101.0,
        low=99.0,
        close=None,  # e.g. no trade printed in this hour
        volume=1000.0,
    )
    db_session.add(bar)
    db_session.commit()  # should not raise

    stored = db_session.query(PriceBar).one()
    assert stored.close is None
    assert stored.open == 100.0


def test_price_bar_allows_all_ohlcv_fields_missing(db_session):
    bar = PriceBar(
        ric="SRAZ26",
        interval="HOURLY",
        datetime=datetime(2026, 1, 1),
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
    )
    db_session.add(bar)
    db_session.commit()  # should not raise


@pytest.mark.parametrize("missing_field", ["ric", "interval", "datetime"])
def test_price_bar_identity_fields_remain_mandatory(db_session, missing_field):
    bar = _make_bar(**{missing_field: None})
    db_session.add(bar)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------
# SyncRange
# ---------------------------------------------------------------------

def test_sync_range_basic_insert_and_query(db_session):
    db_session.add(
        SyncRange(
            ric="SRAZ26",
            interval="DAILY",
            start_datetime=datetime(2026, 1, 1),
            end_datetime=datetime(2026, 1, 31),
        )
    )
    db_session.commit()

    rows = db_session.query(SyncRange).filter_by(ric="SRAZ26", interval="DAILY").all()
    assert len(rows) == 1
    assert rows[0].start_datetime == datetime(2026, 1, 1)
    assert rows[0].end_datetime == datetime(2026, 1, 31)


# ---------------------------------------------------------------------
# Schema idempotency
# ---------------------------------------------------------------------

def test_create_all_is_idempotent_against_existing_tables(db_engine):
    Base.metadata.create_all(db_engine)
    Base.metadata.create_all(db_engine)  # should not raise
