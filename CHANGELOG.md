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

---

## v0.5.0

### Added
- Multi-Lookback / Stability Analytics (`range_analytics/multi_lookback.py`,
  `range_analytics/stability.py` — Module 4B): repeatedly analyzes one
  `StrategyHistory` at multiple lookback windows and describes how
  Module 4A's measurements move across them (dispersion, short-vs-long
  change, pairwise diffs/ratios), built entirely on top of
  `analyze_range()` rather than reaching into its internals.
- Unit Tests (`tests/test_range_stability.py`, `tests/test_range_multi_lookback.py`)

---

## v0.6.0

### Added
- Template / Candidate Universe Engine (`template_scanner/templates.py`,
  `template_scanner/universe.py` — Module 5A): translates dense
  grid-style weight vectors into `StrategyDefinition`s
  (`template_from_dense_weights()`) and rolls one or many templates
  across a market's eligible contracts into a deduplicated candidate
  universe (`generate_candidates()`, `generate_candidate_universe()`,
  `dedupe_candidates()`).
- Unit Tests (`tests/test_template_scanner_templates.py`,
  `tests/test_template_scanner_universe.py`)

---

## v0.7.0

### Added
- Scanner Orchestration (`template_scanner/scanner.py`,
  `template_scanner/filters.py`, `template_scanner/ranking.py`,
  `template_scanner/scan_results.py` — Module 5B): `run_scan()` prices
  a candidate universe through `strategy_engine` (one shared leg cache
  per scan), measures each history through `range_analytics`, and
  offers optional filtering (`FilterCriterion`/`apply_filters()`) and
  transparent multi-key ranking (`SortKey`/`rank_results()`) — never a
  composite score, never a hard-coded threshold.
- Unit Tests (`tests/test_template_scanner_scanner.py`,
  `tests/test_template_scanner_filters.py`,
  `tests/test_template_scanner_ranking.py`,
  `tests/test_template_scanner_scan_results.py`)

### Fixed
- **Scans aborting entirely on one unavailable RIC (5B.1 hardening).**
  `run_scan()` now catches the typed `core.downloader.
  MarketDataUnavailableError` (LSEG's confirmed "universe is not found,"
  error code `TS.Interday.UserRequestError.70005`) around each
  candidate's `build_history()` call, skips just that candidate
  (recorded on the new `ScanReport.skipped`), and continues the scan —
  previously any such candidate would raise uncaught and abort the
  entire scan. A confirmed-unavailable RIC is remembered for the rest
  of that scan so later candidates referencing it are skipped without a
  repeat LSEG attempt. Every other exception still propagates and
  aborts the scan, unchanged.
- **Filters/ranking could not use derived metrics.** Added
  `template_scanner/metrics.py::metric_value()` as the single canonical
  resolver for "a scalar metric by name on a RangeAnalytics" (a direct
  field or a derived metric — `normalized_crossing_frequency`,
  `range_to_volatility_ratio`, `robust_to_full_width_ratio`).
  `results_to_dataframe()` and `filters.at_lookback()` now both resolve
  through this one function, so a metric name means the same thing in
  the result table and in filter/rank accessors — previously
  `filters.py` could only resolve raw `RangeAnalytics` attributes via
  `getattr()`, so a filter/sort on a derived metric was not possible.
  (`tests/test_template_scanner_metrics.py`)

---

## v0.8.0

### Added
- Streamlit Range-Bound Scanner UI, initial version (`ui/` — Module 6A):
  scan configuration form, free-text ratio strategy-template grid, Run
  Scan wired to `run_scan()`, skipped-candidates section, the seven
  Module 5B filters and primary/secondary ranking as visible controls,
  ranked result table, single-row selection with a placeholder
  selected-strategy summary.
- Unit Tests (`tests/test_ui_formatting.py`)

---

## v0.9.0

