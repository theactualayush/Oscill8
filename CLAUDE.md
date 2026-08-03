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

# Module 2 – Current Task

Build the SQLite market-data cache.

Database:

data/oscill8.db

Use:

- SQLite
- SQLAlchemy
- Pandas

Proposed package:

database/
    __init__.py
    connection.py
    models.py
    cache.py
    service.py

The database should be created automatically.

The primary historical price table should store:

- RIC
- interval
- datetime
- open
- high
- low
- close
- volume

RIC + interval + datetime must be unique.

Duplicate bars must never be created.

Also maintain synchronization metadata so Oscill8 knows what history
has already been downloaded.

---

# Desired Data Behaviour

Eventually the application should support:

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
    features while working on Module 2.

The priority is correctness and maintainability over adding features.

---

# Development Roadmap

Module 1
LSEG data layer
STATUS: COMPLETE

Module 2
SQLite market-data cache
STATUS: NEXT

Module 3
Data service / integration

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
6. Explain the proposed Module 2 architecture.
7. Identify any assumptions or compatibility issues.
8. Only then begin implementation.

Do not replace working code merely because you would have designed it
differently.