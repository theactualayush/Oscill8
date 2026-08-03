# Oscill8 – Development Context

## Product

Oscill8 is an internal quantitative trading application for identifying
range-bound relative-value opportunities across global interest-rate
futures markets.

Initial markets:

- SOFR
- Fed Funds
- SONIA
- CORRA
- Eurozone STIR / €STR

The application will allow a trader to construct multi-leg strategies,
scan combinations of futures contracts, identify strategies exhibiting
range-bound behaviour, and inspect their historical behaviour.

The eventual interface will be built with Streamlit.

---

# Core Architecture

LSEG Workspace
      ↓
LSEG Downloader
      ↓
Local Market Data Cache
      ↓
Strategy Engine
      ↓
Analytics Engine
      ↓
Streamlit UI

Important architectural rule:

Only the data/downloader layer should communicate with LSEG.

Strategy calculations and analytics should operate on normalized
Pandas DataFrames and should not directly depend on LSEG.

---

# Current Status

## Module 1 – LSEG Data Layer

COMPLETED AND TESTED.

Current modules include:

core/
    config.py
    downloader.py
    futures_calendar.py
    ric.py
    utils.py

tests/
    test_downloader.py
    test_futures_calendar.py
    test_live_connection.py
    test_ric.py

The project was reorganized into packages and the test suite currently
passes.

Do not rewrite Module 1 unnecessarily.

Preserve its public interfaces unless there is a compelling technical
reason to change them.

## Module 2 – SQLite Market-Data Cache

COMPLETED AND TESTED.

Implements the cache-first `get_history(ric, interval, start, end)`
entry point (`from database import get_history`) that sits between
`core.downloader` and future consumers.

database/
    __init__.py    (re-exports get_history)
    connection.py  (engine/session, auto-creates schema + data/ dir)
    models.py      (PriceBar, SyncRange — SQLAlchemy 2.0 ORM)
    cache.py       (upsert/read/sync-range bookkeeping)
    service.py     (get_history orchestration, missing-range detection)

tests/
    conftest.py, test_connection.py, test_models.py, test_cache.py,
    test_service.py

Key design points a future session needs:

- DB path: `data/oscill8.db` at the repo root, via
  `core.config.SQLITE_DB_PATH` (override with env var
  `RBS_SQLITE_PATH`). `core.config.py` also gained `REPO_ROOT_DIR`,
  added additively — no existing Module 1 lines were changed.
- Schema: `price_bars` (ric, interval, datetime, OHLCV) with a
  database-level `UniqueConstraint(ric, interval, datetime)` —
  duplicates are impossible even if application logic has a bug.
  `sync_ranges` tracks confirmed-downloaded `(start_datetime,
  end_datetime)` coverage per (ric, interval) at **datetime**, not
  date, granularity — required for intraday intervals (HOURLY, 4H).
- Dedup/upsert uses `ON CONFLICT DO NOTHING`, isolated behind
  `cache._upsert_statement()` (dispatches on SQL dialect) so a future
  PostgreSQL migration touches one function, not every call site.
- Sync-range merging: overlapping ranges always merge; non-overlapping
  ranges only merge if the gap is `<=` one bar interval
  (`cache.bar_delta`) — i.e. provably no room for an un-fetched bar.
- The currently in-progress bar (e.g. today's still-open DAILY bar, or
  the current HOURLY bar) is never cached or marked synced — it's
  returned to the caller for that call but re-fetched from LSEG every
  time until it closes. This avoids ever caching a bar whose value can
  still change.
- Known constraint carried over from Module 1: `core.downloader.
  download_history` only accepts date-level (not sub-day) start/end
  boundaries, so intraday cache-miss downloads still fetch full
  calendar days at a time. Harmless (extras are deduped on insert) but
  worth knowing before building Module 3 on top of this.
- Test suite: 96/96 passing (54 Module 1 + 42 Module 2) locally with
  `requirements.txt` installed. `test_live_connection.py` still
  requires a real LSEG Workspace desktop session and cannot run in a
  headless/remote environment.

---

# LSEG

The application currently uses the LSEG Data Library through an
authenticated LSEG Workspace desktop session.

Historical data retrieval has already been successfully tested.

The application is being developed locally first.

Cloud/server deployment will be considered later and may require
different LSEG authentication.

Do not solve cloud deployment yet.

---

# Desired Data Behaviour

Implemented in `database/service.py` (see Module 2 status above):

get_history(
    ric,
    interval,
    start,
    end
)

Behaviour:

Request
   ↓
Check SQLite
   ↓
Is requested history available?
   ├── Yes → return cached DataFrame
   │
   └── No
        ↓
   Determine missing period
        ↓
   Download ONLY missing history from LSEG
        ↓
   Store in SQLite
        ↓
   Return complete DataFrame

The caller should not need to know whether the data came from LSEG
or SQLite.

---

# Important Development Rules

1. Do not unnecessarily rewrite working Module 1 code.
2. Keep modules small and independently testable.
3. Use type hints.
4. Use docstrings.
5. Use logging.
6. Avoid hard-coded paths.
7. Keep secrets and machine-specific configuration out of Git.
8. Return Pandas DataFrames from public market-data interfaces.
9. Prevent duplicate market-data rows at the database level.
10. Write tests for every new module.
11. Run the existing test suite after changes.
12. Do not implement Streamlit, strategy analytics, or additional
    features outside the scope of the module currently being worked on.

The priority is correctness and maintainability over adding features.

---

# Development Roadmap

Module 1
LSEG data layer
STATUS: COMPLETE

Module 2
SQLite market-data cache
STATUS: COMPLETE

Module 3
Data service / integration
STATUS: NEXT

Module 4
Strategy engine

Module 5
Range-bound analytics

Module 6
Streamlit application

Module 7
Deployment architecture

---

# Instructions Before Making Changes

Before modifying the project:

1. Inspect the existing repository.
2. Read the existing implementation.
3. Read the tests.
4. Understand the existing public APIs.
5. Run the current tests.
6. Explain the proposed architecture for the next module.
7. Identify any assumptions or compatibility issues.
8. Only then begin implementation.

Do not replace working code merely because you would have designed it
differently.