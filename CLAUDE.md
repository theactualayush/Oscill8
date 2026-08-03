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
- Test suite: 102 tests passing across Modules 1 + 2 combined, locally
  with `requirements.txt` installed. `test_live_connection.py` still
  requires a real LSEG Workspace desktop session and cannot run in a
  headless/remote environment (it is a standalone script, not a pytest
  suite, so it contributes 0 to this count in any environment).

## Module 3 – Strategy Engine

COMPLETED AND TESTED.

Turns individual futures contracts into historical strategy price
series — from a single outright contract to arbitrary multi-leg
structures (spreads, flies, condors, custom weight/offset shapes) —
generically rather than through per-strategy-name calculation paths.
Retrieves all historical prices exclusively through
`database.get_history` — never calls `core.downloader` or `lseg.data`
directly.

strategy_engine/
    __init__.py     (public re-exports)
    definitions.py  (StrategyDefinition — generic shape/weight model)
    combinations.py (StrategyInstance, generate_instances — wraps
                     core.futures_calendar; no new RIC/calendar logic)
    pricing.py      (StrategyHistory, build_history, generate_histories
                     — leg retrieval, inner-join alignment, weighting)

tests/
    test_strategy_definitions.py, test_strategy_combinations.py,
    test_strategy_pricing.py

Key design points a future session needs:

- A strategy is represented purely as data — `market_key`, `offsets`,
  `weights`, `interval`, `price_field` — never as a named "outright"/
  "spread"/"fly"/"condor" code path. `StrategyDefinition.__post_init__`
  validates eagerly: offsets must start at 0 and be strictly increasing,
  offsets/weights must match in length (>= 1 leg — single-leg outrights,
  e.g. `offsets=(0,)`/`weights=(1,)`, are first-class so outright
  range/volatility behavior can later be benchmarked against multi-leg
  strategies through the same pipeline), weights can't all be zero.
- `generate_instances(definition, contract_start, contract_end)`
  produces rolling `StrategyInstance`s (concrete RIC tuples) by
  delegating entirely to `core.futures_calendar.generate_contracts` +
  `rolling_windows` — Module 3 never calls `core.ric` directly, since
  `generate_contracts` already returns fully-resolved RIC strings.
- Contract-selection window (`contract_start`/`contract_end`, which
  RICs get combined) and price-history window (`price_start`/
  `price_end`, what date range gets fetched for those legs) are
  independent parameters across `generate_instances` vs.
  `build_history`/`generate_histories` — deliberately not one combined
  range, so callers can generate combinations over a wide contract
  universe while pricing only a narrower window, or vice versa.
- Leg alignment is an **inner join on Date** — the strategy value is
  only computed for timestamps where every leg has an observation.
  No forward-fill: a missing leg bar drops that timestamp entirely
  rather than fabricating a value from a stale price. This is
  interval-agnostic (works identically for DAILY/HOURLY/4H) since all
  three intervals produce deterministic, directly comparable `Date`
  timestamps from `database.get_history`.
- `database.get_history` can return a fully-empty DataFrame (no cached
  rows and nothing from LSEG) with `Date` defaulted to `object` dtype
  rather than `datetime64[ns]` (see `database/cache.py:136`).
  `build_history` explicitly re-coerces each leg's `Date` column via
  `pd.to_datetime` before merging, so an all-empty leg still joins
  cleanly instead of raising a pandas dtype-mismatch error.
- `generate_histories(instances, price_start, price_end)` shares one
  `leg_cache` dict across all instances passed to it, keyed on
  `(ric, interval, price_start, price_end)`, so a RIC shared by several
  overlapping rolling instances (e.g. adjacent flies sharing two of
  three legs) is only fetched from `database.get_history` once. This
  cache is scoped to a single call — no cross-call/global cache, no
  invalidation to manage.
- Metadata (market, RICs, weights, offsets, interval, price field)
  lives on the `StrategyInstance`/`StrategyHistory` dataclasses, not as
  DataFrame columns — `StrategyHistory.history` stays a clean,
  purely-numeric frame (`Date`, `Leg_1..Leg_N`, `Strategy`) for Module
  5's analytics to consume directly.
- `price_field` (default `"Close"`) is validated against a whitelist in
  `definitions.py` — `{"Open", "High", "Low", "Close"}`, all four
  already present in `database.get_history`'s canonical output, so
  selecting any of them is pure column selection with no data-layer
  change. `VWAP`/settlement/bid-ask are deliberately excluded (not in
  that schema); adding a genuinely new field later means a data-layer
  change first, then a one-line whitelist addition here.
- The `core.downloader`/`lseg.data` boundary is enforced structurally,
  not via a source-text/string-matching test (rejected as brittle — it
  false-positives on this module's own docstrings and proves nothing
  about actual behavior): one test inspects `strategy_engine.pricing`'s
  live module namespace for the actual `core.downloader` function
  objects by identity (catches even a renamed import), and another
  patches `core.downloader.download_history` to raise if called at all
  while exercising `build_history`/`generate_histories` with
  `database.get_history` separately mocked.
- Single-market strategies only (`StrategyDefinition.market_key` is
  singular) — cross-market relative-value legs are out of scope for
  this module.
- `core.config.ListingCycle` has no serial/hybrid support (a pre-existing
  config.py TODO — CME SOFR also lists monthly serials alongside
  quarterlies, unsupported today). Module 3 deliberately does not solve
  this: it uses whatever `listing_cycle` each market currently declares
  in `core.config.MARKETS` as-is. Adding a hybrid cycle is an orthogonal
  `core.config`/`core.futures_calendar` change, out of scope here.
- Test suite: 139 tests passing in total (102 pre-existing from
  Modules 1 + 2 + 37 new for Module 3), locally with the pinned
  `requirements.txt` versions installed.
- Manually validated against real historical data using a live SOFR
  fly (`SRAZ26` / `SRAH27` / `SRAM27`, weights `+1` / `-2` / `+1`) —
  the engine's calculated strategy prices matched manual calculation
  exactly.

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
Strategy engine
STATUS: COMPLETE

Module 4
Range-bound analytics
STATUS: NEXT

Module 5
Streamlit application

Module 6
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