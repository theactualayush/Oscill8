"""
cache.py

Low-level, DB-facing read/write access to the price-bar and sync-range
tables. DataFrame-in/DataFrame-out at the boundary, no LSEG knowledge --
service.py is the only module that talks to core.downloader.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from core.config import BarInterval
from core.utils import get_logger

from database.models import PriceBar, SyncRange

logger = get_logger(__name__)

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]

# Smallest representable time step, used to build a non-overlapping
# boundary when trimming a sync_ranges row around a deleted window --
# same convention as database.service's own _EPSILON.
_EPSILON = timedelta(microseconds=1)

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


def _to_nullable_float(value) -> float | None:
    """Convert one OHLCV cell to a persistable value: None for any
    representation of "missing" (np.nan, pd.NA, None -- pd.isna catches
    all three), float(value) otherwise.

    None is passed straight through to SQLAlchemy, which binds it as
    SQL NULL -- the genuinely-correct representation of a missing
    market-data field, distinct from a real (if unusual) float value.
    Calling float() directly on a raw cell would raise TypeError on
    pd.NA (it isn't a float subtype) and would silently store an
    ordinary NaN float in what should be a NULL for a plain np.nan
    input if this conversion weren't done explicitly.
    """
    return None if pd.isna(value) else float(value)


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
            "open": _to_nullable_float(row.Open),
            "high": _to_nullable_float(row.High),
            "low": _to_nullable_float(row.Low),
            "close": _to_nullable_float(row.Close),
            "volume": _to_nullable_float(row.Volume),
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
    provider: str | None = None,
) -> None:
    """Record [start, end] as confirmed-downloaded coverage for (ric, interval).

    Merges with any existing range that overlaps [start, end]. Two
    non-overlapping ranges are only merged when the gap between them is
    no larger than one bar interval (see bar_delta) -- i.e. there's
    provably no room for an un-fetched bar in the gap. A larger gap is
    left as two separate rows, since data there genuinely hasn't been
    confirmed as downloaded and must still be treated as missing.

    provider: which provider (see core.providers.Provider) supplied this
        coverage window -- optional, defaults to None so every existing
        caller (any market with no QuantHub mapping) is unaffected. The
        merged row always takes the INCOMING call's provider value, never
        an absorbed row's -- correct because every row for one (ric,
        interval) is guaranteed to already share the same provider (see
        database.service's provider-provenance design), so there is
        nothing to reconcile.
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
        SyncRange(
            ric=ric, interval=interval,
            start_datetime=merged_start, end_datetime=merged_end,
            provider=provider,
        )
    )
    session.commit()
    logger.debug(
        "Recorded sync range for %s [%s]: %s -> %s (absorbed %d existing range(s), provider=%s)",
        ric, interval, merged_start, merged_end, len(absorbed), provider,
    )


def get_established_provider(session: Session, ric: str, interval: str) -> str | None:
    """Return the established provider (see core.providers.Provider,
    e.g. "LSEG" or "QUANTHUB") for (ric, interval), or None if no
    provider has been established yet.

    Every sync_ranges row for a given (ric, interval) is guaranteed to
    carry the SAME provider value once one has been established (see
    database.service's cache -> LSEG -> QuantHub provider-provenance
    design) -- reads whichever row happens to exist first; there is no
    reconciliation logic because there is nothing to reconcile.

    Returns None in THREE distinct situations that this function alone
    cannot tell apart -- callers that need to distinguish them (see
    database.service.get_history/_get_history_batch_with_provenance)
    additionally check whether get_sync_ranges() is non-empty:
      1. No row exists at all -- genuinely never touched.
      2. A row exists with a NULL provider for a market with NO
         QuantHub mapping -- this column is simply never populated for
         those (see SyncRange.provider's own docstring); "no
         established-provider decision applies here" is correct and
         final for this case.
      3. A row exists with a NULL provider for a QuantHub-mapped
         market -- a LEGACY row, most commonly written before the
         provider column existed (migrated to NULL, never backfilled --
         see database/connection.py's _ensure_sync_ranges_provider_
         column). Unlike case 2, this is NOT "no decision applies" --
         it is "a decision was never made, but data already exists".
         Conflating this with case 1 was a real bug: treating a legacy
         row as "nothing established yet" ran the full LSEG-first
         establishment test against the entire historical span and let
         database.service's record_sync_range() merge/relabel that
         whole span under a freshly-decided provider it was never
         actually verified against. See
         database.service._fetch_legacy_unknown_provider for the fix.

    Provider provenance is never inferred from whether bars happen to
    exist -- this column IS the explicit record of that decision, read
    directly, not derived. The ambiguity above is about what None
    itself means, not about this function silently guessing.
    """
    return session.execute(
        select(SyncRange.provider)
        .where(SyncRange.ric == ric, SyncRange.interval == interval)
        .limit(1)
    ).scalar_one_or_none()