### Changed
- **Compact, trader-facing scanner redesign (Module 6A).** Reworked the
  UI for information density and a professional trading-workstation
  feel rather than a generic form:
  - Scan configuration collapsed into a compact, grouped, bordered panel
    (Market/Interval/Universe date-range/History date-range on one row;
    Lookbacks/Primary Lookback/Run Scan on the next), replacing one
    widget per row.
  - Free-text ratio entry (`"1 | -2 | 1"`) replaced by a strategy-grid
    matrix: curve positions as editable columns, one row per template,
    adjustable position count.
  - Range-Bound Opportunities made the dominant section: an
    analyzed/skipped/shown status line, a `Ranked by: <metric> ↑/↓ ·
    Lower/Higher is better` label, a visible `Rank` column, and
    `Ranking ▾`/`Filters ▾` moved into `st.popover` controls attached to
    the results header instead of a permanently-visible filter panel.
  - Selected-strategy panel redesigned as a compact stat row (Current /
    Robust Range / Median / Position / Efficiency Ratio) instead of a
    bare debug-style caption.
  - Result-table columns curated to a trader-facing subset (`Rank`,
    `Strategy`, `Ratio`, `Current`, `Median`, `Pos`, `Width`, `ER`,
    `Cross Freq`, `Half-Life`, `AR1 β`) with tooltip help text on
    non-obvious columns.
- No backend (Modules 1–5) changes; `ui/formatting.py`,
  `ui/controls.py`, `ui/results_view.py`, `ui/scan_view.py`, `ui/app.py`
  updated, `tests/test_ui_formatting.py` extended.

---

## v0.10.0

### Added
- Selected-Strategy History Chart (`ui/chart_view.py` — Module 6B): a
  Plotly chart of the selected candidate's Strategy price series with
  its Robust Low/Median/Robust High levels overlaid (shaded band, latest
  observation marked), a `plotly_dark` theme, and a Chart Horizon
  selector restricted to the lookbacks the scan actually requested.
  Sources data entirely from the already-run scan — no new LSEG call on
  selection or on switching the chart horizon (verified with an
  instrumented call-counting test harness); overlay levels are read
  from the candidate's already-computed analytics, never recomputed.
- Unit Tests (`tests/test_ui_chart.py`)

### Changed
- Further UI polish alongside the chart: strategy-grid blank/unused
  cells now render genuinely blank (previously showed the literal text
  `"None"` — a Streamlit `NumberColumn` rendering limitation for
  unpopulated numeric cells, verified empirically across dtypes; grid
  cells are now `TextColumn` with a numeric-pattern validator instead),
  curve-position column headers simplified to bare numbers, `Lookbacks`
  relabeled `Lookbacks (bars)` and `Display Lookback` relabeled `Primary
  Lookback` (with tooltip) to make the bars-not-calendar-days semantics
  explicit, and the Range-Bound Opportunities / Selected Strategy
  sections wrapped in bordered panels for visual grouping.
- `tests/test_ui_formatting.py` extended, `tests/test_ui_controls.py`
  added.

---

## v0.10.1

### Fixed
- **SONIA `DAILY` Close silently NaN (live LSEG finding).** SONIA's
  `DAILY` response has `TRDPRC_1`/`OPEN_PRC`/`HIGH_1`/`LOW_1`/bid/ask
  entirely NA; the real daily price is carried in `SETTLE`, which
  `core/downloader.py::_normalize_columns` previously discarded
  entirely during canonicalization. `_normalize_columns` now accepts a
  `settle_fallback_for_close` flag (set only when the request's
  top-level interval is `DAILY`); when set, a `SETTLE` column, if
  present, row-wise fills only the NaN gaps remaining in `Close` after
  the primary alias is coerced (`Close = Close.fillna(SETTLE)`) —
  never a global replacement. SOFR/Fed Funds/€STR have both `TRDPRC_1`
  and `SETTLE` populated (and they can differ slightly); their existing
  `TRDPRC_1`-derived `Close` is unaffected. Open/High/Low are never
  fabricated from `SETTLE`. `HOURLY`/`4H` semantics are unchanged —
  `SETTLE` is never consulted there. (`tests/test_downloader.py`)
