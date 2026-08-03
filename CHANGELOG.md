# Changelog

## v0.1.0

### Added
- LSEG Workspace Downloader
- RIC Generator
- Futures Calendar
- Utilities
- Live Connection Test
- Unit Tests
- GitHub Repository
- GitHub Project

---

## v0.2.0

### Added
- SQLite Cache Layer (`database/models.py`, `database/connection.py`)
- Database Service (`database/cache.py`, `database/service.py`,
  `get_history(ric, interval, start, end)`)
- Automatic History Updates (cache-first, missing-range-only LSEG
  downloads, sync-range coverage tracking)
- Unit Tests (`tests/test_connection.py`, `tests/test_models.py`,
  `tests/test_cache.py`, `tests/test_service.py`)

---

## v0.3.0

### Added
- Strategy Engine (`strategy_engine/definitions.py`,
  `strategy_engine/combinations.py`, `strategy_engine/pricing.py`)
- Generic strategy model (`StrategyDefinition` — market, offsets,
  weights, interval, price field; no per-strategy-name calculation
  paths), supporting single-leg outrights through arbitrary multi-leg
  structures
- Selectable canonical price field (`Open`/`High`/`Low`/`Close`,
  default `Close`)
- Rolling contract-combination generation (`generate_instances`,
  built on `core.futures_calendar`)
- Historical strategy pricing (`build_history`, `generate_histories`
  — inner-join leg alignment, weighted price calculation, shared
  leg-history cache across overlapping rolling instances)
- Unit Tests (`tests/test_strategy_definitions.py`,
  `tests/test_strategy_combinations.py`, `tests/test_strategy_pricing.py`)

---

## v0.4.0

### Added
- Range-Bound Analytics (`range_analytics/` — Module 4A)

### Fixed
- **Intraday persistence crash on partial OHLC bars.** LSEG can
  legitimately return an HOURLY (or synthesized 4H) bar where some
  OHLCV fields are missing (e.g. no trade printed within that hour)
  while others, like Close, are populated. That missing value could
  surface as `pd.NA` (a pandas nullable-dtype sentinel) rather than
  `np.nan`, and `database/cache.py::insert_bars()` crashed with
  `TypeError: float() argument must be a string or a real number, not
  'NAType'` when persisting it.
  - `core/downloader.py`: normalized OHLCV columns are now explicitly
    cast to plain numpy `float64` so `pd.NA` never survives past
    `_normalize_columns` — missing values become `np.nan`, never
    filled, never dropped.
  - `database/cache.py`: `insert_bars` now converts each OHLCV cell via
    a small `None if pd.isna(value) else float(value)` helper, so
    `pd.NA`/`np.nan`/`None` all persist as SQL `NULL` instead of
    crashing or silently becoming a stored `NaN` float.
  - `database/models.py`: `PriceBar.open/high/low/close/volume` are now
    individually nullable (`ric`/`interval`/`datetime` remain
    mandatory) — a bar with a valid Close but missing Open/High/Low is
    real and still usable (e.g. for a Close-based strategy), not
    dropped or fabricated.

  **Migration note:** this loosens `price_bars`' OHLCV columns from
  `NOT NULL` to nullable. `database/connection.py::init_db()` only
  creates missing tables (`Base.metadata.create_all` does not alter an
  existing table's constraints), so **any local `data/oscill8.db`
  created before this change must be deleted and rebuilt once** — it
  is a pure LSEG cache and fully re-fetchable, so nothing is
  permanently lost. No automatic migration is performed.