def delete_bars_and_sync_ranges(
    session: Session,
    ric: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> int:
    """ADMINISTRATIVE/RESET utility -- NOT part of normal retrieval.

    database.service's cache -> LSEG -> QuantHub provider-provenance
    design (see database/models.py's SyncRange.provider docstring)
    establishes a provider for a (ric, interval) exactly ONCE and never
    revisits that decision, so under normal operation there is no
    "switch providers and clean up stale data" moment for this function
    to guard -- get_history()/get_history_batch() never call it.

    Its purpose is to let an operator manually FORGET a (ric, interval)'s
    established provider and any cached bars/coverage in [start, end],
    so the NEXT request performs LSEG-first provider discovery again
    from a clean slate (get_established_provider() returns None
    immediately afterward, for whatever portion of [start, end] this
    call actually covered). A typical reset call passes a [start, end]
    wide enough to cover everything ever cached for that (ric, interval)
    -- e.g. because a market's real LSEG availability changed and a
    stale QuantHub assignment should be re-evaluated.

    Permanently removes cached bars AND sync-range coverage (which is
    also where provider provenance lives -- deleting/trimming a
    sync_ranges row clears its provider along with it, no separate step
    needed) for (ric, interval) within [start, end].

    Scoped precisely: only this exact (ric, interval) pair, only bars/
    coverage intersecting [start, end] -- another RIC, another interval,
    or data outside this range is never touched. A sync_ranges row that
    only PARTIALLY overlaps [start, end] is TRIMMED to its surviving
    portion(s) (split into two rows if it fully contains [start, end]),
    never deleted wholesale -- valid cache history outside the requested
    window is preserved exactly, WITH its original provider value intact
    on the surviving fragment(s) (a partial reset only forgets the
    portion actually reset, not the provider identity of what remains).

    Transactional: every delete/trim below is applied against the same
    Session and committed together in one call, at the very end -- if
    anything raises before that commit, nothing here is persisted (the
    caller's session is left exactly as it was, matching this module's
    existing insert_bars/record_sync_range convention of one commit per
    logical operation).

    PROVENANCE CAVEAT: price_bars has no per-bar provider column (see
    database/models.py) -- provenance of an existing cached BAR (as
    opposed to a sync_ranges coverage row, which does carry provider) is
    not tracked and cannot be recovered. This function therefore cannot
    selectively remove "only the bars a specific provider wrote"; it
    clears EVERY bar in [start, end] for (ric, interval) regardless of
    which provider originally wrote it. This is the deliberately
    conservative, safe interpretation of a reset: once an operator asks
    to forget this range, nothing in it is trusted to still belong.

    Returns the number of PriceBar rows deleted.
    """
    delete_stmt = delete(PriceBar).where(
        PriceBar.ric == ric,
        PriceBar.interval == interval,
        PriceBar.datetime >= start,
        PriceBar.datetime <= end,
    )
    result = session.execute(delete_stmt)
    deleted_bars = result.rowcount or 0

    existing_ranges = list(
        session.execute(
            select(SyncRange).where(SyncRange.ric == ric, SyncRange.interval == interval)
        ).scalars().all()
    )
    for row in existing_ranges:
        if row.end_datetime < start or row.start_datetime > end:
            continue  # no overlap with [start, end] -- untouched
        if row.start_datetime >= start and row.end_datetime <= end:
            session.delete(row)  # fully inside [start, end] -- remove entirely
            continue
        if row.start_datetime < start and row.end_datetime > end:
            # [start, end] is a strict interior hole in this row -- split
            # into the two surviving pieces on either side of it, each
            # keeping the original row's provider (a partial reset must
            # not erase provenance for the portion NOT being reset).
            session.delete(row)
            session.add(
                SyncRange(
                    ric=ric, interval=interval,
                    start_datetime=row.start_datetime, end_datetime=start - _EPSILON,
                    provider=row.provider,
                )
            )
            session.add(
                SyncRange(
                    ric=ric, interval=interval,
                    start_datetime=end + _EPSILON, end_datetime=row.end_datetime,
                    provider=row.provider,
                )
            )
            continue
        if row.start_datetime < start:
            # Overlaps only the left edge of [start, end] -- trim the
            # row's own right boundary so it stops just before `start`.
            row.end_datetime = start - _EPSILON
            continue
        # Overlaps only the right edge of [start, end] -- trim the row's
        # own left boundary so it starts just after `end`.
        row.start_datetime = end + _EPSILON

    session.commit()
    logger.info(
        "Invalidated cache for %s [%s] in %s -> %s ahead of a provider switch: "
        "deleted %d bar(s), adjusted sync-range coverage",
        ric, interval, start, end, deleted_bars,
    )
    return deleted_bars
