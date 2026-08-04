# Oscill8 — Range-Bound Strategy Scanner

Oscill8 is an internal quantitative research application for discovering
range-bound relative-value opportunities across global interest-rate
futures markets (SOFR, Fed Funds, SONIA, CORRA, and Eurozone STIR / €STR).

The system generates multi-leg strategy candidates (outrights, spreads,
flies, condors, and arbitrary custom weight/offset shapes), builds their
historical price series, measures range-bound behaviour, and — once the
scanner layer lands — will let a trader filter and rank candidates through
a grid-style interface before drilling into a chosen strategy's chart and
analytics.

## Architecture

```
LSEG Workspace
      ↓
LSEG Downloader        (core/)
      ↓
SQLite Cache            (database/)
      ↓
Strategy Engine         (strategy_engine/)
      ↓
Range-Bound Analytics   (range_analytics/)
      ↓
Template / Candidate Universe Engine   (template_scanner/)
      ↓
Scanner / UI            (planned)
```

Only the data/downloader layer (`core/`) talks to LSEG. Every layer above
it operates on normalized Pandas DataFrames and has no LSEG dependency —
enforced by tests, not just convention.

## Completed modules

- **Module 1 — LSEG Data Layer** (`core/`): `download_history(ric, interval,
  start, end)` pulls historical OHLCV bars from LSEG Workspace, with RIC
  construction/parsing (`ric.py`), the futures calendar (`futures_calendar.py`),
  and the market registry (`config.py`).
- **Module 2 — SQLite Market-Data Cache** (`database/`): `get_history(ric,
  interval, start, end)` is the single public entry point — cache-first,
  fetches only the missing range from LSEG, persists it, and returns the
  complete series. Callers never need to know whether data came from LSEG
  or SQLite.
- **Module 3 — Strategy Engine** (`strategy_engine/`): turns individual
  futures contracts into historical multi-leg strategy price series.
  `StrategyDefinition` represents a strategy generically (market, leg
  offsets, leg weights) rather than through per-strategy-name code paths —
  a single-leg outright and an arbitrary N-leg structure go through the
  same path. `generate_instances()` rolls a definition across a market's
  contract curve; `build_history()` / `generate_histories()` fetch and
  weight-combine each leg's price history via `database.get_history`.
- **Module 4A — Range-Bound Analytics** (`range_analytics/`): `analyze_range()`
  computes range/location, movement, oscillation, and mean-reversion
  diagnostics for a `StrategyHistory` over a selected window.
- **Module 4B — Multi-Lookback / Stability Analytics** (`range_analytics/`):
  `analyze_multi_lookback()` re-runs Module 4A's measurements across
  multiple lookback windows and describes how they move relative to each
  other (dispersion, short-vs-long change, step structure).
- **Module 5A — Template / Candidate Universe Engine** (`template_scanner/`):
  see below.

## Supported intervals

- `DAILY`
- `HOURLY`
- `4H` (synthesized from `HOURLY` bars, not a native LSEG interval)

## Module 5A: templates and candidate generation

A strategy shape can be specified as a dense grid of per-position weights,
where `0` means "no leg at this curve position" (a gap). Examples:

```
(1, -2, 1)          outright fly
(1, -1)              outright spread
(1, 0, -2, 0, 1)     gapped fly
(1, -3, 3, -1)       condor-style shape
(2, 0, -1, 0, -1)    gapped, non-unit-ratio shape
```

`template_from_dense_weights()` translates a dense vector like these into
a `StrategyDefinition` (sparse offsets + weights) — weights are preserved
exactly as given and are **never normalized** (`(2, -4, 2)` stays
`(2, -4, 2)`, distinct from `(1, -2, 1)`). `generate_candidates()` /
`generate_candidate_universe()` then roll one or many templates across a
market's eligible contracts (with optional curve-depth and eligible-RIC
filters) into a deduplicated universe of candidate `StrategyInstance`s,
ready to be priced by `strategy_engine` and measured by `range_analytics`.

## Current limitations / deferred functionality

- **Module 5B — Scanner/orchestration** (wiring candidates through pricing
  and analytics, then filtering/ranking them) is not implemented yet.
- **GENERIC continuous-curve mode** (curve-position history independent of
  dated contracts) is not implemented — deliberately deferred until roll
  semantics are approved.
- **Intermarket strategies** (legs spanning more than one market) are not
  implemented — deferred until cross-market contract alignment and
  risk-normalization semantics are designed.
- **Streamlit / UI** is not implemented yet.

## Testing

```
pip install -r requirements.txt
pytest -v
```

Current suite: **286 tests, all passing** (unit tests, LSEG fully mocked —
no live session required). This is a snapshot from the module currently
completed (5A); re-run the command above for the up-to-date count.

`test_live_connection.py` is a manual smoke test, not part of the pytest
suite — run it directly (`python test_live_connection.py`) on a machine
with LSEG Workspace open. Only the `SOFR` market is currently marked
`verified=True` in `core/config.py`; the other four markets' RIC roots are
best-effort placeholders pending live verification.

## Repository structure

```
core/              LSEG downloader, RIC build/parse, futures calendar, market config
database/          SQLite cache (get_history) sitting between core and everything above it
strategy_engine/   StrategyDefinition, rolling contract combinations, historical pricing
range_analytics/   Range-bound (4A) and multi-lookback stability (4B) measurements
template_scanner/  Dense-grid templates → candidate StrategyInstance universe (5A)
tests/             Unit tests for every module above (pytest, LSEG mocked)
```

## Current status

Module 5A (Template / Candidate Universe Engine) is complete and tested.
**Module 5B — Scanner/orchestration** (candidate pricing, analytics, and
filter/rank) is the next planned development step.
