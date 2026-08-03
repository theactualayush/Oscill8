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
- 139 tests passing in total (102 pre-existing from Modules 1 + 2 + 37
  new for Module 3)
- Manually validated against real historical data using a live SOFR
  fly (`SRAZ26` / `SRAH27` / `SRAM27`, weights `+1` / `-2` / `+1`) —
  calculated strategy prices matched manual calculation exactly