- **€STR (ESTR) RIC convention wrong (live LSEG finding).** The
  configured root/year-digit convention (`ric_root="ESR"`,
  `ric_year_digits=1`, e.g. `ESRU6`) returned LSEG error 70005 ("The
  universe is not found"). Live testing confirmed the correct
  convention is `ric_root="SRE"`, `ric_year_digits=2` (e.g. `SREU26`),
  which returns full `TRDPRC_1`/OHLC/`SETTLE`/bid/ask data. Fixed
  entirely via `core/config.py`'s `ESTR` `MarketDefinition` — no
  change to `core.ric`/`core.futures_calendar`, confirming the
  existing generic root+year-digit RIC builder already supports this
  without per-market branching. (`tests/test_ric.py`,
  `tests/test_strategy_combinations.py`)

### Notes
- CORRA's RIC convention (`CRAU6`, `CRAH7`, ...) was independently
  confirmed correct and left unchanged — live testing found the
  current LSEG account lacks entitlement for this universe
  (`TS.Interday.UserNotPermission.70112`), a permissions issue, not an
  Oscill8 RIC bug.

---

## v0.11.0

### Fixed
- **CORRA's `70112` entitlement error was not a typed
  `MarketDataUnavailableError`.** LSEG's confirmed "no permission for
  this universe" response (`TS.Interday.UserNotPermission.70112`) is
  now classified alongside the existing `70005` ("universe not found")
  classifier in `core/downloader.py` — same narrow, exact-match,
  duck-typed approach (LSEG's real `LDError` type + this exact error
  code), excluded from tenacity retry. (`tests/test_downloader.py`)

### Added
- **QuantHub as a second market-data provider** (`core/quanthub.py`,
  `core/providers.py` — early version of what CLAUDE.md now documents
  as Module 8). `core.providers.PROVIDER_ROUTING`/`resolve_provider()`
  is the single source of truth for which markets are QuantHub-eligible;
  today that's CORRA, SONIA, EURIBOR, SARON, YBA, and ESTR_ICE.
- **New market metadata for EURIBOR, SARON, YBA, and ESTR_ICE**
  (`core/config.py`'s `MARKETS`) — complete, trader-confirmed RIC root/
  exchange/`bp_per_point`/year-digits for each, `verified=False` since
  none has been live-LSEG-tested in this environment. `ESTR_ICE` is a
  deliberately separate market key from the existing CME `ESTR` entry
  (different RIC root, `SRE` vs. `EON3`) — never collapsed with it.
- **QuantHub request capping and retry behavior**: a hard
  `QUANTHUB_MAX_ROWS_PER_REQUEST = 10_000`-row ceiling per HTTP request
  (live-confirmed: 10,000 succeeds, 10,001 returns HTTP 400), and HTTP
  429 responses are no longer retried indefinitely.
- **QuantHub request batching across a scan**: distinct instruments
  needed within one scan are grouped into requests up to
  `QUANTHUB_BATCH_SIZE = 10` instruments each (live-verified maximum);
  because the row ceiling is shared across every instrument in a
  request, `core.quanthub._max_count_for_batch(batch_size)` computes
  `10_000 // batch_size` as the resulting per-instrument row cap for
  that batch size — a smaller trailing batch legitimately gets a higher
  per-instrument count than a full batch of 10.
- **Documented, confirmed QuantHub API limitation**: live testing
  confirmed the `/api/v2/ohlc/` endpoint accepts only `instruments=`/
  `interval=`/`count=` — `start=`/`end=` return HTTP 500,
  `from=`/`to=`/`offset=`/`page=`/`cursor=`/`before=` are all silently
  ignored (byte-identical response with or without them). `count=`
  always means "the most recent N observations as of now" — there is no
  way to anchor a request to an earlier reference point, so older
  history beyond what a single request's effective count can reach is
  genuinely unretrievable in one call. This is why the SQLite cache
  matters more for a QuantHub-served `(ric, interval)` than for an
  LSEG-served one: a cold-started instrument only ever gets whatever
  the ceiling allows on its first fetch, and accumulates the rest over
  repeated scans as "now" (and therefore QuantHub's own reachable
  window) advances — not via any incremental range request, since none
  exists. (`tests/test_quanthub.py`)

---

## v0.12.0

### Added
- **Provider provenance** (`database/models.py`'s new nullable
  `SyncRange.provider` column, `database/cache.py`'s
  `get_established_provider()`/`record_sync_range(..., provider=...)`,
  `database/service.py`'s rewritten `get_history()`/
  `get_history_batch()`): a persisted, per-`(ric, interval)` decision of
  which provider (LSEG or QuantHub) actually serves a given
  contract/interval, for every QuantHub-eligible market. Decided
  **exactly once** per `(ric, interval)`, on first request: LSEG is
  tried for the full window first; a complete response establishes
  LSEG, an incomplete/unavailable one discards the LSEG attempt
  entirely (never persisted) and establishes QuantHub instead. Once
  established, the decision is never automatically revisited —
  established LSEG fetches only missing sub-ranges from LSEG forever
  after; established QuantHub re-requests QuantHub's full effective
  window whenever anything is missing (a QuantHub API limitation, not a
  design choice), and never touches LSEG again either way. A
  `(ric, interval)`'s bars are never a mix of both providers once
  established.
- **Legacy/unknown provenance handling**: a `(ric, interval)` with
  `provider=NULL` but existing `sync_ranges` coverage (cached before
  this column existed) is treated as a genuinely distinct fourth state,
  not silently relabeled LSEG or QuantHub. `provider` stays `NULL`
  permanently for such a row regardless of which provider actually
  serves any given missing sub-range going forward — only an explicit
  administrative reset (`cache.delete_bars_and_sync_ranges()`) can move
  it into fresh establishment.
- `connection.py`'s `init_db()` gained a small, additive, idempotent
  migration (`ALTER TABLE sync_ranges ADD COLUMN provider`) so an
  existing local `data/oscill8.db` gets the new column in place, without
  losing any cached history.
- Unit tests (`tests/test_service_provider_fallback.py`,
  `tests/test_service_provider_routing.py`,
  `tests/test_multimarket_cache_key_independence.py`) covering: no false
  establishment for a legacy cache, no provider mixing once established,
  and independent provider state across intervals for the same
  contract.

---

## v0.13.0

### Fixed
- **A still-forming bar was re-fetched from the provider on every
  identical re-scan.** A plain-date request (e.g. "today") previously
  stayed uncapped to day-end all the way through missing-range detection
  and into the provider request itself, so a scan for a currently-open
  interval (e.g. an in-progress `4H` bucket) re-requested the same
  partial bar every time, since nothing in that always-in-the-future
  tail could ever be marked synced.
  - `database/service.py`: every request's effective end is now capped,
    before any cache-coverage check or provider call, to the last fully
    closed bar as of "now" (`_effective_request_end`,
    `_last_completed_boundary`) — applied independently in
    `get_history()`, `_get_history_batch_with_provenance()`, and
    `_get_history_batch_quanthub()`, and identically across `DAILY`,
    `HOURLY`, and `4H`. A still-forming bar is consequently never
    fetched, cached, **or returned** to any caller — a change from the
    prior behavior, where such a bar was still returned for that one
    call (never cached, but visible in the result).
  - `_persist_downloaded()`'s existing `Date < boundary` filter is the
    actual, load-bearing enforcement point — not a redundant safety
    net: QuantHub's own API has no way to be asked, server-side, to
    exclude a same-day in-progress bar.
  - If the entire requested window falls inside the currently-forming
    period, no provider is contacted at all — including skipping a
    genuinely-new QuantHub-eligible RIC's one-time establishment test,
    rather than running it against a window that could never produce a
    usable result.
- Unit tests (`tests/test_service_effective_request_end.py`): a repeated
  identical scan during the same still-forming period now makes zero
  additional provider requests, for every interval and every provider
  state; the next request after the period closes fetches exactly the
  newly-closed bar.

---

## v0.14.0

### Fixed
- **A legacy/unknown-provider RIC's LSEG failure aborted the entire
  scan instead of falling back to QuantHub.** The legacy/unknown
  provenance path (see v0.12.0) previously left `MarketDataUnavailableError`
  uncaught for these rows — unlike every other provider state — on the
  theory that falling back to QuantHub might risk fabricating QuantHub
  provenance. That reasoning didn't hold once `provider` is recorded as
  `NULL` unconditionally on every branch of the legacy path regardless
  of which provider actually served the data.
  - `database/service.py`'s `_fetch_legacy_unknown_provider()` (also
    reused by `_get_history_batch_with_provenance()`'s legacy branch, an
    inline duplicate eliminated in the same change): for each missing
    sub-range, LSEG is tried first; if it fails with
    `MarketDataUnavailableError`, is empty, or is incomplete, QuantHub
    is fetched as a fallback for just that sub-range — reusing the
    already-existing `_is_complete_history()` and
    `_download_quanthub_full_window()` helpers unchanged. `provider`
    still stays `NULL` on every branch. Live-observed in production for
    `CRAU7 [4H]`.
- **The `TS.Intraday.UserNotPermission.92000` classifier matched on the
  wrong wording.** Added in an earlier round requiring both the error
  code and the exact phrase `"User does not have permission for this
  universe"` — but the real production error text is `"User has no
  permission"`, a different phrase with the same code, so the
  classifier never actually matched live. `core/downloader.py`'s
  `_is_confirmed_no_intraday_permission()` now matches on the error
  **code alone**, deliberately not requiring any specific trailing
  wording — unlike the `70005`/`70112` classifiers, which still require
  an exact phrase match. Confirmed not retried by tenacity
  (`call_count == 1`, no retry storm) both at the `core.downloader`
  classifier level and at the `database.service` legacy-fallback level.
  (`tests/test_downloader.py`, `tests/test_service_provider_fallback.py`)